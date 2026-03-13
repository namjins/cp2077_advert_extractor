import logging
from pathlib import Path
import zipfile

from PIL import Image

from cp2077_adtex.config import load_config
from cp2077_adtex.extractor import run_extract_stage
from cp2077_adtex.finalizer import run_finalize_stage
from cp2077_adtex.manifest import read_manifest, write_manifest
from cp2077_adtex.models import AssetRecord


class FakeRunner:
    def export_texture(
        self,
        *,
        game_dir: Path,
        archive_path: str,
        relative_texture_path: str,
        output_file: Path,
        uext: str,
        logger: logging.Logger,
    ) -> None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGBA", (64, 64), color=(255, 0, 0, 255))
        image.save(output_file)

    def import_texture(
        self,
        *,
        game_dir: Path,
        archive_path: str,
        relative_texture_path: str,
        edited_file: Path,
        packed_root: Path,
        uext: str,
        logger: logging.Logger,
    ) -> None:
        destination = packed_root / relative_texture_path.replace("/", "_")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(edited_file.read_bytes())

    def pack_archive(
        self,
        *,
        packed_root: Path,
        output_archive: Path,
        logger: logging.Logger,
    ) -> None:
        output_archive.parent.mkdir(parents=True, exist_ok=True)
        output_archive.write_bytes(b"archive-data")


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[wolvenkit]
cli_path = "./fake_wk.exe"

[paths]
game_dir = "./game"
work_dir = "./work"
output_dir = "./output"

[mod]
name = "ads_test_mod"
version = "0.1.0"
description = "test"

[textures]
editable_format = "tga"
preserve_dimensions = true
preserve_alpha = true

[discovery]
mode = "hybrid"
approved_manifest = "./assets_manifest.csv"
candidate_report = "./candidate_assets.csv"

[performance]
workers = 1
""".strip(),
        encoding="utf-8",
    )
    return config_path


def test_extract_then_finalize_flow(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    cfg = load_config(config_path)

    cfg.paths.game_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows = [
        AssetRecord(
            asset_id="asset001",
            archive_path="archive/pc/content/basegame_1.archive",
            relative_texture_path="base/textures/ad_screen_01.xbm",
            editable_source_path="",
            edited_path="",
            width=0,
            height=0,
            has_alpha=False,
            status="approved",
            notes="",
        )
    ]
    write_manifest(cfg.discovery.approved_manifest, manifest_rows)

    logger = logging.getLogger("test.pipeline")
    logger.handlers = []
    logger.addHandler(logging.NullHandler())

    runner = FakeRunner()

    extract_result = run_extract_stage(
        cfg,
        runner,
        discover=False,
        all_known_roots=False,
        skip_extract=False,
        clean=False,
        logger=logger,
        log_path=cfg.paths.output_dir / "pipeline_extract.log",
    )

    assert extract_result.succeeded == 1

    updated = read_manifest(cfg.discovery.approved_manifest)
    row = updated[0]
    row.status = "ready"

    editable = cfg.resolve_user_path(row.editable_source_path)
    edited = cfg.resolve_user_path(row.edited_path)
    edited.parent.mkdir(parents=True, exist_ok=True)

    image = Image.open(editable)
    image.putpixel((0, 0), (0, 255, 0, 255))
    image.save(edited)

    write_manifest(cfg.discovery.approved_manifest, [row])

    finalize_result = run_finalize_stage(
        cfg,
        runner,
        only_changed=False,
        skip_validate=False,
        logger=logger,
        log_path=cfg.paths.output_dir / "pipeline_finalize.log",
    )

    assert finalize_result.succeeded == 1
    assert finalize_result.zip_path is not None
    assert finalize_result.zip_path.exists()

    with zipfile.ZipFile(finalize_result.zip_path, "r") as handle:
        names = set(handle.namelist())

    assert "archive/pc/mod/ads_test_mod.archive" in names



