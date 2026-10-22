"""Чтение и проверка настроек сервиса."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

SRC_DIR = Path("/data/records")
DOWNLOAD_DIR = Path("/data/download")
LOG_FILE = Path("/var/log/archiver/archiver.log")
HEARTBEAT_PATH = Path("/tmp/heartbeat")
LOCK_PATH = Path("/var/log/archiver/.lock")

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_ADDRESSING = {"path", "virtual"}


class ConfigError(Exception):
    """Настройки отсутствуют или заданы неверно."""


@dataclass(frozen=True, repr=False)
class Settings:
    """Проверенные настройки одного запуска."""

    src_dir: Path
    log_file: Path
    heartbeat_path: Path
    lock_path: Path
    age_months: int
    check_interval_seconds: int
    extensions: tuple[str, ...]
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    s3_region: str
    s3_addressing_style: str
    s3_prefix: str
    delete_after_upload: bool
    dry_run: bool
    log_level: str
    download_dir: Path = Path("/data/download")

    def safe_summary(self) -> str:
        """Описание настроек для журнала без ключей доступа."""
        return (
            f"src={self.src_dir} bucket={self.s3_bucket} endpoint={self.s3_endpoint} "
            f"region={self.s3_region} addressing={self.s3_addressing_style} "
            f"prefix={self.s3_prefix!r} age_months={self.age_months} "
            f"interval={self.check_interval_seconds}s extensions={','.join(self.extensions)} "
            f"delete_after_upload={self.delete_after_upload} dry_run={self.dry_run} "
            f"log_level={self.log_level} download={self.download_dir}"
        )

    def scrub(self, text: str) -> str:
        """Убирает ключи доступа из текста ошибки."""
        redacted = text
        for secret in (self.s3_secret_key, self.s3_access_key):
            if secret and len(secret) >= 8:
                redacted = redacted.replace(secret, "***")
        return redacted

    def __repr__(self) -> str:
        return f"Settings({self.safe_summary()})"


def log_event(
    level: int,
    message: str,
    *,
    path: object = "-",
    key: object = "-",
    size: object = "-",
) -> None:
    """Пишет строку журнала: время и уровень добавляет форматтер, здесь путь, ключ и размер."""
    logging.getLogger("archiver").log(
        level,
        message,
        extra={"file_path": path, "s3_key": key, "size": size},
    )


def load_settings() -> Settings:
    """Загружает файл `.env`, если он есть, и проверяет переменные окружения."""
    load_dotenv(override=False)
    return settings_from_mapping(os.environ)


def settings_from_mapping(env: Mapping[str, str]) -> Settings:
    """Собирает настройки из словаря и перечисляет все найденные ошибки."""
    errors: list[str] = []

    def text(name: str) -> str | None:
        value = env.get(name)
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None

    def require(name: str) -> str:
        value = text(name)
        if value is None:
            errors.append(f"{name}: обязательная переменная не задана")
            return ""
        return value

    def integer(name: str, default: int, minimum: int) -> int:
        value = text(name)
        if value is None:
            return default
        try:
            parsed = int(value)
        except ValueError:
            errors.append(f"{name}: ожидается целое число, получено {value!r}")
            return default
        if parsed < minimum:
            errors.append(f"{name}: значение должно быть не меньше {minimum}, получено {parsed}")
        return parsed

    def boolean(name: str, default: bool) -> bool:
        value = text(name)
        if value is None:
            return default
        lowered = value.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        errors.append(f"{name}: ожидается true или false, получено {value!r}")
        return default

    endpoint = require("S3_ENDPOINT")
    access_key = require("S3_ACCESS_KEY")
    secret_key = require("S3_SECRET_KEY")
    bucket = require("S3_BUCKET")
    if endpoint and not (endpoint.startswith("https://") or endpoint.startswith("http://")):
        errors.append("S3_ENDPOINT: адрес должен начинаться с https:// или http://")
    if endpoint:
        parsed = urlparse(endpoint)
        if parsed.username or parsed.password:
            errors.append(
                "S3_ENDPOINT: укажите адрес без логина и пароля. "
                "Ключи задаются в S3_ACCESS_KEY и S3_SECRET_KEY"
            )
        endpoint = endpoint.rstrip("/")

    region = text("S3_REGION") or "us-east-1"
    style = (text("S3_ADDRESSING_STYLE") or "path").lower()
    if style not in _ADDRESSING:
        errors.append("S3_ADDRESSING_STYLE: допустимы значения path или virtual")

    try:
        prefix = normalize_prefix(text("S3_PREFIX") or "")
    except ValueError as exc:
        errors.append(str(exc))
        prefix = ""

    level = (text("LOG_LEVEL") or "INFO").upper()
    if level not in _LEVELS:
        errors.append("LOG_LEVEL: допустимы DEBUG, INFO, WARNING, ERROR, CRITICAL")

    extensions = parse_extensions(text("FILE_EXTENSIONS") or ".mp3", errors)
    age_months = integer("AGE_MONTHS", 3, 0)
    interval = integer("CHECK_INTERVAL_SECONDS", 300, 1)
    delete_after_upload = boolean("DELETE_AFTER_UPLOAD", True)
    dry_run = boolean("DRY_RUN", False)

    if errors:
        raise ConfigError("\n".join(errors))

    return Settings(
        src_dir=SRC_DIR,
        log_file=LOG_FILE,
        heartbeat_path=HEARTBEAT_PATH,
        lock_path=LOCK_PATH,
        age_months=age_months,
        check_interval_seconds=interval,
        extensions=extensions,
        s3_endpoint=endpoint,
        s3_access_key=access_key,
        s3_secret_key=secret_key,
        s3_bucket=bucket,
        s3_region=region,
        s3_addressing_style=style,
        s3_prefix=prefix,
        delete_after_upload=delete_after_upload,
        dry_run=dry_run,
        log_level=level,
        download_dir=DOWNLOAD_DIR,
    )


def normalize_prefix(prefix: str) -> str:
    """Приводит префикс ключей к виду `records/` без ведущего слэша и без `..`."""
    cleaned = prefix.strip().replace("\\", "/").lstrip("/")
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("S3_PREFIX: не должен содержать '..'")
    if not parts:
        return ""
    return "/".join(parts) + "/"


def parse_extensions(value: str, errors: list[str]) -> tuple[str, ...]:
    """Разбирает список расширений через запятую."""
    items: list[str] = []
    for part in value.split(","):
        extension = part.strip().lower()
        if not extension:
            continue
        if not extension.startswith("."):
            extension = f".{extension}"
        if len(extension) < 2 or "/" in extension or "\\" in extension:
            errors.append(f"FILE_EXTENSIONS: некорректное расширение {part!r}")
            continue
        items.append(extension)
    if not items:
        errors.append("FILE_EXTENSIONS: укажите хотя бы одно расширение, например .mp3")
    return tuple(dict.fromkeys(items))


def heartbeat_is_fresh(
    path: Path | None = None,
    *,
    interval_seconds: int | None = None,
    now: float | None = None,
) -> bool:
    """True, если файл heartbeat свежее, чем 3 × CHECK_INTERVAL_SECONDS."""
    heartbeat = HEARTBEAT_PATH if path is None else path
    if interval_seconds is None:
        raw = os.environ.get("CHECK_INTERVAL_SECONDS", "300").strip() or "300"
        try:
            interval_seconds = int(raw)
        except ValueError:
            return False
    if interval_seconds < 1 or not heartbeat.is_file():
        return False
    moment = time.time() if now is None else now
    return moment - heartbeat.stat().st_mtime < 3 * interval_seconds
