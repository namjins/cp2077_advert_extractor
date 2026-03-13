"""Shared Rich progress bar factory used by discovery, extraction, and finalize stages."""

from __future__ import annotations

from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)


def make_progress() -> Progress:
    """Create a standard progress bar with description, bar, percentage, and elapsed time."""
    return Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    )
