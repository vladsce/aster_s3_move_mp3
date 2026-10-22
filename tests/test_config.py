"""Проверка настроек."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import (
    LOG_FILE,
    SRC_DIR,
    ConfigError,
    heartbeat_is_fresh,
    normalize_prefix,
    settings_from_mapping,
)


def valid_env() -> dict[str, str]:
    return {
        "S3_ENDPOINT": "https://s3.hoster.by",
        "S3_ACCESS_KEY": "AKIATESTKEY",
        "S3_SECRET_KEY": "secret-test-value",
        "S3_BUCKET": "recordings",
    }


def test_zero_age_months_is_allowed() -> None:
    env = valid_env()
    env["AGE_MONTHS"] = "0"
    settings = settings_from_mapping(env)
    assert settings.age_months == 0


def test_defaults_and_secret_are_not_in_repr() -> None:
    settings = settings_from_mapping(valid_env())
    assert settings.src_dir == SRC_DIR
    assert settings.src_dir == Path("/data/records")
    assert settings.log_file == LOG_FILE
    assert settings.age_months == 3
    assert settings.check_interval_seconds == 300
    assert settings.extensions == (".mp3",)
    assert settings.s3_region == "us-east-1"
    assert settings.s3_addressing_style == "path"
    assert settings.s3_prefix == ""
    assert settings.delete_after_upload is True
    assert settings.dry_run is False
    assert settings.log_level == "INFO"
    assert "secret-test-value" not in settings.safe_summary()
    assert "AKIATESTKEY" not in repr(settings)
    assert "secret-test-value" not in repr(settings)


def test_normalizes_prefix_extensions_and_endpoint() -> None:
    env = valid_env()
    env.update(
        {
            "S3_ENDPOINT": "https://s3.hoster.by/",
            "S3_PREFIX": "/records/2024",
            "FILE_EXTENSIONS": ".MP3, wav",
            "LOG_LEVEL": "debug",
            "S3_ADDRESSING_STYLE": "virtual",
            "DELETE_AFTER_UPLOAD": "false",
            "DRY_RUN": "yes",
        }
    )
    settings = settings_from_mapping(env)
    assert settings.s3_endpoint == "https://s3.hoster.by"
    assert settings.s3_prefix == "records/2024/"
    assert settings.extensions == (".mp3", ".wav")
    assert settings.log_level == "DEBUG"
    assert settings.s3_addressing_style == "virtual"
    assert settings.delete_after_upload is False
    assert settings.dry_run is True


def test_reports_all_invalid_values() -> None:
    env = valid_env()
    env.update(
        {
            "S3_ENDPOINT": "s3.hoster.by",
            "S3_ADDRESSING_STYLE": "auto",
            "AGE_MONTHS": "abc",
            "CHECK_INTERVAL_SECONDS": "0",
            "DELETE_AFTER_UPLOAD": "maybe",
            "FILE_EXTENSIONS": ",",
            "S3_PREFIX": "../secret",
            "LOG_LEVEL": "verbose",
        }
    )
    with pytest.raises(ConfigError) as caught:
        settings_from_mapping(env)
    text = str(caught.value)
    for name in (
        "S3_ENDPOINT",
        "S3_ADDRESSING_STYLE",
        "AGE_MONTHS",
        "CHECK_INTERVAL_SECONDS",
        "DELETE_AFTER_UPLOAD",
        "FILE_EXTENSIONS",
        "S3_PREFIX",
        "LOG_LEVEL",
    ):
        assert name in text


def test_requires_credentials_without_echoing_them() -> None:
    env = valid_env()
    del env["S3_SECRET_KEY"]
    env["S3_ACCESS_KEY"] = ""
    with pytest.raises(ConfigError) as caught:
        settings_from_mapping(env)
    text = str(caught.value)
    assert "S3_SECRET_KEY" in text
    assert "S3_ACCESS_KEY" in text
    assert "secret-test-value" not in text


def test_rejects_credentials_inside_endpoint() -> None:
    env = valid_env()
    env["S3_ENDPOINT"] = "https://user:password@s3.hoster.by"
    with pytest.raises(ConfigError, match="S3_ENDPOINT"):
        settings_from_mapping(env)


def test_prefix_helpers() -> None:
    assert normalize_prefix("") == ""
    assert normalize_prefix("records") == "records/"
    assert normalize_prefix("records/") == "records/"
    with pytest.raises(ValueError):
        normalize_prefix("records/../x")


def test_heartbeat_is_fresh_only_inside_three_intervals(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    stamp = 1_700_000_000
    assert heartbeat_is_fresh(path, interval_seconds=300, now=stamp) is False
    path.write_text("ok", encoding="utf-8")
    os.utime(path, (stamp, stamp))
    assert heartbeat_is_fresh(path, interval_seconds=300, now=stamp + 899) is True
    assert heartbeat_is_fresh(path, interval_seconds=300, now=stamp + 900) is False
    assert heartbeat_is_fresh(path, interval_seconds=0, now=stamp) is False
