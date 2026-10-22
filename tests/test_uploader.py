"""Загрузка, проверка объекта и решение об удалении локального файла."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from dateutil.relativedelta import relativedelta
from moto import mock_aws

from app.main import run_pass, setup_logging
from app.uploader import (
    SINGLE_PUT_LIMIT,
    build_object_key,
    create_s3_client,
    etag_matches_md5,
    process_file,
)
from tests.conftest import FakeS3, make_settings, place_file

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def _old(root: Path, relative: str, content: bytes = b"audio-data") -> Path:
    return place_file(root, relative, content, NOW - relativedelta(months=4))


def test_object_key_keeps_folder_structure(tmp_path: Path) -> None:
    src = tmp_path / "records"
    path = src / "2023" / "01" / "call 1.mp3"
    assert build_object_key(src, path, "records/") == "records/2023/01/call 1.mp3"
    assert build_object_key(src, path, "") == "2023/01/call 1.mp3"


def test_etag_accepts_quoted_md5_and_rejects_multipart() -> None:
    assert etag_matches_md5('"ABCDEF"', "abcdef") is True
    assert etag_matches_md5("abcdef-2", "abcdef") is False
    assert etag_matches_md5("", "abcdef") is False


def test_client_config_matches_s3_compatible_storage(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, s3_addressing_style="virtual")
    client = create_s3_client(settings)
    config = client.meta.config
    assert config.signature_version == "s3v4"
    assert config.s3["addressing_style"] == "virtual"
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"
    retries = config.retries
    # max_attempts=5 — пять повторов после первого запроса, всего шесть попыток.
    assert retries["mode"] == "standard"
    assert retries["total_max_attempts"] == 6
    assert os_env_checksums()


def os_env_checksums() -> bool:
    import os

    return (
        os.environ["AWS_REQUEST_CHECKSUM_CALCULATION"] == "when_required"
        and os.environ["AWS_RESPONSE_CHECKSUM_VALIDATION"] == "when_required"
    )


def test_upload_uses_single_put_and_deletes_after_verification(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path = _old(settings.src_dir, "2023/01/call.mp3", b"hello-mp3")
    client = FakeS3()
    outcome = process_file(settings, client, path, now=NOW)
    assert outcome.status == "uploaded"
    assert outcome.deleted is True
    assert not path.exists()
    assert client.uploads == ["records/2023/01/call.mp3"]
    config = client.configs[0]
    assert config.multipart_threshold == SINGLE_PUT_LIMIT
    assert config.use_threads is False
    stored = client.objects["records/2023/01/call.mp3"]
    assert stored["ContentLength"] == len(b"hello-mp3")


def test_size_mismatch_keeps_local_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path = _old(settings.src_dir, "a.mp3", b"12345")
    client = FakeS3()
    client.wrong_size = True
    outcome = process_file(settings, client, path, now=NOW)
    assert outcome.status == "error"
    assert outcome.deleted is False
    assert path.read_bytes() == b"12345"


def test_etag_mismatch_keeps_local_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path = _old(settings.src_dir, "a.mp3", b"12345")
    client = FakeS3()
    client.bad_etag = True
    outcome = process_file(settings, client, path, now=NOW)
    assert outcome.status == "error"
    assert path.exists()


def test_upload_error_keeps_local_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path = _old(settings.src_dir, "bad.mp3", b"12345")
    client = FakeS3()
    client.fail_suffixes.add("bad.mp3")
    outcome = process_file(settings, client, path, now=NOW)
    assert outcome.status == "error"
    assert outcome.deleted is False
    assert client.uploads == []
    assert path.exists()


def test_existing_object_with_same_size_is_not_uploaded_again(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, delete_after_upload=False)
    path = _old(settings.src_dir, "keep.mp3", b"same-bytes")
    client = FakeS3()
    first = run_pass(settings, client, now=NOW)
    second = run_pass(settings, client, now=NOW)
    assert first.uploaded == 1
    assert first.deleted == 0
    assert second.uploaded == 0
    assert second.skipped == 1
    assert second.deleted == 0
    assert client.uploads == ["records/keep.mp3"]
    assert path.exists()


def test_delete_disabled_and_dry_run_keep_files(tmp_path: Path) -> None:
    content = b"stay"
    kept = make_settings(tmp_path / "kept", delete_after_upload=False)
    kept_path = _old(kept.src_dir, "a.mp3", content)
    kept_client = FakeS3()
    kept_stats = run_pass(kept, kept_client, now=NOW)
    assert kept_stats.uploaded == 1
    assert kept_stats.deleted == 0
    assert kept_path.read_bytes() == content

    dry = make_settings(tmp_path / "dry", dry_run=True, delete_after_upload=True)
    dry_path = _old(dry.src_dir, "nested/b.mp3", content)
    dry_client = FakeS3()
    dry_stats = run_pass(dry, dry_client, now=NOW)
    assert dry_stats.uploaded == 0
    assert dry_stats.deleted == 0
    assert dry_stats.bytes_planned == len(content)
    assert dry_client.uploads == []
    assert dry_path.exists()
    assert "nested/b.mp3" not in dry_client.objects


def test_one_file_error_does_not_stop_the_pass(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, s3_prefix="")
    bad = _old(settings.src_dir, "bad.mp3", b"bad")
    good = _old(settings.src_dir, "good.mp3", b"good-audio")
    client = FakeS3()
    client.fail_suffixes.add("bad.mp3")
    stats = run_pass(settings, client, now=NOW)
    assert stats.found == 2
    assert stats.uploaded == 1
    assert stats.deleted == 1
    assert stats.errors == 1
    assert stats.bytes_uploaded == len(b"good-audio")
    assert bad.exists()
    assert not good.exists()
    assert client.uploads == ["good.mp3"]


def test_oversized_file_is_not_sent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.uploader.SINGLE_PUT_LIMIT", 4)
    settings = make_settings(tmp_path)
    path = _old(settings.src_dir, "big.mp3", b"12345")
    client = FakeS3()
    outcome = process_file(settings, client, path, now=NOW)
    assert outcome.status == "error"
    assert client.uploads == []
    assert path.exists()


def test_library_loggers_stay_quiet_and_file_rotates(tmp_path: Path) -> None:
    setup_logging(tmp_path / "archiver.log", "DEBUG")
    assert logging.getLogger("archiver").level == logging.DEBUG
    assert logging.getLogger("botocore").level == logging.WARNING
    assert logging.getLogger("boto3").level == logging.WARNING
    assert logging.getLogger("urllib3").level == logging.WARNING
    handlers = [
        handler
        for handler in logging.getLogger("archiver").handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert handlers[0].maxBytes == 10 * 1024 * 1024
    assert handlers[0].backupCount == 5


@mock_aws
def test_moto_stores_object_and_removes_local_file(tmp_path: Path) -> None:
    import boto3

    settings = make_settings(tmp_path, s3_prefix="records/")
    setup_logging(settings.log_file, "INFO")
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=settings.s3_bucket)
    content = b"ID3-real-bytes"
    path = _old(settings.src_dir, "2023/jan/call 1.mp3", content)
    fresh = place_file(
        settings.src_dir,
        "2026/fresh.mp3",
        b"new",
        NOW - relativedelta(days=1),
    )
    client = create_s3_client(settings)
    stats = run_pass(settings, client, now=NOW)
    assert stats.errors == 0
    assert stats.uploaded == 1
    assert stats.deleted == 1
    assert not path.exists()
    assert (settings.src_dir / "2023" / "jan").is_dir()
    assert fresh.exists()
    assert settings.src_dir.exists()
    key = "records/2023/jan/call 1.mp3"
    head = client.head_object(Bucket=settings.s3_bucket, Key=key)
    assert head["ContentLength"] == len(content)
    assert settings.heartbeat_path.exists()
    log_text = settings.log_file.read_text(encoding="utf-8")
    assert "итог прохода:" in log_text
    assert "найдено=1" in log_text
    assert "загружено=1" in log_text
    assert "удалено=1" in log_text
    assert "байт=" in log_text
    assert f"path={path}" in log_text
    assert f"key={key}" in log_text
    assert f"size={len(content)}" in log_text
    assert settings.s3_secret_key not in log_text
    assert settings.s3_access_key not in log_text

    again = run_pass(settings, client, now=NOW)
    assert again.uploaded == 0
    assert again.found == 0
    assert fresh.exists()
