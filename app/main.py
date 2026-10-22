"""Цикл архивации: проход по каталогу, пауза, остановка по сигналу."""

from __future__ import annotations

import argparse
import fcntl
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Any

from app.config import ConfigError, Settings, load_settings, log_event
from app.scanner import find_candidates
from app.uploader import (
    count_objects,
    create_s3_client,
    download_object,
    list_object_page,
    process_file,
    safe_error,
)

_stop = threading.Event()
_signal_number: int | None = None
_lock_handle: IO[str] | None = None


@dataclass
class PassStats:
    """Счётчики одного прохода."""

    found: int = 0
    uploaded: int = 0
    skipped: int = 0
    deleted: int = 0
    errors: int = 0
    bytes_uploaded: int = 0
    bytes_planned: int = 0


def _close_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def setup_logging(log_file: Path, level: str) -> None:
    """Журнал в файл с ротацией 10 МБ × 5 и копия в stdout."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("archiver")
    _close_handlers(logger)
    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s path=%(file_path)s key=%(s3_key)s size=%(size)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        defaults={"file_path": "-", "s3_key": "-", "size": "-"},
    )
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    for name in ("boto3", "botocore", "urllib3", "s3transfer"):
        logging.getLogger(name).setLevel(logging.WARNING)


def acquire_lock(path: Path) -> bool:
    """Эксклюзивная блокировка, чтобы два прохода в контейнере не шли одновременно."""
    global _lock_handle
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    _lock_handle = handle
    return True


def release_lock() -> None:
    """Снимает блокировку. Нужно тестам; рабочий процесс отпускает её при выходе."""
    global _lock_handle
    if _lock_handle is None:
        return
    try:
        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    _lock_handle.close()
    _lock_handle = None


def touch_heartbeat(path: Path) -> None:
    """Обновляет метку времени живого процесса."""
    path.write_text(f"{time.time()}\n", encoding="utf-8")


def register_signals() -> list[tuple[int, Any]]:
    """SIGTERM и SIGINT завершают цикл после текущего файла."""
    if threading.current_thread() is not threading.main_thread():
        return []
    saved: list[tuple[int, Any]] = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        saved.append((signum, signal.getsignal(signum)))
        signal.signal(signum, _handle_signal)
    return saved


def restore_signals(saved: list[tuple[int, Any]]) -> None:
    """Возвращает прежние обработчики сигналов."""
    for signum, handler in saved:
        signal.signal(signum, handler)


def _handle_signal(signum: int, _frame: object) -> None:
    global _signal_number
    _signal_number = signum
    _stop.set()


def sleep_until_stop(seconds: int) -> None:
    """Пауза, которая заканчивается раньше, если пришёл сигнал остановки."""
    deadline = time.monotonic() + seconds
    while not _stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def run_pass(
    settings: Settings,
    client: Any,
    *,
    now: datetime | None = None,
) -> PassStats:
    """Один проход: найти, загрузить, проверить, удалить локальные копии, записать итог."""
    moment = now if now is not None else datetime.now(timezone.utc)
    stats = PassStats()
    try:
        _execute_pass(settings, client, moment, stats)
    finally:
        finish_pass(settings, stats)
    return stats


def _execute_pass(
    settings: Settings,
    client: Any,
    moment: datetime,
    stats: PassStats,
) -> None:
    try:
        candidates = find_candidates(
            settings.src_dir,
            extensions=set(settings.extensions),
            age_months=settings.age_months,
            now=moment,
        )
    except OSError as exc:
        log_event(logging.ERROR, f"каталог записей недоступен: {exc}")
        stats.errors += 1
        return

    stats.found = len(candidates)
    log_event(logging.INFO, f"найдено файлов: {stats.found}")
    for candidate in candidates:
        if _stop.is_set():
            log_event(
                logging.INFO,
                f"получен сигнал {_signal_number}, оставшиеся файлы ждут следующего запуска",
            )
            break
        try:
            outcome = process_file(settings, client, candidate.path, now=moment)
        except Exception as exc:
            stats.errors += 1
            log_event(
                logging.ERROR,
                f"ошибка обработки: {safe_error(exc, settings)}",
                path=candidate.path,
                size=candidate.size,
            )
            continue
        if outcome.status == "uploaded":
            stats.uploaded += 1
            stats.bytes_uploaded += outcome.size
        elif outcome.status == "skipped":
            stats.skipped += 1
        elif outcome.status == "error":
            stats.errors += 1
        if outcome.deleted:
            stats.deleted += 1
        if outcome.delete_failed:
            stats.errors += 1
        if outcome.planned_upload:
            stats.bytes_planned += outcome.size


def finish_pass(settings: Settings, stats: PassStats) -> None:
    """Итог прохода и обновление heartbeat."""
    message = (
        "итог прохода: "
        f"найдено={stats.found} загружено={stats.uploaded} пропущено={stats.skipped} "
        f"удалено={stats.deleted} ошибок={stats.errors} байт={stats.bytes_uploaded}"
    )
    if settings.dry_run:
        message += f" dry-run=1 к_загрузке_байт={stats.bytes_planned}"
    if _stop.is_set():
        message += " остановлено=1"
    log_event(logging.INFO, message)
    try:
        touch_heartbeat(settings.heartbeat_path)
    except OSError as exc:
        log_event(logging.ERROR, f"не удалось обновить heartbeat: {exc}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.main",
        description="Перенос старых записей в S3",
    )
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только журнал, без загрузки и без удаления",
    )
    parser.add_argument(
        "--count",
        action="store_true",
        help="посчитать объекты в бакете по S3_PREFIX и выйти",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="показать страницу объектов по S3_PREFIX и выйти",
    )
    parser.add_argument("--page", type=int, default=None, help="номер страницы списка, с 1")
    parser.add_argument("--limit", type=int, default=None, help="сколько объектов на странице")
    parser.add_argument(
        "--download",
        metavar="KEY",
        help="скачать объект в каталог download и выйти",
    )
    return parser.parse_args(argv)


def validate_cli(args: argparse.Namespace) -> str | None:
    """Проверяет, что разовая команда одна и страница списка задана корректно."""
    chosen = [
        name
        for name, enabled in (
            ("--count", args.count),
            ("--list", args.list),
            ("--download", bool(args.download)),
            ("--once", args.once),
        )
        if enabled
    ]
    lookups = [name for name in chosen if name != "--once"]
    if len(lookups) > 1 or (args.once and lookups):
        return "Укажите одну команду: обычный запуск, --once, --count, --list или --download"
    if args.dry_run and lookups:
        return "--dry-run используется для прохода по локальным файлам"
    if (args.page is not None or args.limit is not None) and not args.list:
        return "--page и --limit задаются вместе с --list"
    if not args.list:
        return None
    page = 1 if args.page is None else args.page
    limit = 20 if args.limit is None else args.limit
    if page < 1 or limit < 1:
        return "--page и --limit должны быть целыми числами от 1"
    args.page = page
    args.limit = limit
    return None


def main(argv: list[str] | None = None) -> int:
    """Точка входа. Код 1 — неверные настройки, занятый lock или ошибки разового прохода."""
    global _signal_number
    _stop.clear()
    _signal_number = None
    args = parse_args(argv)
    usage_error = validate_cli(args)
    if usage_error:
        print(usage_error, file=sys.stderr)
        return 1
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Ошибка конфигурации:\n{exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        settings = replace(settings, dry_run=True)

    try:
        setup_logging(settings.log_file, settings.log_level)
    except OSError as exc:
        print(
            f"Не удалось открыть журнал {settings.log_file}: {exc}. "
            "Каталог логов должен быть доступен на запись пользователю контейнера.",
            file=sys.stderr,
        )
        return 1

    if args.count:
        return _count_bucket(settings)
    if args.list:
        return _list_bucket(settings, page=args.page, limit=args.limit)
    if args.download:
        return _download_key(settings, args.download)

    try:
        locked = acquire_lock(settings.lock_path)
    except OSError as exc:
        log_event(logging.ERROR, f"не удалось создать lock-файл: {exc}")
        return 1
    if not locked:
        log_event(
            logging.ERROR,
            "уже выполняется другой проход. Остановите сервис: docker compose stop archiver",
        )
        return 1

    saved_signals = register_signals()
    try:
        log_event(logging.INFO, f"старт сервиса {settings.safe_summary()} once={args.once}")
        try:
            touch_heartbeat(settings.heartbeat_path)
        except OSError as exc:
            log_event(logging.ERROR, f"не удалось обновить heartbeat: {exc}")
        if args.once:
            code = _run_once(settings)
        else:
            code = _run_forever(settings)
        log_event(logging.INFO, "сервис остановлен")
        return code
    finally:
        restore_signals(saved_signals)
        release_lock()


def _count_bucket(settings: Settings) -> int:
    """Один запрос списка объектов. Ничего не загружает и не занимает lock."""
    try:
        client = create_s3_client(settings)
        objects, total = count_objects(client, settings.s3_bucket, settings.s3_prefix)
    except Exception as exc:
        log_event(
            logging.ERROR,
            f"не удалось посчитать объекты: {safe_error(exc, settings)}",
        )
        return 1
    log_event(
        logging.INFO,
        "объекты в бакете: "
        f"бакет={settings.s3_bucket} префикс={settings.s3_prefix!r} "
        f"объектов={objects} байт={total}",
    )
    return 0


def _list_bucket(settings: Settings, *, page: int, limit: int) -> int:
    """Одна страница списка. Ничего не загружает и не занимает lock."""
    try:
        client = create_s3_client(settings)
        items, has_next = list_object_page(
            client,
            settings.s3_bucket,
            settings.s3_prefix,
            limit=limit,
            page=page,
        )
    except Exception as exc:
        log_event(
            logging.ERROR,
            f"не удалось получить список: {safe_error(exc, settings)}",
        )
        return 1
    log_event(
        logging.INFO,
        "список: "
        f"страница={page} размер_страницы={limit} показано={len(items)} "
        f"есть_далее={int(has_next)} префикс={settings.s3_prefix!r}",
    )
    for key, size in items:
        log_event(logging.INFO, "объект", key=key, size=size)
    return 0


def _download_key(settings: Settings, key: str) -> int:
    """Скачивает один объект в каталог download. Ничего не удаляет и не занимает lock."""
    try:
        client = create_s3_client(settings)
        destination, size = download_object(
            client,
            settings.s3_bucket,
            key,
            settings.download_dir,
        )
    except FileNotFoundError:
        log_event(logging.ERROR, "объект не найден", key=key)
        return 1
    except ValueError as exc:
        log_event(logging.ERROR, str(exc), key=key)
        return 1
    except OSError as exc:
        log_event(
            logging.ERROR,
            f"не удалось сохранить файл: {exc}. "
            "Каталог download должен быть доступен на запись пользователю контейнера.",
            key=key,
        )
        return 1
    except Exception as exc:
        log_event(
            logging.ERROR,
            f"не удалось скачать объект: {safe_error(exc, settings)}",
            key=key,
        )
        return 1
    relative = destination.relative_to(settings.download_dir).as_posix()
    log_event(
        logging.INFO,
        f"файл сохранён: {destination}. В каталоге проекта это ./download/{relative}",
        key=key,
        size=size,
    )
    return 0


def _run_once(settings: Settings) -> int:
    try:
        client = create_s3_client(settings)
    except Exception as exc:
        log_event(logging.ERROR, f"не удалось создать клиент S3: {safe_error(exc, settings)}")
        return 1
    stats = run_pass(settings, client)
    if _stop.is_set():
        return 0
    return 1 if stats.errors else 0


def _run_forever(settings: Settings) -> int:
    client: Any | None = None
    while not _stop.is_set():
        if client is None:
            try:
                client = create_s3_client(settings)
            except Exception as exc:
                log_event(
                    logging.ERROR,
                    f"не удалось создать клиент S3: {safe_error(exc, settings)}",
                )
                sleep_until_stop(settings.check_interval_seconds)
                continue
        try:
            run_pass(settings, client)
        except Exception as exc:
            log_event(logging.ERROR, f"проход не выполнен: {safe_error(exc, settings)}")
            try:
                touch_heartbeat(settings.heartbeat_path)
            except OSError:
                pass
        if _stop.is_set():
            break
        sleep_until_stop(settings.check_interval_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
