"""Atomic file-write helpers and directory creation.

All writes go to a temp file in the same directory, then os.replace() swaps
atomically — readers never see a half-written manifest or report.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from pathlib import Path
from typing import Iterable


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text to a file atomically via temp-file-then-replace.

    The temp file is created in the same directory as the target so that
    os.replace() is a same-filesystem rename (atomic on both POSIX and NTFS).
    newline="" prevents Python from doing platform-specific newline translation
    (we want consistent output across platforms).
    """
    ensure_dir(path.parent)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding=encoding, dir=path.parent, delete=False, newline=""
    ) as tmp:
        tmp.write(text)
        temp_name = tmp.name
    os.replace(temp_name, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes to a file atomically via temp-file-then-replace."""
    ensure_dir(path.parent)
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        temp_name = tmp.name
    os.replace(temp_name, path)


def atomic_write_csv(
    path: Path, fieldnames: list[str], rows: Iterable[dict[str, str]]
) -> None:
    """Write a CSV file atomically.

    Buffers the entire CSV into a StringIO first, then delegates to
    atomic_write_text for the temp-file-then-replace dance.  This avoids
    the DictWriter holding a file handle open during the write — if the
    process crashes mid-row, the original file is untouched.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    atomic_write_text(path, buffer.getvalue())
