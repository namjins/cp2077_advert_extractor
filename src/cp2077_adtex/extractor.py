"""Extract and discovery orchestration — the main pipeline stages.

Two public entry points:
  run_discovery_stage()  — scan archives, write candidate report, optionally merge into manifest.
  run_extract_stage()    — read manifest, export approved textures via WolvenKit, write
                           editable .tga files, update manifest with image metadata.

Extraction flow for each approved asset:
  1. WolvenKit unbundle: pull the .xbm from the game archive into a temp dir
  2. WolvenKit export:   convert .xbm -> .tga (or configured format)
  3. Copy to original/:  pristine backup of the exported texture
  4. Copy to editable/:  working copy the user can open/inspect
  5. inspect_image():    read dimensions + alpha, store in manifest

All heavy work (unbundle/export per asset) runs in a ThreadPoolExecutor so
multiple textures can be processed concurrently.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import logging
from pathlib import Path, PurePosixPath
import shutil

from .config import PipelineConfig
from .discovery import (
    discover_candidate_assets,
    force_approve_candidates,
    merge_candidates_into_manifest,
    write_candidate_report,
)
from .io_utils import ensure_dir
from .manifest import read_manifest, write_manifest
from .progress import make_progress
from .models import AssetRecord
from .reporting import AssetLogEntry, write_asset_log, write_summary
from .validation import inspect_image
from .wolvenkit import WolvenKitRunner


@dataclass(slots=True)
class StageResult:
    """Returned by discovery and extract stages to report what happened."""
    processed: int
    succeeded: int
    failed: int
    skipped: int
    log_path: Path
    summary_path: Path
    asset_log_path: Path
    notes: list[str]


@dataclass(slots=True)
class ExportOutcome:
    """Per-asset result from _export_single_asset — carries image metadata on success."""
    asset_id: str
    status: str          # "ok", "skipped", or "failed"
    message: str
    width: int | None = None
    height: int | None = None
    has_alpha: bool | None = None


def run_discovery_stage(
    config: PipelineConfig,
    runner: WolvenKitRunner,
    *,
    report_only: bool,
    logger: logging.Logger,
    log_path: Path | None = None,
) -> StageResult:
    ensure_dir(config.paths.work_dir)
    ensure_dir(config.paths.output_dir)

    logger.info("Starting discovery stage (mode=%s, workers=%s)", config.discovery.mode, config.performance.workers)
    candidates = discover_candidate_assets(
        config.paths.game_dir,
        runner,
        logger,
        workers=config.performance.workers,
    )
    write_candidate_report(config.discovery.candidate_report, candidates)

    notes = [f"discovered candidates={len(candidates)}"]

    if not report_only:
        existing = read_manifest(config.discovery.approved_manifest)
        merged = merge_candidates_into_manifest(config, existing, candidates)
        write_manifest(config.discovery.approved_manifest, merged)
        notes.append(f"manifest rows={len(merged)}")

    asset_log_path = config.paths.output_dir / "asset_log.csv"
    summary_path = config.paths.output_dir / "summary.txt"

    log_rows = [
        AssetLogEntry(
            stage="discover",
            asset_id="-",
            status="ok",
            message=f"candidate_report={config.discovery.candidate_report}",
        )
    ]
    write_asset_log(asset_log_path, log_rows)

    write_summary(
        summary_path,
        stage="discover",
        mod_name=config.mod.name,
        counts={
            "processed": len(candidates),
            "succeeded": len(candidates),
            "failed": 0,
            "skipped": 0,
        },
        notes=notes,
        outputs=[config.discovery.candidate_report, config.discovery.approved_manifest],
    )

    logger.info("Discovery stage complete with candidates=%s", len(candidates))

    return StageResult(
        processed=len(candidates),
        succeeded=len(candidates),
        failed=0,
        skipped=0,
        log_path=log_path or Path(""),
        summary_path=summary_path,
        asset_log_path=asset_log_path,
        notes=notes,
    )


def run_extract_stage(
    config: PipelineConfig,
    runner: WolvenKitRunner,
    *,
    discover: bool,
    all_known_roots: bool,
    skip_extract: bool,
    clean: bool,
    logger: logging.Logger,
    log_path: Path,
) -> StageResult:
    """Orchestrate the full extraction pipeline.

    Three mutually-exclusive manifest-population strategies:

      --all-known-roots  Discover + auto-approve ALL candidates from known ad
                         roots.  Best for a fresh bulk extraction.
      --discover         Run discovery and merge into manifest (default_status
                         = "skipped"), then extract only already-approved rows.
      (neither)          Read the existing manifest as-is — assumes the user
                         has already set up approvals.

    After manifest population, rows with status "approved" or "ready" are
    exported in parallel via WolvenKit (unbundle -> export -> copy to
    original/ + editable/).
    """
    logger.info(
        "Starting extract stage discover=%s all_known_roots=%s skip_extract=%s clean=%s workers=%s",
        discover,
        all_known_roots,
        skip_extract,
        clean,
        config.performance.workers,
    )

    # --clean wipes the entire work/ads/ tree so extraction starts fresh.
    # The CLI layer (cli.py) prompts for confirmation if edited/ has files
    # to prevent accidental data loss.
    if clean and config.ads_root_dir.exists():
        shutil.rmtree(config.ads_root_dir)

    # Ensure all work directories exist (no-op if they already do).
    ensure_dir(config.ads_original_dir)
    ensure_dir(config.ads_editable_dir)
    ensure_dir(config.ads_edited_dir)
    ensure_dir(config.ads_packed_dir)
    ensure_dir(config.paths.output_dir)

    notes: list[str] = []

    # --- Manifest population strategy ---
    if all_known_roots:
        # Bulk mode: discover everything, auto-approve all candidates.
        candidates = discover_candidate_assets(
            config.paths.game_dir,
            runner,
            logger,
            workers=config.performance.workers,
        )
        write_candidate_report(config.discovery.candidate_report, candidates)

        existing = read_manifest(config.discovery.approved_manifest)
        manifest_rows = merge_candidates_into_manifest(
            config,
            existing,
            candidates,
            default_status="approved",
        )
        # force_approve also sets existing rows (that match a candidate) to
        # approved, in case they were previously skipped or failed.
        force_approve_candidates(manifest_rows, candidates)
        write_manifest(config.discovery.approved_manifest, manifest_rows)

        notes.append(f"bulk known-root candidates={len(candidates)}")
        notes.append(f"manifest rows={len(manifest_rows)}")

    elif discover:
        # Discovery mode: scan and merge, but don't auto-approve — user
        # reviews candidates and sets status manually.
        discovery_result = run_discovery_stage(
            config,
            runner,
            report_only=False,
            logger=logger,
        )
        notes.extend(discovery_result.notes)
        manifest_rows = read_manifest(config.discovery.approved_manifest)

    else:
        # Plain mode: use the manifest as-is.
        manifest_rows = read_manifest(config.discovery.approved_manifest)

    if not manifest_rows:
        raise ValueError(
            "Approved manifest is empty. Run discover-assets first, use --all-known-roots, or provide approved rows."
        )

    # Fill in blank editable_source_path / edited_path for any manually-added
    # rows that didn't go through merge_candidates_into_manifest.
    for row in manifest_rows:
        _ensure_edit_paths(config, row)

    # --clean destroyed the edited/ directory, so "ready" rows can no longer
    # be finalized.  Demote them back to "approved" so they get a fresh
    # export without a dangling edited_path reference.
    if clean:
        for row in manifest_rows:
            if row.status == "ready":
                row.status = "approved"

    # Only extract rows that are approved (ready for first export) or ready
    # (already edited, but may need metadata refresh).
    approved = [row for row in manifest_rows if row.status in {"approved", "ready"}]

    log_rows: list[AssetLogEntry] = []
    processed = len(approved)
    succeeded = 0
    failed = 0
    skipped = 0

    if not approved:
        notes.append("No rows with status=approved|ready; nothing to export")

    outcomes: dict[str, ExportOutcome] = {}

    if skip_extract:
        # --skip-extract: mark everything as skipped without touching WolvenKit.
        # Useful for refreshing manifest metadata or logs without re-exporting.
        with make_progress() as progress:
            task_id = progress.add_task("Skipping extraction", total=len(approved))
            for row in approved:
                progress.advance(task_id)
                outcomes[row.asset_id] = ExportOutcome(
                    asset_id=row.asset_id,
                    status="skipped",
                    message="Skipped export due to --skip-extract",
                )
    else:
        # Normal path: export all approved assets in parallel.
        # force=clean makes the workers ignore cached files and re-export
        # everything from scratch (matches --clean behavior).
        outcomes = _export_assets_parallel(
            config,
            runner,
            approved,
            logger,
            force=clean,
        )

    # --- Apply outcomes back to the manifest rows ---
    for row in approved:
        outcome = outcomes.get(row.asset_id)
        if outcome is None:
            # Safety net — should never happen unless a thread was killed
            # without raising an exception.
            row.status = "failed"
            row.notes = "Missing export outcome"
            failed += 1
            log_rows.append(
                AssetLogEntry(
                    stage="extract",
                    asset_id=row.asset_id,
                    status="failed",
                    message=row.notes,
                )
            )
            continue

        if outcome.status == "ok":
            # Update image metadata from the freshly-exported file.
            # Use "or" to preserve existing values if the outcome has None/0.
            row.width = outcome.width or row.width
            row.height = outcome.height or row.height
            row.has_alpha = row.has_alpha if outcome.has_alpha is None else outcome.has_alpha
            # Don't demote "ready" rows back to "approved" — the user has
            # already signaled intent to finalize these.
            if row.status != "ready":
                row.status = "approved"
            row.notes = ""
            succeeded += 1
            log_rows.append(
                AssetLogEntry(
                    stage="extract",
                    asset_id=row.asset_id,
                    status="ok",
                    message=outcome.message,
                )
            )
        elif outcome.status == "skipped":
            # --skip-extract or skip-existing optimization.
            skipped += 1
            log_rows.append(
                AssetLogEntry(
                    stage="extract",
                    asset_id=row.asset_id,
                    status="skipped",
                    message=outcome.message,
                )
            )
        else:
            # Export failed — mark the row so the user can diagnose.
            row.status = "failed"
            row.notes = outcome.message
            failed += 1
            logger.warning("Failed to export asset %s: %s", row.asset_id, outcome.message)
            log_rows.append(
                AssetLogEntry(
                    stage="extract",
                    asset_id=row.asset_id,
                    status="failed",
                    message=outcome.message,
                )
            )

    write_manifest(config.discovery.approved_manifest, manifest_rows)

    asset_log_path = config.paths.output_dir / "asset_log.csv"
    summary_path = config.paths.output_dir / "summary.txt"
    write_asset_log(asset_log_path, log_rows)

    write_summary(
        summary_path,
        stage="extract",
        mod_name=config.mod.name,
        counts={
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
        },
        notes=notes,
        outputs=[
            config.discovery.approved_manifest,
            config.discovery.candidate_report,
            asset_log_path,
            summary_path,
            log_path,
        ],
    )

    logger.info(
        "Extract stage complete processed=%s succeeded=%s failed=%s skipped=%s",
        processed,
        succeeded,
        failed,
        skipped,
    )

    return StageResult(
        processed=processed,
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        log_path=log_path,
        summary_path=summary_path,
        asset_log_path=asset_log_path,
        notes=notes,
    )


def _export_assets_parallel(
    config: PipelineConfig,
    runner: WolvenKitRunner,
    rows: list[AssetRecord],
    logger: logging.Logger,
    *,
    force: bool = False,
) -> dict[str, ExportOutcome]:
    """Export multiple assets concurrently via a thread pool.

    Each asset is exported independently in its own thread.  Failures are
    captured per-asset (broad ``except Exception``) so one broken texture
    doesn't abort the entire batch.

    Python's logging module is thread-safe, so logger calls from workers
    are safe without additional synchronization.
    """
    if not rows:
        return {}

    # Don't spawn more threads than there are assets to process.
    max_workers = max(1, min(config.performance.workers, len(rows)))
    outcomes: dict[str, ExportOutcome] = {}

    with make_progress() as progress:
        task_id = progress.add_task("Exporting textures", total=len(rows))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(
                    _export_single_asset, config, runner, row, logger, force=force,
                ): row.asset_id
                for row in rows
            }

            for future in as_completed(future_map):
                asset_id = future_map[future]
                progress.advance(task_id)
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001
                    # Catch-all so one asset failure doesn't crash the pool.
                    outcome = ExportOutcome(
                        asset_id=asset_id,
                        status="failed",
                        message=str(exc),
                    )
                outcomes[outcome.asset_id] = outcome

    return outcomes


def _export_single_asset(
    config: PipelineConfig,
    runner: WolvenKitRunner,
    row: AssetRecord,
    logger: logging.Logger,
    *,
    force: bool = False,
) -> ExportOutcome:
    """Export one texture: unbundle .xbm -> convert to editable format -> read metadata.

    Produces two file copies:
      - original/<asset_id>.tga  — pristine backup, never touched by the user
      - editable/<friendly_name>.tga — working copy the user can open/inspect

    Skip-existing optimization (force=False): if both files already exist,
    skip the expensive WolvenKit unbundle+export round-trip and just re-read
    image metadata from the existing editable file.  This makes repeated
    extract runs fast when most textures haven't changed.
    """
    original_path = config.ads_original_dir / f"{row.asset_id}.{config.textures.editable_format}"
    editable_path = config.resolve_user_path(row.editable_source_path)

    try:
        # Skip-existing: both files present and not force-refreshing.
        if not force and original_path.exists() and editable_path.exists():
            image = inspect_image(editable_path)
            return ExportOutcome(
                asset_id=row.asset_id,
                status="ok",
                message=f"Already exported, refreshed metadata from {editable_path}",
                width=image.width,
                height=image.height,
                has_alpha=image.has_alpha,
            )

        # Full export: unbundle .xbm from archive, convert to editable format.
        runner.export_texture(
            game_dir=config.paths.game_dir,
            archive_path=row.archive_path,
            relative_texture_path=row.relative_texture_path,
            output_file=original_path,
            uext=config.textures.editable_format,
            logger=logger,
        )
        # Copy the pristine export to editable/ with a human-readable name.
        ensure_dir(editable_path.parent)
        shutil.copy2(original_path, editable_path)

        # Read image dimensions and alpha from the freshly-exported file.
        image = inspect_image(editable_path)
        return ExportOutcome(
            asset_id=row.asset_id,
            status="ok",
            message=f"Exported editable source to {editable_path}",
            width=image.width,
            height=image.height,
            has_alpha=image.has_alpha,
        )
    except Exception as exc:  # noqa: BLE001
        return ExportOutcome(
            asset_id=row.asset_id,
            status="failed",
            message=str(exc),
        )


def _ensure_edit_paths(config: PipelineConfig, row: AssetRecord) -> None:
    """Fill in editable_source_path and edited_path if the manifest row has blanks.

    Used as a safety net for rows that were added manually without going
    through merge_candidates_into_manifest (which assigns collision-free
    friendly names).  Uses a simple stem derivation (no collision detection)
    — falls back to asset_id if the texture path has no usable stem.

    Discovery-created rows already have correct paths stored, so this is
    normally a no-op for them.
    """
    if not row.editable_source_path or not row.edited_path:
        # Compute stem once and reuse for both paths.
        stem = _simple_stem(row.relative_texture_path) or row.asset_id
        ext = config.textures.editable_format
        if not row.editable_source_path:
            row.editable_source_path = config.make_relative(
                config.ads_editable_dir / f"{stem}.{ext}"
            )
        if not row.edited_path:
            row.edited_path = config.make_relative(
                config.ads_edited_dir / f"{stem}.{ext}"
            )


def _simple_stem(relative_texture_path: str) -> str:
    """Return the filename stem of a texture path (e.g. 'rayfield_720p')."""
    return PurePosixPath(relative_texture_path.replace("\\", "/")).stem


