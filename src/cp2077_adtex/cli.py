"""CLI entry point — Typer commands for each pipeline stage.

Commands:
  extract          — discover + export approved textures to editable format
  discover-assets  — scan archives for ad texture candidates
  validate-list    — cross-reference a research file against the manifest
  finalize         — validate edits, re-import, pack archive, produce zip
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from .config import ConfigError, load_config
from .extractor import run_discovery_stage, run_extract_stage
from .finalizer import run_finalize_stage
from .list_validator import run_validate_list_stage
from .logging_utils import setup_pipeline_logger
from .wolvenkit import WolvenKitRunner

app = typer.Typer(help="Cyberpunk 2077 advertisement texture extraction/finalize pipeline")
console = Console()


@app.command("extract")
def extract_cmd(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    discover: bool = typer.Option(False, "--discover", help="Run discovery before extraction"),
    all_known_roots: bool = typer.Option(
        False,
        "--all-known-roots",
        help=(
            "Populate and approve manifest from known ad roots in base+ep1 archives "
            "before extraction"
        ),
    ),
    skip_extract: bool = typer.Option(
        False,
        "--skip-extract",
        help="Skip the export step and only refresh logs/manifest metadata",
    ),
    clean: bool = typer.Option(
        False,
        "--clean",
        help="Delete work/ads and rebuild extract outputs (will prompt if edited/ has files)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip confirmation prompts (e.g. when --clean would delete edited files)",
    ),
) -> None:
    """Extract approved assets and generate editable files."""
    try:
        cfg = load_config(config)

        # Guard against accidental deletion of user-edited textures
        if clean and not force and cfg.ads_edited_dir.exists():
            edited_files = list(cfg.ads_edited_dir.iterdir())
            if edited_files:
                console.print(
                    f"[yellow]warning:[/yellow] --clean will delete {len(edited_files)} "
                    f"file(s) in {cfg.ads_edited_dir}"
                )
                if not typer.confirm("Continue?"):
                    raise typer.Abort()

        logger, log_path = setup_pipeline_logger(cfg.paths.output_dir, stage="extract")
        runner = WolvenKitRunner(cfg.wolvenkit.cli_path)
        result = run_extract_stage(
            cfg,
            runner,
            discover=discover,
            all_known_roots=all_known_roots,
            skip_extract=skip_extract,
            clean=clean,
            logger=logger,
            log_path=log_path,
        )
        console.print(
            "extract complete: "
            f"processed={result.processed} succeeded={result.succeeded} "
            f"failed={result.failed} skipped={result.skipped}"
        )
        console.print(f"summary: {result.summary_path}")
        console.print(f"asset log: {result.asset_log_path}")
        console.print(f"pipeline log: {result.log_path}")
    except (ConfigError, ValueError, RuntimeError) as exc:
        raise typer.Exit(code=_print_error(str(exc))) from exc


@app.command("discover-assets")
def discover_assets_cmd(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    report_only: bool = typer.Option(
        False,
        "--report-only",
        help="Only emit candidate report; do not update approved manifest",
    ),
) -> None:
    """Discover ad texture candidates using hybrid heuristics."""
    try:
        cfg = load_config(config)
        logger, log_path = setup_pipeline_logger(cfg.paths.output_dir, stage="discover")
        runner = WolvenKitRunner(cfg.wolvenkit.cli_path)
        result = run_discovery_stage(
            cfg,
            runner,
            report_only=report_only,
            logger=logger,
            log_path=log_path,
        )
        console.print(f"discover complete: candidates={result.processed}")
        console.print(f"candidate report: {cfg.discovery.candidate_report}")
        console.print(f"manifest: {cfg.discovery.approved_manifest}")
        console.print(f"pipeline log: {log_path}")
    except (ConfigError, ValueError, RuntimeError) as exc:
        raise typer.Exit(code=_print_error(str(exc))) from exc


@app.command("validate-list")
def validate_list_cmd(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    research_file: Path = typer.Option(..., "--research-file", exists=True, readable=True),
) -> None:
    """Validate a research list against extracted manifest assets."""
    try:
        cfg = load_config(config)
        logger, log_path = setup_pipeline_logger(cfg.paths.output_dir, stage="validate_list")
        result = run_validate_list_stage(
            cfg,
            research_file=research_file,
            logger=logger,
            log_path=log_path,
        )
        console.print(
            "validate-list complete: "
            f"processed={result.processed} matched={result.matched} "
            f"missing_in_extract={result.missing_in_extract} "
            f"archive_mismatch={result.archive_mismatch} "
            f"unparseable={result.unparseable}"
        )
        console.print(f"report: {result.csv_path}")
        console.print(f"summary: {result.summary_path}")
        console.print(f"pipeline log: {result.log_path}")
    except (ConfigError, ValueError, RuntimeError) as exc:
        raise typer.Exit(code=_print_error(str(exc))) from exc


@app.command("finalize")
def finalize_cmd(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    only_changed: bool = typer.Option(
        False,
        "--only-changed",
        help="Process only ready assets whose edited file differs from editable source",
    ),
    skip_validate: bool = typer.Option(
        False,
        "--skip-validate",
        help="Skip image validation checks during finalize",
    ),
) -> None:
    """Validate edited assets, import, pack archive, and output installable zip."""
    try:
        cfg = load_config(config)
        logger, log_path = setup_pipeline_logger(cfg.paths.output_dir, stage="finalize")
        runner = WolvenKitRunner(cfg.wolvenkit.cli_path)
        result = run_finalize_stage(
            cfg,
            runner,
            only_changed=only_changed,
            skip_validate=skip_validate,
            logger=logger,
            log_path=log_path,
        )
        console.print(
            "finalize complete: "
            f"processed={result.processed} succeeded={result.succeeded} "
            f"failed={result.failed} skipped={result.skipped}"
        )
        if result.zip_path:
            console.print(f"mod zip: {result.zip_path}")
        console.print(f"summary: {result.summary_path}")
        console.print(f"asset log: {result.asset_log_path}")
        console.print(f"pipeline log: {result.log_path}")
    except (ConfigError, ValueError, RuntimeError) as exc:
        raise typer.Exit(code=_print_error(str(exc))) from exc


def _print_error(message: str) -> int:
    console.print(f"[red]error:[/red] {message}")
    return 1


if __name__ == "__main__":
    app()
