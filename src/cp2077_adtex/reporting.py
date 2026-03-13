"""Pipeline reporting — asset log (CSV) and human-readable summary (TXT).

Every stage writes:
  output/asset_log.csv  — one row per asset processed (stage, status, message)
  output/summary.txt    — aggregate counts and a list of output file paths
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .io_utils import atomic_write_csv, atomic_write_text

ASSET_LOG_COLUMNS = ["timestamp", "stage", "asset_id", "status", "message"]


@dataclass(slots=True)
class AssetLogEntry:
    stage: str
    asset_id: str
    status: str
    message: str

    def to_row(self) -> dict[str, str]:
        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "stage": self.stage,
            "asset_id": self.asset_id,
            "status": self.status,
            "message": self.message,
        }


def write_asset_log(path: Path, rows: list[AssetLogEntry]) -> None:
    atomic_write_csv(path, ASSET_LOG_COLUMNS, [row.to_row() for row in rows])


def write_summary(
    path: Path,
    *,
    stage: str,
    mod_name: str,
    counts: dict[str, int],
    notes: list[str],
    outputs: list[Path],
) -> None:
    lines = [
        f"stage={stage}",
        f"mod_name={mod_name}",
        f"processed={counts.get('processed', 0)}",
        f"succeeded={counts.get('succeeded', 0)}",
        f"failed={counts.get('failed', 0)}",
        f"skipped={counts.get('skipped', 0)}",
    ]

    if notes:
        lines.append("notes:")
        for item in notes:
            lines.append(f"- {item}")

    if outputs:
        lines.append("outputs:")
        for output in outputs:
            lines.append(f"- {output}")

    atomic_write_text(path, "\n".join(lines) + "\n")
