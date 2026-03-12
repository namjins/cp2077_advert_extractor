from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import shutil

from rich.progress import track

from .config import PipelineConfig
from .discovery import (
    discover_candidate_assets,
    merge_candidates_into_manifest,
    write_candidate_report,
)
from .io_utils import ensure_dir
from .manifest import read_manifest, write_manifest
from .models import AssetRecord
from .reporting import AssetLogEntry, write_asset_log, write_summary
from .validation import inspect_image
from .wolvenkit import WolvenKitRunner


@dataclass(slots=True)
class StageResult:
    processed: int
    succeeded: int
    failed: int
    skipped: int
    log_path: Path
    summary_path: Path
    asset_log_path: Path
    notes: list[str]


def run_discovery_stage(
    config: PipelineConfig,
    runner: WolvenKitRunner,
    *,
    report_only: bool,
    logger: logging.Logger,
) -> StageResult:
    ensure_dir(config.paths.work_dir)
    ensure_dir(config.paths.output_dir)

    logger.info("Starting discovery stage (mode=%s)", config.discovery.mode)
    candidates = discover_candidate_assets(config.paths.game_dir, runner, logger)
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
        log_path=Path(""),
        summary_path=summary_path,
        asset_log_path=asset_log_path,
        notes=notes,
    )


def run_extract_stage(
    config: PipelineConfig,
    runner: WolvenKitRunner,
    *,
    discover: bool,
    skip_extract: bool,
    clean: bool,
    logger: logging.Logger,
    log_path: Path,
) -> StageResult:
    logger.info(
        "Starting extract stage discover=%s skip_extract=%s clean=%s",
        discover,
        skip_extract,
        clean,
    )

    if clean and config.ads_root_dir.exists():
        shutil.rmtree(config.ads_root_dir)

    ensure_dir(config.ads_original_dir)
    ensure_dir(config.ads_editable_dir)
    ensure_dir(config.ads_edited_dir)
    ensure_dir(config.ads_packed_dir)
    ensure_dir(config.paths.output_dir)

    notes: list[str] = []

    if discover:
        discovery_result = run_discovery_stage(
            config,
            runner,
            report_only=False,
            logger=logger,
        )
        notes.extend(discovery_result.notes)

    manifest_rows = read_manifest(config.discovery.approved_manifest)
    if not manifest_rows:
        raise ValueError(
            "Approved manifest is empty. Run discover-assets first or provide approved rows."
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

    for row in track(approved, description="Exporting textures"):
        original_path = config.ads_original_dir / f"{row.asset_id}.{config.textures.editable_format}"
        editable_path = config.resolve_user_path(row.editable_source_path)

        if skip_extract:
            skipped += 1
            log_rows.append(
                AssetLogEntry(
                    stage="extract",
                    asset_id=row.asset_id,
                    status="skipped",
                    message="Skipped export due to --skip-extract",
                )
            )
            continue

        try:
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
            row.width = image.width
            row.height = image.height
            row.has_alpha = image.has_alpha
            if row.status != "ready":
                row.status = "approved"
            succeeded += 1
            log_rows.append(
                AssetLogEntry(
                    stage="extract",
                    asset_id=row.asset_id,
                    status="ok",
                    message=f"Exported editable source to {editable_path}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            row.status = "failed"
            row.notes = str(exc)
            failed += 1
            logger.warning("Failed to export asset %s: %s", row.asset_id, exc)
            log_rows.append(
                AssetLogEntry(
                    stage="extract",
                    asset_id=row.asset_id,
                    status="failed",
                    message=str(exc),
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


def _ensure_edit_paths(config: PipelineConfig, row: AssetRecord) -> None:
    if not row.editable_source_path:
        row.editable_source_path = config.make_relative(
            config.ads_editable_dir / f"{row.asset_id}.{config.textures.editable_format}"
        )
    if not row.edited_path:
        row.edited_path = config.make_relative(
            config.ads_edited_dir / f"{row.asset_id}.{config.textures.editable_format}"
        )
