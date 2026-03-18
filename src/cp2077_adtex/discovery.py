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

# Archives that contain the vast majority of ad textures.  Scanning only
# these four is far faster than a recursive scan of the entire game directory
# (which can contain 50+ archives totaling ~60 GB).  The fallback in
# find_candidate_archives covers non-standard installs or future DLC.
TARGET_ARCHIVES = [
    "basegame_3_nightcity.archive",   # Night City environment + UI textures
    "basegame_4_gamedata.archive",     # Additional gameplay/UI assets
    "ep1_1_nightcity.archive",         # Phantom Liberty environment textures
    "ep1_2_gamedata.archive",          # Phantom Liberty gameplay assets
]

# Internal archive paths that start with one of these prefixes are considered
# ad texture candidates.  Uses backslash because WolvenKit archiveinfo returns
# paths with backslash separators on all platforms.
AD_ROOT_PREFIXES = [
    "base\\environment\\decoration\\advertising\\",  # Physical signs, posters, holograms
    "base\\gameplay\\gui\\world\\adverts\\",          # Brand adverts on screens/billboards
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
    """Scan game archives in parallel and return textures matching ad heuristics.

    Each archive is scanned in its own thread via WolvenKit's archiveinfo
    command.  The results are then filtered through _match_reason() to keep
    only textures whose internal path matches known ad-texture prefixes.

    Returns a deduplicated, sorted list keyed by asset_id (SHA-1 hash of
    archive + texture path).
    """
    archives = find_candidate_archives(game_dir)
    logger.info("Discovery scanning archives=%s", len(archives))

    if not archives:
        return []

    # Cap thread count to the number of archives (no point spawning idle threads).
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
                    # Per-archive failure is non-fatal — log and continue with
                    # the remaining archives so partial results are still useful.
                    logger.warning("archiveinfo failed for %s: %s", archive, exc)

    # Flatten chunks and filter to ad textures.  Use a dict keyed by
    # asset_id to deduplicate (same texture in multiple archives is rare
    # but possible with DLC overlays).
    candidates: dict[str, CandidateAsset] = {}

    for chunk in chunks:
        for entry in chunk.entries:
            reason = _match_reason(entry)
            if not reason:
                continue

            # Normalize to forward slashes for consistent storage in the
            # manifest / candidate report.
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

    New candidates get *default_status* (usually "skipped" for manual review,
    or "approved" when using ``--all-known-roots`` for bulk extraction).

    Editable and edited filenames are derived from the texture path
    (e.g. "rayfield_720p.tga") rather than the opaque asset_id hash, making
    the ``work/ads/editable/`` directory navigable by humans.  Collisions
    (two textures with the same stem) are resolved by prepending the parent
    directory name (e.g. "rayfield__rayfield_720p.tga").  If that is still
    not unique, the asset_id is appended as a final disambiguator.

    Friendly filenames are re-derived for ALL candidate-origin rows on every
    merge, so running merge again upgrades old hash-based paths to
    human-readable names.  Manually-added rows (not in candidates) keep
    their existing filenames via the "reserved stems" mechanism.
    """
    # Start with all existing manifest rows, keyed by asset_id for O(1) lookup.
    merged: dict[str, AssetRecord] = {row.asset_id: row for row in existing}

    # Insert newly-discovered candidates that don't already appear in the
    # manifest.  Existing rows are preserved as-is (status, notes, paths).
    new_candidates = [c for c in candidates if c.asset_id not in merged]
    for candidate in new_candidates:
        merged[candidate.asset_id] = AssetRecord(
            asset_id=candidate.asset_id,
            archive_path=candidate.archive_path,
            relative_texture_path=candidate.relative_texture_path,
            editable_source_path="",  # Filled in by friendly-stem assignment below
            edited_path="",           # Filled in by friendly-stem assignment below
            width=0,
            height=0,
            has_alpha=False,
            status=default_status,
            notes=(
                "discovered candidate; set status to approved to export "
                f"({candidate.reason})"
            ),
        )

    # --- Friendly filename assignment ---
    #
    # Build collision-free human-readable stems for ALL candidate-origin rows.
    # "Reserved" stems come from manually-added manifest rows (those not in
    # candidates).  These are locked so candidates don't steal filenames the
    # user assigned by hand.
    #
    # Rows that ARE candidates will have their stems re-derived here, so they
    # must NOT appear in reserved (or they'd collide with themselves and get
    # needlessly renamed on every re-run).
    candidate_ids = {c.asset_id for c in candidates}
    reserved_stems = set()
    for row in existing:
        if row.asset_id not in candidate_ids and row.editable_source_path:
            reserved_stems.add(PurePosixPath(row.editable_source_path).stem)

    friendly_stems = _derive_friendly_stems(candidates, reserved=reserved_stems)

    # Apply friendly stems to the merged rows, updating editable_source_path
    # and edited_path with the human-readable filenames.
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

    Fallback order (stops at first non-empty result):
      1. Preferred archives from TARGET_ARCHIVES (fastest — only 4 files)
      2. Any .archive in the standard content/ep1 directories
      3. Recursive scan of entire game_dir (last resort for non-standard installs)
    """
    roots = [
        game_dir / "archive" / "pc" / "content",  # Base game archives
        game_dir / "archive" / "pc" / "ep1",       # Phantom Liberty archives
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

    # Last resort: recursive scan.  Slow but handles non-standard installs
    # (e.g. game installed to a custom directory structure).
    return sorted(game_dir.rglob("*.archive"))


def _scan_archive(
    runner: WolvenKitRunner,
    game_dir: Path,
    archive: Path,
    logger: logging.Logger,
) -> DiscoveryChunk:
    """List all files in one archive via WolvenKit and return as a chunk.

    The archive path is stored relative to game_dir (e.g.
    "archive/pc/content/basegame_4_gamedata.archive") so it can be resolved
    later from any machine with the same game install.

    Note: archive.relative_to(game_dir) will raise ValueError if the archive
    is not under game_dir.  This is safe because find_candidate_archives
    always returns paths rooted under game_dir.
    """
    entries = runner.list_archive_files(
        archive_file=archive,
        logger=logger,
    )

    archive_rel = archive.relative_to(game_dir).as_posix()
    return DiscoveryChunk(archive_rel=archive_rel, entries=entries)


def _match_reason(entry: str) -> str | None:
    """Return a human-readable reason if the entry is an ad texture, else None.

    Three heuristic buckets:
      1. Starts with a known AD_ROOT_PREFIX → definite ad texture
      2. Under base\\worlds\\ with \\proxy\\ and "advert" in path → distance
         proxy texture for ad meshes (low-res stand-ins rendered at distance)
      3. Anything else → not an ad texture (return None)

    Only .xbm files (WolvenKit's internal texture format) are considered.
    """
    lowered = entry.lower()
    if not lowered.endswith(".xbm"):
        return None

    for prefix in AD_ROOT_PREFIXES:
        if lowered.startswith(prefix):
            return f"known ad root: {prefix}"

    # Proxy textures live under base\worlds\<city>\<district>\proxy\ and
    # contain "advert" somewhere in the remaining path.
    if lowered.startswith("base\\worlds\\") and "\\proxy\\" in lowered and "advert" in lowered:
        return "known ad proxy path"

    return None


def _build_asset_id(archive_path: str, relative_texture_path: str) -> str:
    """Derive a stable 16-hex-char ID from the archive + texture path pair.

    Uses SHA-1 (truncated to 16 hex chars = 64 bits).  This is NOT used for
    security — just as a short, collision-resistant key that stays the same
    across re-runs.  64 bits gives ~2^32 expected uniqueness (birthday bound),
    which is far more than the ~2000 ad textures in the game.

    The pipe separator ensures that archive "a|b" + texture "c" doesn't
    collide with archive "a" + texture "b|c".
    """
    digest = hashlib.sha1(f"{archive_path}|{relative_texture_path}".encode("utf-8")).hexdigest()
    return digest[:16]



def _derive_friendly_stems(
    candidates: list[CandidateAsset],
    reserved: set[str] | None = None,
) -> dict[str, str]:
    """Map asset_id -> human-readable filename stem, resolving collisions.

    *reserved* is a set of stems already claimed by manually-added manifest
    rows — new stems that would collide with these are expanded just like
    internal collisions, so user-assigned names are never overwritten.

    Three-pass collision resolution:

      Pass 1 — Use just the texture filename stem.
               Example: "base/.../rayfield/rayfield_720p.xbm" -> "rayfield_720p"
               Most textures have unique stems, so this pass resolves ~95% of cases.

      Pass 2 — For any stems that collide (with each other OR with reserved),
               prepend the immediate parent directory name separated by "__".
               Example: "rayfield__rayfield_720p"
               This disambiguates textures with the same name in different
               subdirectories (e.g. two "banner_d.xbm" in different folders).

      Pass 3 — For any stems STILL colliding after pass 2, append the asset_id
               as a final disambiguator (guaranteed unique).
               Example: "rayfield__rayfield_720p__fcea8993b4ef62b5"
               This is the nuclear option — always unique, but less readable.
    """
    reserved = reserved or set()
    id_to_candidate = {c.asset_id: c for c in candidates}

    # --- Pass 1: start with just the texture filename stem ---
    stems: dict[str, str] = {}
    for c in candidates:
        p = PurePosixPath(c.relative_texture_path.replace("\\", "/"))
        stems[c.asset_id] = p.stem

    # --- Pass 2: expand collisions by prepending parent directory ---
    # Build a reverse map: stem -> list of asset_ids that want that stem.
    # Any stem claimed by >1 asset, or that collides with a reserved stem,
    # gets expanded.
    stem_to_ids: dict[str, list[str]] = {}
    for asset_id, stem in stems.items():
        stem_to_ids.setdefault(stem, []).append(asset_id)

    for stem, ids in stem_to_ids.items():
        if len(ids) <= 1 and stem not in reserved:
            continue  # No collision — keep the simple stem.
        for asset_id in ids:
            p = PurePosixPath(id_to_candidate[asset_id].relative_texture_path.replace("\\", "/"))
            stems[asset_id] = f"{p.parent.name}__{p.stem}"

    # --- Pass 3: append asset_id to any remaining collisions ---
    stem_to_ids2: dict[str, list[str]] = {}
    for asset_id, stem in stems.items():
        stem_to_ids2.setdefault(stem, []).append(asset_id)

    for stem, ids in stem_to_ids2.items():
        if len(ids) <= 1 and stem not in reserved:
            continue  # No collision after pass 2.
        for asset_id in ids:
            stems[asset_id] = f"{stem}__{asset_id}"

    return stems


