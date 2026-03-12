from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from .config import ConfigError, load_config
from .extractor import run_discovery_stage, run_extract_stage
from .finalizer import run_finalize_stage
from .logging_utils import setup_pipeline_logger
from .wolvenkit import WolvenKitRunner

app = typer.Typer(help="Cyberpunk 2077 advertisement texture extraction/finalize pipeline")
console = Console()


@app.command("extract")
def extract_cmd(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    discover: bool = typer.Option(False, "--discover", help="Run discovery before extraction"),
    skip_extract: bool = typer.Option(
        False,
        "--skip-extract",
        help="Skip the export step and only refresh logs/manifest metadata",
    ),
    clean: bool = typer.Option(
        False,
        "--clean",
        help="Delete work/ads and rebuild extract outputs",
    ),
) -> None:
    """Extract approved assets and generate editable files."""
    try:
        cfg = load_config(config)
        logger, log_path = setup_pipeline_logger(cfg.paths.output_dir, stage="extract")
        runner = WolvenKitRunner(cfg.wolvenkit.cli_path)
        result = run_extract_stage(
            cfg,
            runner,
            discover=discover,
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
        )
        console.print(f"discover complete: candidates={result.processed}")
        console.print(f"candidate report: {cfg.discovery.candidate_report}")
        console.print(f"manifest: {cfg.discovery.approved_manifest}")
        console.print(f"pipeline log: {log_path}")
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
