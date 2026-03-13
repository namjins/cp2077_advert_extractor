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
    logger.info(
        "Starting extract stage discover=%s all_known_roots=%s skip_extract=%s clean=%s workers=%s",
        discover,
        all_known_roots,
        skip_extract,
        clean,
        config.performance.workers,
    )

    if clean and config.ads_root_dir.exists():
        shutil.rmtree(config.ads_root_dir)

    ensure_dir(config.ads_original_dir)
    ensure_dir(config.ads_editable_dir)
    ensure_dir(config.ads_edited_dir)
    ensure_dir(config.ads_packed_dir)
    ensure_dir(config.paths.output_dir)

    notes: list[str] = []

    if all_known_roots:
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
        force_approve_candidates(manifest_rows, candidates)
        write_manifest(config.discovery.approved_manifest, manifest_rows)

        notes.append(f"bulk known-root candidates={len(candidates)}")
        notes.append(f"manifest rows={len(manifest_rows)}")

    elif discover:
        discovery_result = run_discovery_stage(
            config,
            runner,
            report_only=False,
            logger=logger,
        )
        notes.extend(discovery_result.notes)
        manifest_rows = read_manifest(config.discovery.approved_manifest)

    else:
        manifest_rows = read_manifest(config.discovery.approved_manifest)
    if not manifest_rows:
        raise ValueError(
            "Approved manifest is empty. Run discover-assets first, use --all-known-roots, or provide approved rows."
        )

    for row in manifest_rows:
        _ensure_edit_paths(config, row)

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
        outcomes = _export_assets_parallel(
            config,
            runner,
            approved,
            logger,
            force=clean,
        )

    for row in approved:
        outcome = outcomes.get(row.asset_id)
        if outcome is None:
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
            row.width = outcome.width or row.width
            row.height = outcome.height or row.height
            row.has_alpha = row.has_alpha if outcome.has_alpha is None else outcome.has_alpha
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
    if not rows:
        return {}

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

    Writes two copies: original/ (pristine backup) and editable/ (user working copy).
    When force=False and both files already exist, skips the WolvenKit round-trip
    and just refreshes image metadata from the existing editable file.
    """
    original_path = config.ads_original_dir / f"{row.asset_id}.{config.textures.editable_format}"
    editable_path = config.resolve_user_path(row.editable_source_path)

    try:
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

        runner.export_texture(
            game_dir=config.paths.game_dir,
            archive_path=row.archive_path,
            relative_texture_path=row.relative_texture_path,
            output_file=original_path,
            uext=config.textures.editable_format,
            logger=logger,
        )
        ensure_dir(editable_path.parent)
        shutil.copy2(original_path, editable_path)
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

    Used as a safety net for rows that were added manually without going through
    merge_candidates_into_manifest.  Uses a simple stem derivation (no collision
    detection); discovery-created rows already have correct paths stored.
    """
    if not row.editable_source_path:
        stem = _simple_stem(row.relative_texture_path) or row.asset_id
        row.editable_source_path = config.make_relative(
            config.ads_editable_dir / f"{stem}.{config.textures.editable_format}"
        )
    if not row.edited_path:
        stem = _simple_stem(row.relative_texture_path) or row.asset_id
        row.edited_path = config.make_relative(
            config.ads_edited_dir / f"{stem}.{config.textures.editable_format}"
        )


def _simple_stem(relative_texture_path: str) -> str:
    """Return the filename stem of a texture path (e.g. 'rayfield_720p')."""
    return PurePosixPath(relative_texture_path.replace("\\", "/")).stem


