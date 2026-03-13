"""Archive scanning and candidate discovery for Cyberpunk 2077 ad textures.

Discovery finds textures that are likely in-world advertisements by matching
their internal archive paths against known prefixes and heuristics.  Results
are written to candidate_assets.csv and optionally merged into the approved
manifest so downstream stages (extract, finalize) can act on them.

The three heuristic buckets:
  1. base\\gameplay\\gui\\world\\adverts\\  — named brand adverts shown on
     billboards and screens (Rayfield, Kiroshi, Arasaka, etc.)
  2. base\\environment\\decoration\\advertising\\  — physical signage, posters,
     digital vector boards, holograms, and streamer banners.
  3. base\\worlds\\...\\proxy\\...advert...  — low-resolution proxy textures
     rendered at distance for the same ad meshes.

Note: Category 2 includes auxiliary texture maps (_g gradient, _m metalness,
_n normal, _r roughness, _e emissive) that control material properties rather
than visible ad imagery.  These are small (often 32x8 or 64x4) and usually
don't need editing unless you want to alter surface lighting.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path, PurePosixPath

from .config import PipelineConfig
from .io_utils import atomic_write_csv
from .models import AssetRecord
from .progress import make_progress
from .wolvenkit import WolvenKitRunner

# Archives that contain the vast majority of ad textures.  The pipeline
# prefers these over a full recursive scan (see find_candidate_archives).
TARGET_ARCHIVES = [
    "basegame_3_nightcity.archive",
    "basegame_4_gamedata.archive",
    "ep1_1_nightcity.archive",
    "ep1_2_gamedata.archive",
]

# Internal archive paths that start with one of these prefixes are considered
# ad texture candidates.  Uses backslash because WolvenKit archiveinfo returns
# paths with backslash separators.
AD_ROOT_PREFIXES = [
    "base\\environment\\decoration\\advertising\\",
    "base\\gameplay\\gui\\world\\adverts\\",
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


@dataclass(slots=True)
class DiscoveryChunk:
    archive_rel: str
    entries: list[str]


def discover_candidate_assets(
    game_dir: Path,
    runner: WolvenKitRunner,
    logger: logging.Logger,
    *,
    workers: int,
) -> list[CandidateAsset]:
    """Scan game archives in parallel and return textures matching ad heuristics."""
    archives = find_candidate_archives(game_dir)
    logger.info("Discovery scanning archives=%s", len(archives))

    if not archives:
        return []

    max_workers = max(1, min(workers, len(archives)))
    chunks: list[DiscoveryChunk] = []

    with make_progress() as progress:
        task_id = progress.add_task("Scanning archives", total=len(archives))

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(_scan_archive, runner, game_dir, archive, logger): archive
                for archive in archives
            }

            for future in as_completed(future_map):
                archive = future_map[future]
                progress.advance(task_id)

                try:
                    chunks.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("archiveinfo failed for %s: %s", archive, exc)

    candidates: dict[str, CandidateAsset] = {}

    for chunk in chunks:
        for entry in chunk.entries:
            reason = _match_reason(entry)
            if not reason:
                continue

            rel_path = entry.replace("\\", "/")
            asset_id = _build_asset_id(chunk.archive_rel, rel_path)
            candidates[asset_id] = CandidateAsset(
                asset_id=asset_id,
                archive_path=chunk.archive_rel,
                relative_texture_path=rel_path,
                reason=reason,
            )

    return sorted(candidates.values(), key=lambda row: row.asset_id)


def write_candidate_report(path: Path, candidates: list[CandidateAsset]) -> None:
    atomic_write_csv(path, CANDIDATE_COLUMNS, [row.to_report_row() for row in candidates])


def merge_candidates_into_manifest(
    config: PipelineConfig,
    existing: list[AssetRecord],
    candidates: list[CandidateAsset],
    *,
    default_status: str = "skipped",
) -> list[AssetRecord]:
    """Upsert discovered candidates into the manifest, preserving existing rows.

    New candidates get default_status (usually "skipped" for manual review, or
    "approved" when using --all-known-roots for bulk extraction).

    Editable and edited filenames are derived from the texture path
    (e.g. "rayfield_720p.tga") rather than the opaque asset_id hash, making
    the work/ads/editable/ directory navigable by humans.  Collisions (two
    textures with the same stem) are resolved by prepending the parent
    directory name (e.g. "rayfield__rayfield_720p.tga").  If that is still
    not unique, the asset_id is appended as a final disambiguator.

    Note: friendly filenames are re-derived for ALL rows (existing + new), so
    running merge again will update old hash-based paths to human-readable names.
    """
    merged: dict[str, AssetRecord] = {row.asset_id: row for row in existing}

    # Add new candidates to the merged set
    new_candidates = [c for c in candidates if c.asset_id not in merged]
    for candidate in new_candidates:
        merged[candidate.asset_id] = AssetRecord(
            asset_id=candidate.asset_id,
            archive_path=candidate.archive_path,
            relative_texture_path=candidate.relative_texture_path,
            editable_source_path="",  # Will be filled in below
            edited_path="",  # Will be filled in below
            width=0,
            height=0,
            has_alpha=False,
            status=default_status,
            notes=(
                "discovered candidate; set status to approved to export "
                f"({candidate.reason})"
            ),
        )

    # Build collision-free friendly stems for ALL candidates (existing + new).
    # This ensures re-runs update old hash-based paths to human-readable names.
    friendly_stems = _derive_friendly_stems(candidates)

    for row in merged.values():
        if row.asset_id in friendly_stems:
            stem = friendly_stems[row.asset_id]
            filename = f"{stem}.{config.textures.editable_format}"
            row.editable_source_path = config.make_relative(
                config.ads_editable_dir / filename
            )
            row.edited_path = config.make_relative(
                config.ads_edited_dir / filename
            )

    return sorted(merged.values(), key=lambda row: row.asset_id)


def force_approve_candidates(
    manifest_rows: list[AssetRecord],
    candidates: list[CandidateAsset],
) -> None:
    """Set status='approved' on all manifest rows that match a candidate.

    Used by --all-known-roots to bypass the manual approval step.
    """
    candidate_ids = {row.asset_id for row in candidates}
    for row in manifest_rows:
        if row.asset_id in candidate_ids:
            row.status = "approved"


def find_candidate_archives(game_dir: Path) -> list[Path]:
    """Locate .archive files to scan, preferring the known TARGET_ARCHIVES.

    Fallback order: preferred archives > any archives in content/ep1 dirs >
    recursive scan of game_dir (last resort for non-standard installs).
    """
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


def _scan_archive(
    runner: WolvenKitRunner,
    game_dir: Path,
    archive: Path,
    logger: logging.Logger,
) -> DiscoveryChunk:
    entries = runner.list_archive_files(
        archive_file=archive,
        logger=logger,
    )

    archive_rel = archive.relative_to(game_dir).as_posix()
    return DiscoveryChunk(archive_rel=archive_rel, entries=entries)


def _match_reason(entry: str) -> str | None:
    """Return a human-readable reason if the entry is an ad texture, else None."""
    lowered = entry.lower()
    if not lowered.endswith(".xbm"):
        return None

    for prefix in AD_ROOT_PREFIXES:
        if lowered.startswith(prefix):
            return f"known ad root: {prefix}"

    if lowered.startswith("base\\worlds\\") and "\\proxy\\" in lowered and "advert" in lowered:
        return "known ad proxy path"

    return None


def _build_asset_id(archive_path: str, relative_texture_path: str) -> str:
    """Derive a stable 16-hex-char ID from the archive + texture path pair.

    Uses SHA-1 (truncated) — not for security, just for a short collision-
    resistant key that stays the same across re-runs.
    """
    digest = hashlib.sha1(f"{archive_path}|{relative_texture_path}".encode("utf-8")).hexdigest()
    return digest[:16]



def _derive_friendly_stems(
    candidates: list[CandidateAsset],
    reserved: set[str] | None = None,
) -> dict[str, str]:
    """Map asset_id -> human-readable filename stem, resolving collisions.

    reserved is a set of stems already claimed by existing manifest rows —
    new stems that would collide with these are expanded just like internal
    collisions.

    Strategy (applied in order until all stems are unique):
      1. Use just the texture filename stem  (e.g. "rayfield_720p")
      2. Prepend the immediate parent dir    (e.g. "rayfield__rayfield_720p")
      3. Append the asset_id as a suffix     (e.g. "rayfield__rayfield_720p__fcea8993b4ef62b5")
    """
    reserved = reserved or set()
    id_to_candidate = {c.asset_id: c for c in candidates}

    stems: dict[str, str] = {}
    for c in candidates:
        p = PurePosixPath(c.relative_texture_path.replace("\\", "/"))
        stems[c.asset_id] = p.stem

    # Pass 2: expand stems that collide with each other or with reserved set
    stem_to_ids: dict[str, list[str]] = {}
    for asset_id, stem in stems.items():
        stem_to_ids.setdefault(stem, []).append(asset_id)

    for stem, ids in stem_to_ids.items():
        if len(ids) <= 1 and stem not in reserved:
            continue
        for asset_id in ids:
            p = PurePosixPath(id_to_candidate[asset_id].relative_texture_path.replace("\\", "/"))
            stems[asset_id] = f"{p.parent.name}__{p.stem}"

    # Pass 3: append asset_id to any still-colliding stems
    stem_to_ids2: dict[str, list[str]] = {}
    for asset_id, stem in stems.items():
        stem_to_ids2.setdefault(stem, []).append(asset_id)

    for stem, ids in stem_to_ids2.items():
        if len(ids) <= 1 and stem not in reserved:
            continue
        for asset_id in ids:
            stems[asset_id] = f"{stem}__{asset_id}"

    return stems


