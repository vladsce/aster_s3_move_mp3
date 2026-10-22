"""Проход целиком, сигналы, lock и код выхода."""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import EndpointConnectionError
from dateutil.relativedelta import relativedelta
from moto import mock_aws

from app.config import ConfigError
from app.main import (
    PassStats,
    _handle_signal,
    _stop,
    acquire_lock,
    main,
    pause_after_errors,
    run_pass,
    setup_logging,
)
from tests.conftest import make_settings, place_file

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def fill_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    values = {
        "S3_ENDPOINT": "https://s3.test.local",
        "S3_ACCESS_KEY": "AKIATESTKEY",
        "S3_SECRET_KEY": "secret-test-value",
        "S3_BUCKET": "recordings",
        "S3_REGION": "us-east-1",
        "S3_ADDRESSING_STYLE": "path",
        "S3_PREFIX": "records/",
        "AGE_MONTHS": "3",
        "CHECK_INTERVAL_SECONDS": "300",
        "FILE_EXTENSIONS": ".mp3",
        "DELETE_AFTER_UPLOAD": "true",
        "DRY_RUN": "false",
        "LOG_LEVEL": "INFO",
    }
    values.update(extra)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("app.config.load_dotenv", lambda *args, **kwargs: False)


def point_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    records = tmp_path / "records"
    records.mkdir()
    monkeypatch.setattr("app.config.SRC_DIR", records)
    monkeypatch.setattr("app.config.DOWNLOAD_DIR", tmp_path / "download")
    monkeypatch.setattr("app.config.LOG_FILE", tmp_path / "archiver.log")
    monkeypatch.setattr("app.config.LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr("app.config.HEARTBEAT_PATH", tmp_path / "heartbeat")
    return records


def test_s3_outage_keeps_files(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    place_file(settings.src_dir, "a.mp3", b"a", NOW - relativedelta(months=4))
    place_file(settings.src_dir, "b.mp3", b"bb", NOW - relativedelta(months=4))

    class Down:
        def head_object(self, **kwargs: object) -> None:
            raise EndpointConnectionError(endpoint_url="https://s3.hoster.by")

        def upload_file(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("загрузка при недоступном S3")

    stats = run_pass(settings, Down(), now=NOW)
    assert stats.errors == 2
    assert stats.deleted == 0
    assert stats.uploaded == 0
    assert (settings.src_dir / "a.mp3").exists()
    assert (settings.src_dir / "b.mp3").exists()


def test_exception_text_is_scrubbed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    setup_logging(settings.log_file, "INFO")
    place_file(settings.src_dir, "a.mp3", b"a", NOW - relativedelta(months=4))

    class Leaky:
        def head_object(self, **kwargs: object) -> None:
            raise RuntimeError(f"denied {settings.s3_secret_key}")

        def upload_file(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("upload")

    stats = run_pass(settings, Leaky(), now=NOW)
    assert stats.errors == 1
    text = settings.log_file.read_text(encoding="utf-8")
    assert settings.s3_secret_key not in text
    assert "***" in text


def test_signal_sets_stop_flag() -> None:
    _stop.clear()
    _handle_signal(15, None)
    assert _stop.is_set()
    _stop.clear()


def test_lock_is_exclusive_between_processes(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    assert acquire_lock(path) is True
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from app.main import acquire_lock; "
            "raise SystemExit(0 if acquire_lock(Path(sys.argv[1])) else 1)",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1, completed.stderr


def test_config_error_exits_with_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("app.config.load_dotenv", lambda *args, **kwargs: False)
    code = main(["--once"])
    assert code == 1
    error = capsys.readouterr().err
    assert "Ошибка конфигурации" in error
    assert "S3_BUCKET" in error


def test_dry_run_flag_overrides_setting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    point_paths(monkeypatch, tmp_path)
    fill_env(monkeypatch, DRY_RUN="false")
    seen: dict[str, bool] = {}

    def fake_pass(settings: object, client: object, now: object = None) -> PassStats:
        seen["dry"] = bool(getattr(settings, "dry_run"))
        return PassStats()

    monkeypatch.setattr("app.main.run_pass", fake_pass)
    monkeypatch.setattr("app.main.create_s3_client", lambda settings: object())
    assert main(["--once", "--dry-run"]) == 0
    assert seen["dry"] is True


def test_once_returns_error_when_pass_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    point_paths(monkeypatch, tmp_path)
    fill_env(monkeypatch)

    def fake_pass(settings: object, client: object, now: object = None) -> PassStats:
        return PassStats(found=1, errors=1)

    monkeypatch.setattr("app.main.run_pass", fake_pass)
    monkeypatch.setattr("app.main.create_s3_client", lambda settings: object())
    assert main(["--once"]) == 1


def test_pause_grows_by_tens_and_caps_at_four_hours() -> None:
    assert pause_after_errors(1, 0) == 1
    assert pause_after_errors(1, 9) == 1
    assert pause_after_errors(1, 10) == 30
    assert pause_after_errors(1, 19) == 30
    assert pause_after_errors(1, 20) == 60
    assert pause_after_errors(1, 30) == 120
    assert pause_after_errors(300, 10) == 300
    assert pause_after_errors(300, 50) == 480
    assert pause_after_errors(1, 10_000) == 4 * 60 * 60


def test_daemon_slows_after_error_streak_and_resets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    point_paths(monkeypatch, tmp_path)
    fill_env(monkeypatch, CHECK_INTERVAL_SECONDS="1")
    sleeps: list[int] = []
    passes = {"n": 0}

    def fake_pass(settings: object, client: object, now: object = None, streak: int = 0) -> PassStats:
        passes["n"] += 1
        if passes["n"] == 1:
            return PassStats(errors=10, consecutive_errors=streak + 10)
        return PassStats(uploaded=1, consecutive_errors=0)

    def fake_sleep(seconds: int, heartbeat: object = None, beat_every: int = 0) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            _stop.set()

    monkeypatch.setattr("app.main.run_pass", fake_pass)
    monkeypatch.setattr("app.main.sleep_until_stop", fake_sleep)
    monkeypatch.setattr("app.main.create_s3_client", lambda settings: object())
    assert main([]) == 0
    assert sleeps == [30, 1]
    text = (tmp_path / "archiver.log").read_text(encoding="utf-8")
    assert "пауза увеличена: ошибок_подряд=10 пауза=30с" in text
    assert "пауза снова 1с" in text


def test_daemon_stops_after_current_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    point_paths(monkeypatch, tmp_path)
    fill_env(monkeypatch)
    calls = {"n": 0}

    def fake_pass(
        settings: object,
        client: object,
        now: object = None,
        streak: int = 0,
    ) -> PassStats:
        calls["n"] += 1
        _stop.set()
        return PassStats()

    monkeypatch.setattr("app.main.run_pass", fake_pass)
    monkeypatch.setattr("app.main.create_s3_client", lambda settings: object())
    assert main([]) == 0
    assert calls["n"] == 1


def test_busy_lock_exits_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records = point_paths(monkeypatch, tmp_path)
    fill_env(monkeypatch)
    monkeypatch.setattr("app.main.acquire_lock", lambda path: False)
    code = main(["--once"])
    assert code == 1
    text = (tmp_path / "archiver.log").read_text(encoding="utf-8")
    assert "docker compose stop archiver" in text
    assert "secret-test-value" not in text
    assert "AKIATESTKEY" not in text
    assert records.exists()


@mock_aws
def test_count_lists_prefix_without_changing_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import boto3

    records = point_paths(monkeypatch, tmp_path)
    fill_env(monkeypatch)
    place_file(records, "keep.mp3", b"local", datetime.now(timezone.utc))
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="recordings")
    client.put_object(Bucket="recordings", Key="records/a.wav", Body=b"abc")
    client.put_object(Bucket="recordings", Key="records/b.wav", Body=b"12345")
    client.put_object(Bucket="recordings", Key="other/c.wav", Body=b"zz")

    def fail_lock(path: Path) -> bool:
        raise AssertionError(path)

    monkeypatch.setattr("app.main.acquire_lock", fail_lock)
    code = main(["--count"])
    text = (tmp_path / "archiver.log").read_text(encoding="utf-8")
    assert code == 0, text
    assert "объектов=2" in text
    assert "байт=8" in text
    assert "префикс='records/'" in text
    assert "бакет=recordings" in text
    assert (records / "keep.mp3").exists()
    assert "secret-test-value" not in text
    assert "AKIATESTKEY" not in text


@mock_aws
def test_list_page_shows_requested_slice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import boto3

    point_paths(monkeypatch, tmp_path)
    fill_env(monkeypatch)
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="recordings")
    for name, body in (("a", b"aa"), ("b", b"bbb"), ("c", b"cccc"), ("d", b"d"), ("e", b"ee")):
        client.put_object(Bucket="recordings", Key=f"records/{name}.wav", Body=body)
    client.put_object(Bucket="recordings", Key="other/skip.wav", Body=b"no")

    def fail_lock(path: Path) -> bool:
        raise AssertionError(path)

    monkeypatch.setattr("app.main.acquire_lock", fail_lock)
    code = main(["--list", "--limit", "2", "--page", "2"])
    text = (tmp_path / "archiver.log").read_text(encoding="utf-8")
    assert code == 0, text
    assert "страница=2" in text
    assert "размер_страницы=2" in text
    assert "показано=2" in text
    assert "есть_далее=1" in text
    assert "key=records/c.wav" in text
    assert "key=records/d.wav" in text
    assert "key=records/a.wav" not in text
    assert "key=records/e.wav" not in text
    assert "key=other/skip.wav" not in text


@mock_aws
def test_list_defaults_and_download_saves_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import boto3

    point_paths(monkeypatch, tmp_path)
    fill_env(monkeypatch)
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="recordings")
    client.put_object(Bucket="recordings", Key="records/call.wav", Body=b"audio-bytes")
    list_code = main(["--list"])
    list_text = (tmp_path / "archiver.log").read_text(encoding="utf-8")
    assert list_code == 0, list_text
    assert "страница=1" in list_text
    assert "размер_страницы=20" in list_text
    assert "показано=1" in list_text
    assert "есть_далее=0" in list_text

    code = main(["--download", "records/call.wav"])
    text = (tmp_path / "archiver.log").read_text(encoding="utf-8")
    saved = tmp_path / "download" / "records" / "call.wav"
    assert code == 0, text
    assert saved.read_bytes() == b"audio-bytes"
    assert "файл сохранён" in text
    assert "./download/records/call.wav" in text
    assert "secret-test-value" not in text


@mock_aws
def test_download_missing_and_unsafe_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import boto3

    point_paths(monkeypatch, tmp_path)
    fill_env(monkeypatch)
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="recordings")
    assert main(["--download", "records/missing.wav"]) == 1
    text = (tmp_path / "archiver.log").read_text(encoding="utf-8")
    assert "объект не найден" in text
    assert main(["--download", "../secret.wav"]) == 1
    text = (tmp_path / "archiver.log").read_text(encoding="utf-8")
    assert "недопустимый ключ" in text
    assert not (tmp_path / "download").exists() or not any((tmp_path / "download").rglob("*"))


def test_list_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--list", "--count"]) == 1
    assert "--list" in capsys.readouterr().err
    assert main(["--list", "--page", "0"]) == 1
    assert "--page" in capsys.readouterr().err
    assert main(["--page", "2"]) == 1
    assert "--list" in capsys.readouterr().err


def test_count_error_is_scrubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    point_paths(monkeypatch, tmp_path)
    fill_env(monkeypatch)

    def fail_client(settings: object) -> None:
        raise RuntimeError("secret-test-value failed")

    monkeypatch.setattr("app.main.create_s3_client", fail_client)
    assert main(["--count"]) == 1
    text = (tmp_path / "archiver.log").read_text(encoding="utf-8")
    assert "не удалось посчитать объекты" in text
    assert "secret-test-value" not in text
    assert "***" in text


@mock_aws
def test_main_once_archives_old_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import boto3

    records = point_paths(monkeypatch, tmp_path)
    fill_env(monkeypatch)
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="recordings")
    moment = datetime.now(timezone.utc)
    old = place_file(records, "2023/a.mp3", b"archived", moment - relativedelta(months=5))
    young = place_file(records, "2026/b.mp3", b"fresh", moment)
    code = main(["--once"])
    assert code == 0, (tmp_path / "archiver.log").read_text(encoding="utf-8")
    assert not old.exists()
    assert young.exists()
    assert (records / "2023").is_dir()
    assert (tmp_path / "heartbeat").exists()
    body = boto3.client("s3", region_name="us-east-1").get_object(
        Bucket="recordings",
        Key="records/2023/a.mp3",
    )["Body"].read()
    assert body == b"archived"
    log_text = (tmp_path / "archiver.log").read_text(encoding="utf-8")
    assert "secret-test-value" not in log_text
    assert "AKIATESTKEY" not in log_text
    assert logging.getLogger("botocore").level >= logging.WARNING


def test_settings_mapping_rejects_missing_bucket() -> None:
    from app.config import settings_from_mapping

    env = {
        "S3_ENDPOINT": "https://s3.hoster.by",
        "S3_ACCESS_KEY": "AKIATESTKEY",
        "S3_SECRET_KEY": "secret-test-value",
    }
    with pytest.raises(ConfigError, match="S3_BUCKET"):
        settings_from_mapping(env)
