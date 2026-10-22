"""Выбор файлов по возрасту, расширению и типу."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dateutil.relativedelta import relativedelta

from app.scanner import find_candidates, is_old_enough
from tests.conftest import place_file

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def test_age_boundary_uses_relativedelta() -> None:
    cutoff = NOW - relativedelta(months=3)
    assert is_old_enough(cutoff, NOW, 3) is False
    assert is_old_enough(cutoff - timedelta(seconds=1), NOW, 3) is True


def test_selects_old_mp3_and_skips_fresh_other_and_links(tmp_path: Path) -> None:
    src = tmp_path / "records"
    src.mkdir()
    old = place_file(src, "2023/jan/call.mp3", b"old", NOW - relativedelta(months=4))
    place_file(src, "2026/fresh.mp3", b"new", NOW - relativedelta(months=1))
    place_file(src, "2023/note.txt", b"text", NOW - relativedelta(months=4))
    place_file(src, "2023/UPPER.MP3", b"upper", NOW - relativedelta(months=4))
    place_file(src, "2023/partial.mp3.part", b"part", NOW - relativedelta(months=4))
    (src / "not-a-file.mp3").mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"out")
    link = src / "link.mp3"
    link.symlink_to(outside)

    found = find_candidates(
        src,
        extensions={".mp3"},
        age_months=3,
        now=NOW,
    )
    paths = {item.path for item in found}
    assert paths == {old, src / "2023" / "UPPER.MP3"}
    assert all(item.path.suffix.lower() == ".mp3" for item in found)


def test_symlink_stat_is_not_followed(tmp_path: Path) -> None:
    src = tmp_path / "records"
    src.mkdir()
    outside = tmp_path / "real.mp3"
    outside.write_bytes(b"audio")
    old = NOW.timestamp() - 200 * 24 * 3600
    os.utime(outside, (old, old))
    link = src / "alias.mp3"
    link.symlink_to(outside)
    found = find_candidates(
        src,
        extensions={".mp3"},
        age_months=3,
        now=NOW,
    )
    assert found == []
