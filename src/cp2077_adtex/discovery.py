from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path

from .config import PipelineConfig
from .io_utils import atomic_write_csv
from .models import AssetRecord
from .wolvenkit import WolvenKitRunner

TARGET_ARCHIVES = [
    "basegame_3_nightcity.archive",
    "basegame_4_gamedata.archive",
    "ep1_1_nightcity.archive",
    "ep1_2_gamedata.archive",
]

DISCOVERY_REGEXES = [
    r"^base\\environment\\decoration\\advertising\\.*\\.xbm$",
    r"^base\\gameplay\\gui\\world\\adverts\\.*\\.xbm$",
    r"^base\\worlds\\.*proxy\\.*advert.*\\.xbm$",
]

CANDIDATE_COLUMNS = [
    "asset_id",
    "archive_path",
    "relative_texture_path",
    "reason",
]


@dataclass(slots=True)
class CandidateAsset:
    asset_id: str
    archive_path: str
    relative_texture_path: str
    reason: str

    def to_report_row(self) -> dict[str, str]:
        return {
            "asset_id": self.asset_id,
            "archive_path": self.archive_path,
            "relative_texture_path": self.relative_texture_path,
            "reason": self.reason,
        }


def discover_candidate_assets(
    game_dir: Path,
    runner: WolvenKitRunner,
    logger: logging.Logger,
) -> list[CandidateAsset]:
    archives = find_candidate_archives(game_dir)
    logger.info("Discovery scanning archives=%s", len(archives))

    candidates: dict[str, CandidateAsset] = {}

    for archive in archives:
        archive_rel = archive.relative_to(game_dir).as_posix()
        for regex in DISCOVERY_REGEXES:
            try:
                entries = runner.list_archive_files(
                    archive_file=archive,
                    regex=regex,
                    logger=logger,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("archiveinfo failed for %s (%s): %s", archive, regex, exc)
                continue

            for entry in entries:
                rel_path = entry.replace("\\", "/")
                if not rel_path.lower().endswith(".xbm"):
                    continue

                asset_id = _build_asset_id(archive_rel, rel_path)
                candidates[asset_id] = CandidateAsset(
                    asset_id=asset_id,
                    archive_path=archive_rel,
                    relative_texture_path=rel_path,
                    reason=f"archiveinfo regex match: {regex}",
                )

    return sorted(candidates.values(), key=lambda row: row.asset_id)


def write_candidate_report(path: Path, candidates: list[CandidateAsset]) -> None:
    atomic_write_csv(path, CANDIDATE_COLUMNS, [row.to_report_row() for row in candidates])


def merge_candidates_into_manifest(
    config: PipelineConfig,
    existing: list[AssetRecord],
    candidates: list[CandidateAsset],
) -> list[AssetRecord]:
    merged: dict[str, AssetRecord] = {row.asset_id: row for row in existing}

    for candidate in candidates:
        if candidate.asset_id in merged:
            continue

        editable_path = config.make_relative(
            config.ads_editable_dir
            / f"{candidate.asset_id}.{config.textures.editable_format}"
        )
        edited_path = config.make_relative(
            config.ads_edited_dir / f"{candidate.asset_id}.{config.textures.editable_format}"
        )

        merged[candidate.asset_id] = AssetRecord(
            asset_id=candidate.asset_id,
            archive_path=candidate.archive_path,
            relative_texture_path=candidate.relative_texture_path,
            editable_source_path=editable_path,
            edited_path=edited_path,
            width=0,
            height=0,
            has_alpha=False,
            status="skipped",
            notes=(
                "discovered candidate; set status to approved to export "
                f"({candidate.reason})"
            ),
        )

    return sorted(merged.values(), key=lambda row: row.asset_id)


def find_candidate_archives(game_dir: Path) -> list[Path]:
    roots = [
        game_dir / "archive" / "pc" / "content",
        game_dir / "archive" / "pc" / "ep1",
    ]

    preferred_names = {name.lower() for name in TARGET_ARCHIVES}

    preferred: list[Path] = []
    all_archives: list[Path] = []

    for root in roots:
        if not root.exists():
            continue

        for archive in sorted(root.glob("*.archive")):
            all_archives.append(archive)
            if archive.name.lower() in preferred_names:
                preferred.append(archive)

    if preferred:
        return preferred
    if all_archives:
        return all_archives

    return sorted(game_dir.rglob("*.archive"))


def _build_asset_id(archive_path: str, relative_texture_path: str) -> str:
    digest = hashlib.sha1(f"{archive_path}|{relative_texture_path}".encode("utf-8")).hexdigest()
    return digest[:16]
