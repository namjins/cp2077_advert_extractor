from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from rich.progress import track

from .config import PipelineConfig
from .manifest import read_manifest, write_manifest
from .models import AssetRecord
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

    manifest_rows = read_manifest(config.discovery.approved_manifest)
    target_rows = [row for row in manifest_rows if row.status == "ready"]

    if only_changed:
        target_rows = [row for row in target_rows if _is_changed(config, row)]

    processed = len(target_rows)
    succeeded = 0
    failed = 0
    skipped = 0
    log_rows: list[AssetLogEntry] = []
    notes: list[str] = []

    if not target_rows:
        notes.append("No rows with status=ready matched finalize filters")

    for row in track(target_rows, description="Finalizing textures"):
        edited_path = config.resolve_user_path(row.edited_path)

        if not skip_validate:
            validation = validate_edited_asset(
                row,
                edited_path,
                preserve_dimensions=config.textures.preserve_dimensions,
                preserve_alpha=config.textures.preserve_alpha,
            )
            if not validation.ok:
                row.status = "failed"
                row.notes = "; ".join(validation.messages)
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
            succeeded += 1
            log_rows.append(
                AssetLogEntry(
                    stage="finalize",
                    asset_id=row.asset_id,
                    status="ok",
                    message=f"Imported edit from {edited_path}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            row.status = "failed"
            row.notes = str(exc)
            failed += 1
            logger.warning("Failed to import asset %s: %s", row.asset_id, exc)
            log_rows.append(
                AssetLogEntry(
                    stage="finalize",
                    asset_id=row.asset_id,
                    status="failed",
                    message=str(exc),
                )
            )

    skipped = max(processed - succeeded - failed, 0)

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


def _is_changed(config: PipelineConfig, row: AssetRecord) -> bool:
    edited = config.resolve_user_path(row.edited_path)
    source = config.resolve_user_path(row.editable_source_path)

    if not edited.exists():
        return False
    if not source.exists():
        return True

    return sha256_file(edited) != sha256_file(source)
