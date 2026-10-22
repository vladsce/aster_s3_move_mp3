"""Общие помощники тестов. Все пути лежат во временном каталоге."""

from __future__ import annotations

import logging
import os

# moto перехватывает только amazonaws.com, пока здесь не указан адрес из тестов.
# Переменная читается при первом импорте moto, поэтому задаётся до остальных импортов.
os.environ.setdefault("MOTO_S3_CUSTOM_ENDPOINTS", "https://s3.test.local")
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.config import Settings
from app.main import release_lock
from app.uploader import md5_file


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    from app import main as main_module

    main_module._stop.clear()
    main_module._signal_number = None
    logger = logging.getLogger("archiver")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield
    main_module._stop.clear()
    main_module._signal_number = None
    release_lock()
    logger = logging.getLogger("archiver")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def place_file(root: Path, relative: str, content: bytes, when: datetime) -> Path:
    """Создаёт файл и выставляет mtime."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Настройки с каталогами внутри tmp_path, без боевых путей."""
    src_dir = tmp_path / "records"
    src_dir.mkdir(parents=True, exist_ok=True)
    values: dict[str, Any] = {
        "src_dir": src_dir,
        "log_file": tmp_path / "logs" / "archiver.log",
        "heartbeat_path": tmp_path / "heartbeat",
        "lock_path": tmp_path / "lock",
        "age_months": 3,
        "check_interval_seconds": 300,
        "extensions": (".mp3",),
        "s3_endpoint": "https://s3.test.local",
        "s3_access_key": "AKIATESTKEY",
        "s3_secret_key": "secret-test-value",
        "s3_bucket": "recordings",
        "s3_region": "us-east-1",
        "s3_addressing_style": "path",
        "s3_prefix": "records/",
        "delete_after_upload": True,
        "dry_run": False,
        "log_level": "INFO",
    }
    values.update(overrides)
    return Settings(**values)


def client_error(code: str, message: str, status: int, operation: str = "HeadObject") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class FakeS3:
    """Память вместо бакета: Head Object и один Put Object."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.uploads: list[str] = []
        self.configs: list[Any] = []
        self.fail_suffixes: set[str] = set()
        self.wrong_size = False
        self.bad_etag = False

    def head_object(self, Bucket: str, Key: str, **kwargs: Any) -> dict[str, Any]:
        if Key not in self.objects:
            raise client_error("404", "Not Found", 404)
        return self.objects[Key]

    def upload_file(self, Filename: str, Bucket: str, Key: str, **kwargs: Any) -> None:
        if any(Key.endswith(suffix) for suffix in self.fail_suffixes):
            raise client_error("InternalError", "boom", 500, "PutObject")
        self.uploads.append(Key)
        self.configs.append(kwargs.get("Config"))
        path = Path(Filename)
        size = path.stat().st_size
        digest = md5_file(path)
        if self.wrong_size:
            size += 1
        if self.bad_etag:
            digest = "0" * 32
        self.objects[Key] = {"ContentLength": size, "ETag": f'"{digest}"'}
