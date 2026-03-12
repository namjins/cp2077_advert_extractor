from __future__ import annotations

import csv
from pathlib import Path

from .io_utils import atomic_write_csv
from .models import AssetRecord, MANIFEST_COLUMNS, ManifestError


def read_manifest(path: Path) -> list[AssetRecord]:
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
    sorted_rows = sorted(rows, key=lambda row: row.asset_id)
    atomic_write_csv(path, MANIFEST_COLUMNS, [row.to_row() for row in sorted_rows])


def _validate_manifest_header(fieldnames: list[str] | None, path: Path) -> None:
    if fieldnames is None:
        raise ManifestError(f"{path} is empty or missing a header")

    missing = [name for name in MANIFEST_COLUMNS if name not in fieldnames]
    if missing:
        raise ManifestError(
            f"{path} is missing required manifest columns: {', '.join(missing)}"
        )
