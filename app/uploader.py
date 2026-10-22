"""Загрузка одного файла в S3 и проверка, что объект записан целиком."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings, log_event
from app.scanner import is_old_enough

# Хранилище hoster.by принимает Put Object и не принимает составную загрузку.
# Порог равен лимиту одного Put Object, файлы этого размера и больше сервис не отправляет.
SINGLE_PUT_LIMIT = 5 * 1024**3
_TRANSFER = TransferConfig(
    multipart_threshold=SINGLE_PUT_LIMIT,
    use_threads=False,
)


class VerificationError(Exception):
    """Объект в бакете не совпал с локальным файлом."""


@dataclass(frozen=True)
class FileOutcome:
    """Результат обработки одного файла."""

    status: str
    deleted: bool
    size: int
    delete_failed: bool = False
    planned_upload: bool = False


def create_s3_client(settings: Settings) -> Any:
    """Клиент S3 без дополнительных контрольных сумм, которые отклоняет hoster.by.

    Новые версии boto3 по умолчанию добавляют заголовки checksum. Режим
    `when_required` оставляет подпись s3v4 и не отправляет эти заголовки.
    Адресация берётся из S3_ADDRESSING_STYLE.
    """
    os.environ["AWS_REQUEST_CHECKSUM_CALCULATION"] = "when_required"
    os.environ["AWS_RESPONSE_CHECKSUM_VALIDATION"] = "when_required"
    config = Config(
        signature_version="s3v4",
        connect_timeout=10,
        read_timeout=300,
        retries={"max_attempts": 5, "mode": "standard"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        s3={"addressing_style": settings.s3_addressing_style},
    )
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=config,
    )


def build_object_key(src_dir: Path, file_path: Path, prefix: str) -> str:
    """Ключ объекта: префикс и путь относительно каталога записей, разделитель `/`."""
    relative = file_path.relative_to(src_dir).as_posix()
    if relative == ".." or relative.startswith("../") or relative.startswith("/"):
        raise ValueError(f"Файл вне каталога записей: {file_path}")
    return f"{prefix}{relative}"


def md5_file(path: Path) -> str:
    """MD5 файла, чтение порциями без загрузки целиком в память."""
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def etag_matches_md5(etag: str, digest: str) -> bool:
    """Сравнивает ETag одиночной загрузки с MD5. Составной ETag с суффиксом `-N` не подходит."""
    cleaned = etag.strip().strip('"').lower()
    if not cleaned or "-" in cleaned:
        return False
    return cleaned == digest.lower()


def safe_error(exc: BaseException, settings: Settings | None = None) -> str:
    """Короткий текст ошибки без заголовков запроса и без ключей доступа."""
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = error.get("Code", "ClientError")
        message = error.get("Message", "")
        text = f"{code}: {message}".strip()
    else:
        text = f"{type(exc).__name__}: {exc}"
    if settings is not None:
        text = settings.scrub(text)
    return text


def is_not_found(exc: ClientError) -> bool:
    """Head Object не нашёл ключ."""
    error = exc.response.get("Error", {})
    code = str(error.get("Code", ""))
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def count_objects(client: Any, bucket: str, prefix: str) -> tuple[int, int]:
    """Число объектов и их суммарный размер. Пустой префикс считает весь бакет."""
    count = 0
    total = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            count += 1
            total += int(obj.get("Size", 0))
    return count, total


def list_object_page(
    client: Any,
    bucket: str,
    prefix: str,
    *,
    limit: int,
    page: int,
) -> tuple[list[tuple[str, int]], bool]:
    """Страница объектов по префиксу и признак, что следующая страница не пустая."""
    skip = (page - 1) * limit
    selected: list[tuple[str, int]] = []
    seen = 0
    paginator = client.get_paginator("list_objects_v2")
    for batch in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in batch.get("Contents", []):
            if seen < skip:
                seen += 1
                continue
            if len(selected) >= limit:
                return selected, True
            selected.append((str(obj["Key"]), int(obj.get("Size", 0))))
            seen += 1
    return selected, False


def object_download_path(root: Path, key: str) -> Path:
    """Путь внутри каталога скачивания. Ключ с `..` или пустой ключ отклоняется."""
    cleaned = key.strip().replace("\\", "/").lstrip("/")
    parts = [part for part in cleaned.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"недопустимый ключ: {key}")
    return root.joinpath(*parts)


def download_object(client: Any, bucket: str, key: str, root: Path) -> tuple[Path, int]:
    """Скачивает объект одним GetObject во временный файл и заменяет им назначение."""
    destination = object_download_path(root, key)
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if is_not_found(exc):
            raise FileNotFoundError(key) from exc
        raise
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    expected = int(response.get("ContentLength", -1))
    size = 0
    try:
        with temporary.open("wb") as handle:
            body = response["Body"]
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                size += len(chunk)
        if expected >= 0 and size != expected:
            raise VerificationError(
                f"скачанный размер {size} не совпал с размером объекта {expected}"
            )
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination, size


def head_if_exists(client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    """Возвращает ответ Head Object или None, если ключа нет."""
    try:
        response = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if is_not_found(exc):
            return None
        raise
    return response


def upload_single(client: Any, path: Path, bucket: str, key: str) -> None:
    """Отправляет файл одним запросом Put Object."""
    client.upload_file(
        str(path),
        bucket,
        key,
        Config=_TRANSFER,
    )


def verify_upload(client: Any, bucket: str, key: str, path: Path, size: int) -> None:
    """Сверяет ContentLength и ETag одиночной загрузки с локальным файлом."""
    head = client.head_object(Bucket=bucket, Key=key)
    remote_size = int(head["ContentLength"])
    if remote_size != size:
        raise VerificationError(
            f"размер объекта {remote_size} не совпал с размером файла {size}"
        )
    digest = md5_file(path)
    etag = str(head.get("ETag", ""))
    if not etag_matches_md5(etag, digest):
        raise VerificationError(f"ETag {etag} не совпал с MD5 {digest}")


def process_file(
    settings: Settings,
    client: Any,
    path: Path,
    *,
    now: datetime,
) -> FileOutcome:
    """Загружает файл, проверяет объект и удаляет локальную копию только после проверки."""
    if path.is_symlink():
        log_event(logging.INFO, "символическая ссылка пропущена", path=path)
        return FileOutcome("skipped", deleted=False, size=0)

    try:
        stat_result = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        log_event(logging.INFO, "файл уже отсутствует", path=path)
        return FileOutcome("skipped", deleted=False, size=0)
    except OSError as exc:
        log_event(logging.ERROR, f"не удалось прочитать файл: {exc}", path=path)
        return FileOutcome("error", deleted=False, size=0)

    size = stat_result.st_size
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    mtime = datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc)
    if not is_old_enough(mtime, moment, settings.age_months):
        log_event(
            logging.INFO,
            "файл изменился после сканирования, пропуск",
            path=path,
            size=size,
        )
        return FileOutcome("skipped", deleted=False, size=size)

    if size >= SINGLE_PUT_LIMIT:
        log_event(
            logging.ERROR,
            "файл не меньше 5 ГиБ, хранилище не принимает составную загрузку",
            path=path,
            size=size,
        )
        return FileOutcome("error", deleted=False, size=size)

    try:
        key = build_object_key(settings.src_dir, path, settings.s3_prefix)
    except ValueError as exc:
        log_event(logging.ERROR, str(exc), path=path, size=size)
        return FileOutcome("error", deleted=False, size=size)

    try:
        existing = head_if_exists(client, settings.s3_bucket, key)
    except (ClientError, BotoCoreError) as exc:
        log_event(
            logging.ERROR,
            f"не удалось проверить объект: {safe_error(exc, settings)}",
            path=path,
            key=key,
            size=size,
        )
        return FileOutcome("error", deleted=False, size=size)

    remote_size = _content_length(existing)
    already = existing is not None and remote_size == size

    if settings.dry_run:
        return _dry_run(settings, path, key, size, already)

    uploaded = False
    if not already:
        try:
            upload_single(client, path, settings.s3_bucket, key)
            verify_upload(client, settings.s3_bucket, key, path, size)
        except (ClientError, BotoCoreError, VerificationError, OSError) as exc:
            log_event(
                logging.ERROR,
                f"загрузка не подтверждена, локальный файл сохранён: {safe_error(exc, settings)}",
                path=path,
                key=key,
                size=size,
            )
            return FileOutcome("error", deleted=False, size=size)
        uploaded = True
        log_event(logging.INFO, "файл загружен и проверен", path=path, key=key, size=size)
    else:
        log_event(
            logging.INFO,
            "объект уже есть и размер совпадает, повторная загрузка не нужна",
            path=path,
            key=key,
            size=size,
        )

    deleted, delete_failed = _delete_local(settings, path, key, size)
    status = "uploaded" if uploaded else "skipped"
    return FileOutcome(status, deleted=deleted, size=size, delete_failed=delete_failed)


def _content_length(head: dict[str, Any] | None) -> int | None:
    if not head or "ContentLength" not in head:
        return None
    return int(head["ContentLength"])


def _dry_run(
    settings: Settings,
    path: Path,
    key: str,
    size: int,
    already: bool,
) -> FileOutcome:
    if already and settings.delete_after_upload:
        message = "dry-run: объект уже есть, локальный файл был бы удалён"
        planned = False
    elif already:
        message = "dry-run: объект уже есть, локальный файл был бы сохранён"
        planned = False
    elif settings.delete_after_upload:
        message = "dry-run: файл был бы загружен и удалён локально"
        planned = True
    else:
        message = "dry-run: файл был бы загружен, локальный файл был бы сохранён"
        planned = True
    log_event(logging.INFO, message, path=path, key=key, size=size)
    return FileOutcome("skipped", deleted=False, size=size, planned_upload=planned)


def _delete_local(
    settings: Settings,
    path: Path,
    key: str,
    size: int,
) -> tuple[bool, bool]:
    if not settings.delete_after_upload:
        log_event(
            logging.INFO,
            "удаление выключено, локальный файл сохранён",
            path=path,
            key=key,
            size=size,
        )
        return False, False
    if path.is_symlink():
        log_event(
            logging.ERROR,
            "на месте файла появилась символическая ссылка, удаление отменено",
            path=path,
            key=key,
            size=size,
        )
        return False, True
    try:
        current = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        log_event(logging.INFO, "локальный файл уже отсутствует", path=path, key=key, size=size)
        return False, False
    except OSError as exc:
        log_event(
            logging.ERROR,
            f"не удалось проверить файл перед удалением: {exc}",
            path=path,
            key=key,
            size=size,
        )
        return False, True
    if current.st_size != size:
        log_event(
            logging.ERROR,
            "размер файла изменился после проверки, удаление отменено",
            path=path,
            key=key,
            size=size,
        )
        return False, True
    try:
        path.unlink()
    except OSError as exc:
        log_event(
            logging.ERROR,
            f"не удалось удалить локальный файл: {exc}",
            path=path,
            key=key,
            size=size,
        )
        return False, True
    log_event(logging.INFO, "локальный файл удалён", path=path, key=key, size=size)
    return True, False
