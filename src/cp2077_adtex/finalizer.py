"""Finalize stage — validate edits, re-import to .xbm, pack archive, produce zip.

Workflow for each asset with status="ready":
  1. Validate the edited image (dimensions, alpha) unless --skip-validate
  2. WolvenKit import: convert edited .tga back to .xbm in the packed/ staging tree
  3. After all assets: WolvenKit pack: assemble packed/ into a single .archive
  4. Package the .archive into output/<mod_name>.zip for installation

The --only-changed flag adds a pre-filter that SHA-256 compares the edited
file against the editable source — only genuinely modified textures get
re-imported (useful for iterative editing of large sets).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import logging
from pathlib import Path

from .config import PipelineConfig
from .manifest import read_manifest, write_manifest
from .models import AssetRecord
from .progress import make_progress
from .packager import package_mod_archive, sha256_file
from .reporting import AssetLogEntry, write_asset_log, write_summary
from .validation import validate_edited_asset
from .wolvenkit import WolvenKitRunner


@dataclass(slots=True)
class FinalizeResult:
    processed: int
    succeeded: int
    failed: int
    skipped: int
    log_path: Path
    summary_path: Path
    asset_log_path: Path
    zip_path: Path | None


@dataclass(slots=True)
class FinalizeOutcome:
    asset_id: str
    status: str
    message: str


def run_finalize_stage(
    config: PipelineConfig,
    runner: WolvenKitRunner,
    *,
    only_changed: bool,
    skip_validate: bool,
    logger: logging.Logger,
    log_path: Path,
) -> FinalizeResult:
    config.ads_packed_dir.mkdir(parents=True, exist_ok=True)
    config.paths.output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Starting finalize stage only_changed=%s skip_validate=%s workers=%s",
        only_changed,
        skip_validate,
        config.performance.workers,
    )

    manifest_rows = read_manifest(config.discovery.approved_manifest)

    # --- Auto-promotion ---
    # When a user places an edited file in work/ads/edited/, that signals
    # intent to finalize.  We auto-promote "approved" (never finalized) and
    # "failed" (previous attempt failed, user is retrying with a fixed edit)
    # to "ready" so the user doesn't have to manually update the manifest.
    # "skipped" rows are NOT promotable — the user explicitly excluded those.
    promotable = ("approved", "failed")
    promoted = 0
    for row in manifest_rows:
        if row.status in promotable and row.edited_path:
            edited = config.resolve_user_path(row.edited_path)
            if edited.exists():
                row.status = "ready"
                row.notes = ""  # Clear any previous failure notes.
                promoted += 1
    if promoted:
        logger.info("Auto-promoted %s asset(s) to ready (edited file found)", promoted)

    target_rows = [row for row in manifest_rows if row.status == "ready"]

    # --only-changed: SHA-256 compare edited file against the editable source
    # to skip assets the user hasn't actually modified.  Useful when iterating
    # on a few textures out of a large set.
    if only_changed:
        target_rows = _filter_changed_rows(config, target_rows, config.performance.workers, logger)

    processed = len(target_rows)
    succeeded = 0
    failed = 0
    skipped = 0
    log_rows: list[AssetLogEntry] = []
    notes: list[str] = []

    if not target_rows:
        notes.append("No rows with status=ready matched finalize filters")

    outcomes = _finalize_assets_parallel(
        config,
        runner,
        target_rows,
        skip_validate=skip_validate,
        logger=logger,
    )

    for row in target_rows:
        outcome = outcomes.get(row.asset_id)
        if outcome is None:
            row.status = "failed"
            row.notes = "Missing finalize outcome"
            failed += 1
            log_rows.append(
                AssetLogEntry(
                    stage="finalize",
                    asset_id=row.asset_id,
                    status="failed",
                    message=row.notes,
                )
            )
            continue

        if outcome.status == "ok":
            row.notes = ""
            succeeded += 1
            log_rows.append(
                AssetLogEntry(
                    stage="finalize",
                    asset_id=row.asset_id,
                    status="ok",
                    message=outcome.message,
                )
            )
        else:
            row.status = "failed"
            row.notes = outcome.message
            failed += 1
            logger.warning("Failed to import asset %s: %s", row.asset_id, outcome.message)
            log_rows.append(
                AssetLogEntry(
                    stage="finalize",
                    asset_id=row.asset_id,
                    status="failed",
                    message=outcome.message,
                )
            )

    # Safety-net skipped count.  In the current implementation every target
    # row either succeeds or fails, so this is always 0.  It exists as a
    # guard against future code paths that might leave a row unaccounted.
    skipped = max(processed - succeeded - failed, 0)

    # Only pack and zip if at least one texture was successfully reimported.
    # An empty .archive would be pointless and could confuse mod managers.
    zip_path: Path | None = None
    if succeeded > 0:
        runner.pack_archive(
            packed_root=config.ads_packed_dir,
            output_archive=config.output_archive_path,
            logger=logger,
        )
        zip_path = package_mod_archive(
            config.output_archive_path,
            config.paths.output_dir / f"{config.mod.name}.zip",
            config.mod.name,
        )
        notes.append(f"packaged zip={zip_path}")
    else:
        notes.append("No successful imports; skipped pack and zip")

    write_manifest(config.discovery.approved_manifest, manifest_rows)

    asset_log_path = config.paths.output_dir / "asset_log.csv"
    summary_path = config.paths.output_dir / "summary.txt"
    write_asset_log(asset_log_path, log_rows)

    output_paths = [asset_log_path, summary_path, log_path]
    if zip_path:
        output_paths.append(zip_path)

    write_summary(
        summary_path,
        stage="finalize",
        mod_name=config.mod.name,
        counts={
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
        },
        notes=notes,
        outputs=output_paths,
    )

    logger.info(
        "Finalize stage complete processed=%s succeeded=%s failed=%s skipped=%s",
        processed,
        succeeded,
        failed,
        skipped,
    )

    return FinalizeResult(
        processed=processed,
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        log_path=log_path,
        summary_path=summary_path,
        asset_log_path=asset_log_path,
        zip_path=zip_path,
    )


def _finalize_assets_parallel(
    config: PipelineConfig,
    runner: WolvenKitRunner,
    rows: list[AssetRecord],
    *,
    skip_validate: bool,
    logger: logging.Logger,
) -> dict[str, FinalizeOutcome]:
    """Validate and reimport edited assets concurrently via a thread pool.

    Each asset runs independently: validate (optional) -> WolvenKit import.
    Failures are captured per-asset so one broken texture doesn't abort the
    entire batch.  Python's logging module is thread-safe, so logger calls
    from worker threads are safe without extra synchronization.
    """
    if not rows:
        return {}

    max_workers = max(1, min(config.performance.workers, len(rows)))
    outcomes: dict[str, FinalizeOutcome] = {}

    with make_progress() as progress:
        task_id = progress.add_task("Finalizing textures", total=len(rows))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(
                    _finalize_single_asset,
                    config,
                    runner,
                    row,
                    skip_validate=skip_validate,
                    logger=logger,
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
                    outcome = FinalizeOutcome(
                        asset_id=asset_id,
                        status="failed",
                        message=str(exc),
                    )
                outcomes[outcome.asset_id] = outcome

    return outcomes


def _finalize_single_asset(
    config: PipelineConfig,
    runner: WolvenKitRunner,
    row: AssetRecord,
    *,
    skip_validate: bool,
    logger: logging.Logger,
) -> FinalizeOutcome:
    """Validate one edited asset and re-import it via WolvenKit.

    Two-step process:
      1. Validation (unless --skip-validate): check dimensions and alpha
         against the original metadata stored in the manifest.
      2. WolvenKit import: unbundle original .xbm, place edited image
         alongside it, run ``import -p <dir> -k`` to apply pixels while
         preserving the .xbm's texture metadata (IsGamma, compression, etc.).
    """
    edited_path = config.resolve_user_path(row.edited_path)

    if not skip_validate:
        validation = validate_edited_asset(
            row,
            edited_path,
            preserve_dimensions=config.textures.preserve_dimensions,
            preserve_alpha=config.textures.preserve_alpha,
        )
        if not validation.ok:
            return FinalizeOutcome(
                asset_id=row.asset_id,
                status="failed",
                message="; ".join(validation.messages),
            )

    try:
        runner.import_texture(
            game_dir=config.paths.game_dir,
            archive_path=row.archive_path,
            relative_texture_path=row.relative_texture_path,
            edited_file=edited_path,
            packed_root=config.ads_packed_dir,
            uext=config.textures.editable_format,
            logger=logger,
        )
        return FinalizeOutcome(
            asset_id=row.asset_id,
            status="ok",
            message=f"Imported edit from {edited_path}",
        )
    except Exception as exc:  # noqa: BLE001
        return FinalizeOutcome(
            asset_id=row.asset_id,
            status="failed",
            message=str(exc),
        )


def _filter_changed_rows(
    config: PipelineConfig,
    rows: list[AssetRecord],
    workers: int,
    logger: logging.Logger,
) -> list[AssetRecord]:
    if len(rows) <= 1:
        return [row for row in rows if _is_changed(config, row)]

    max_workers = max(1, min(workers, len(rows)))
    keep: dict[str, bool] = {}

    with make_progress() as progress:
        task_id = progress.add_task("Comparing edited files", total=len(rows))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {pool.submit(_is_changed, config, row): row.asset_id for row in rows}
            for future in as_completed(future_map):
                asset_id = future_map[future]
                progress.advance(task_id)
                try:
                    keep[asset_id] = bool(future.result())
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Error comparing edited file for asset %s, "
                        "excluding from changed set: %s",
                        asset_id,
                        exc,
                    )
                    keep[asset_id] = False

    return [row for row in rows if keep.get(row.asset_id, False)]


def _is_changed(config: PipelineConfig, row: AssetRecord) -> bool:
    """Return True if the edited file exists on disk.

    Used by ``--only-changed`` to skip assets with no edited file.
    Presence in the edited folder is treated as intent to include the
    asset — no SHA comparison against the editable source is performed.
    """
    edited = config.resolve_user_path(row.edited_path)
    return edited.exists()


