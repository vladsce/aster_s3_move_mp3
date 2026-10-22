"""Поиск записей, которые пора переносить в архив."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dateutil.relativedelta import relativedelta

from app.config import log_event


@dataclass(frozen=True)
class Candidate:
    """Файл, прошедший фильтр возраста и расширения."""

    path: Path
    size: int
    mtime: datetime


def is_old_enough(mtime: datetime, now: datetime, age_months: int) -> bool:
    """Файл старше порога. Ноль месяцев отключает проверку возраста."""
    if age_months == 0:
        return True
    cutoff = now - relativedelta(months=age_months)
    return mtime < cutoff


def find_candidates(
    src_dir: Path,
    *,
    extensions: set[str],
    age_months: int,
    now: datetime,
) -> list[Candidate]:
    """Рекурсивно находит обычные файлы. Символические ссылки пропускает."""
    if not src_dir.exists():
        raise FileNotFoundError(f"Каталог записей не найден: {src_dir}")
    if not src_dir.is_dir():
        raise NotADirectoryError(f"Путь записей не является каталогом: {src_dir}")

    allowed = {
        item.lower() if item.startswith(".") else f".{item.lower()}"
        for item in extensions
    }
    found: list[Candidate] = []
    for path in src_dir.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat_result = path.stat(follow_symlinks=False)
        except OSError as exc:
            log_event(logging.WARNING, f"файл пропущен: {exc}", path=path)
            continue
        if path.suffix.lower() not in allowed:
            continue
        mtime = datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc)
        if not is_old_enough(mtime, now, age_months):
            continue
        found.append(Candidate(path=path, size=stat_result.st_size, mtime=mtime))
    found.sort(key=lambda item: item.path.as_posix())
    return found
