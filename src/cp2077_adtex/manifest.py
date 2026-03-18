"""Read/write the assets_manifest.csv file that drives the pipeline.

The manifest is the single source of truth for which textures are discovered,
approved, exported, edited, and ready for finalization.  Every pipeline stage
reads it, and extract/finalize stages write back updated status and metadata.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .io_utils import atomic_write_csv
from .models import AssetRecord, MANIFEST_COLUMNS, ManifestError


def read_manifest(path: Path) -> list[AssetRecord]:
    """Load the manifest CSV and return a list of validated AssetRecords.

    Returns an empty list if the file doesn't exist yet (first run).
    Raises ManifestError with file path and line number on parse failures
    so the user can locate the bad row quickly.
    """
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        _validate_manifest_header(reader.fieldnames, path)
        rows: list[AssetRecord] = []
        for line_number, row in enumerate(reader, start=2):
            try:
                rows.append(AssetRecord.from_row(row))
            except ManifestError as exc:
                raise ManifestError(f"{path}:{line_number}: {exc}") from exc
    return rows


def write_manifest(path: Path, rows: list[AssetRecord]) -> None:
    """Write the manifest CSV, sorted by asset_id for stable diffs.

    Uses atomic_write_csv (temp file + os.replace) so readers never see a
    half-written file — important because multiple stages read this file.
    """
    sorted_rows = sorted(rows, key=lambda row: row.asset_id)
    atomic_write_csv(path, MANIFEST_COLUMNS, [row.to_row() for row in sorted_rows])


def _validate_manifest_header(fieldnames: list[str] | None, path: Path) -> None:
    """Ensure the CSV header contains all required columns.

    This catches two common problems:
      - Completely empty file (fieldnames is None)
      - Hand-edited CSV that accidentally dropped a column
    """
    if fieldnames is None:
        raise ManifestError(f"{path} is empty or missing a header")

    missing = [name for name in MANIFEST_COLUMNS if name not in fieldnames]
    if missing:
        raise ManifestError(
            f"{path} is missing required manifest columns: {', '.join(missing)}"
        )
