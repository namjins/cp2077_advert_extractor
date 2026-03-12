from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

MANIFEST_COLUMNS = [
    "asset_id",
    "archive_path",
    "relative_texture_path",
    "editable_source_path",
    "edited_path",
    "width",
    "height",
    "has_alpha",
    "status",
    "notes",
]

VALID_STATUSES = {"approved", "skipped", "failed", "ready"}


class ManifestError(ValueError):
    """Raised when manifest rows are invalid."""


def parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n", ""}:
        return False
    raise ManifestError(f"Invalid boolean value: {raw!r}")


@dataclass(slots=True)
class AssetRecord:
    asset_id: str
    archive_path: str
    relative_texture_path: str
    editable_source_path: str
    edited_path: str
    width: int
    height: int
    has_alpha: bool
    status: str
    notes: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, str]) -> "AssetRecord":
        missing = [name for name in MANIFEST_COLUMNS if name not in row]
        if missing:
            raise ManifestError(f"Missing manifest columns: {', '.join(missing)}")

        asset_id = row["asset_id"].strip()
        if not asset_id:
            raise ManifestError("asset_id is required")

        status = row["status"].strip().lower()
        if status not in VALID_STATUSES:
            raise ManifestError(
                f"Invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}"
            )

        try:
            width = int((row["width"] or "0").strip())
            height = int((row["height"] or "0").strip())
        except ValueError as exc:
            raise ManifestError("width/height must be integers") from exc

        has_alpha = parse_bool(row["has_alpha"])

        return cls(
            asset_id=asset_id,
            archive_path=row["archive_path"].strip(),
            relative_texture_path=row["relative_texture_path"].strip(),
            editable_source_path=row["editable_source_path"].strip(),
            edited_path=row["edited_path"].strip(),
            width=width,
            height=height,
            has_alpha=has_alpha,
            status=status,
            notes=row["notes"].strip(),
        )

    def to_row(self) -> dict[str, str]:
        return {
            "asset_id": self.asset_id,
            "archive_path": self.archive_path,
            "relative_texture_path": self.relative_texture_path,
            "editable_source_path": self.editable_source_path,
            "edited_path": self.edited_path,
            "width": str(self.width),
            "height": str(self.height),
            "has_alpha": "true" if self.has_alpha else "false",
            "status": self.status,
            "notes": self.notes,
        }
