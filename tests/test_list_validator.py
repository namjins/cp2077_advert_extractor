from __future__ import annotations

import logging
from pathlib import Path

from cp2077_adtex.config import load_config
from cp2077_adtex.list_validator import (
    STATUS_ARCHIVE_MISMATCH,
    STATUS_MATCHED,
    STATUS_MISSING,
    STATUS_UNPARSEABLE,
    ResearchPathRow,
    compare_research_paths,
    parse_research_markdown,
    run_validate_list_stage,
)
from cp2077_adtex.manifest import read_manifest, write_manifest
from cp2077_adtex.models import AssetRecord


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
workers = 2
""".strip(),
        encoding="utf-8",
    )
    return config_path


def test_parse_research_markdown_extracts_normalizes_and_dedupes(tmp_path: Path) -> None:
    research_file = tmp_path / "research.md"
    research_file.write_text(
        "\n".join(
            [
                "archive\\pc\\content\\basegame_4_gamedata.archive : base\\gameplay\\gui\\world\\adverts\\rayfield\\rayfield_720p.xbm",
                "duplicate entry base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm",
                "regex-ish token base\\gameplay\\gui\\world\\adverts\\konpeki\\konpeki_1080p\\.xbm",
                "this line only says .xbm and should be unparseable",
            ]
        ),
        encoding="utf-8",
    )

    rows = parse_research_markdown(research_file)

    assert len(rows) == 3

    normalized_paths = {row.normalized_relative_texture_path for row in rows if not row.parse_error}
    assert "base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm" in normalized_paths
    assert "base/gameplay/gui/world/adverts/konpeki/konpeki_1080p.xbm" in normalized_paths

    rayfield_row = next(
        row
        for row in rows
        if row.normalized_relative_texture_path.endswith("rayfield_720p.xbm")
    )
    assert rayfield_row.research_archive_path == "archive/pc/content/basegame_4_gamedata.archive"

    assert sum(1 for row in rows if row.parse_error) == 1


def test_compare_research_paths_statuses() -> None:
    manifest_rows = [
        AssetRecord(
            asset_id="a1",
            archive_path="archive/pc/content/basegame_4_gamedata.archive",
            relative_texture_path="base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm",
            editable_source_path="",
            edited_path="",
            width=0,
            height=0,
            has_alpha=False,
            status="approved",
            notes="",
        ),
        AssetRecord(
            asset_id="a2",
            archive_path="archive/pc/content/basegame_3_nightcity.archive",
            relative_texture_path="base/environment/decoration/advertising/signage/textures/signage_kabuki_hotel_a.xbm",
            editable_source_path="",
            edited_path="",
            width=0,
            height=0,
            has_alpha=False,
            status="approved",
            notes="",
        ),
    ]

    research_rows = [
        ResearchPathRow(
            line_number=1,
            raw_text="ok",
            research_archive_path="",
            research_relative_texture_path="base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm",
            normalized_relative_texture_path="base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm",
        ),
        ResearchPathRow(
            line_number=2,
            raw_text="archive mismatch",
            research_archive_path="archive/pc/ep1/ep1_1_nightcity.archive",
            research_relative_texture_path="base/environment/decoration/advertising/signage/textures/signage_kabuki_hotel_a.xbm",
            normalized_relative_texture_path="base/environment/decoration/advertising/signage/textures/signage_kabuki_hotel_a.xbm",
        ),
        ResearchPathRow(
            line_number=3,
            raw_text="missing",
            research_archive_path="",
            research_relative_texture_path="base/gameplay/gui/world/adverts/missing/missing.xbm",
            normalized_relative_texture_path="base/gameplay/gui/world/adverts/missing/missing.xbm",
        ),
        ResearchPathRow(
            line_number=4,
            raw_text="bad",
            research_archive_path="",
            research_relative_texture_path="",
            normalized_relative_texture_path="",
            parse_error="Unable to normalize extracted texture path",
        ),
    ]

    compared = compare_research_paths(research_rows, manifest_rows)
    statuses = [row.status for row in compared]

    assert statuses.count(STATUS_MATCHED) == 1
    assert statuses.count(STATUS_ARCHIVE_MISMATCH) == 1
    assert statuses.count(STATUS_MISSING) == 1
    assert statuses.count(STATUS_UNPARSEABLE) == 1


def test_run_validate_list_stage_writes_outputs_and_is_deterministic(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    cfg = load_config(config_path)

    manifest_rows = [
        AssetRecord(
            asset_id="a1",
            archive_path="archive/pc/content/basegame_4_gamedata.archive",
            relative_texture_path="base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm",
            editable_source_path="work/ads/editable/a1.tga",
            edited_path="work/ads/edited/a1.tga",
            width=0,
            height=0,
            has_alpha=False,
            status="approved",
            notes="",
        ),
        AssetRecord(
            asset_id="a2",
            archive_path="archive/pc/content/basegame_3_nightcity.archive",
            relative_texture_path="base/environment/decoration/advertising/signage/textures/signage_kabuki_hotel_a.xbm",
            editable_source_path="work/ads/editable/a2.tga",
            edited_path="work/ads/edited/a2.tga",
            width=0,
            height=0,
            has_alpha=False,
            status="approved",
            notes="",
        ),
    ]
    write_manifest(cfg.discovery.approved_manifest, manifest_rows)

    before_manifest = [row.to_row() for row in read_manifest(cfg.discovery.approved_manifest)]

    research_file = tmp_path / "research.md"
    research_file.write_text(
        "\n".join(
            [
                "archive/pc/content/basegame_4_gamedata.archive base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm",
                "archive/pc/ep1/ep1_1_nightcity.archive base/environment/decoration/advertising/signage/textures/signage_kabuki_hotel_a.xbm",
                "base/gameplay/gui/world/adverts/missing/missing_asset.xbm",
                "text with .xbm but not a valid path",
            ]
        ),
        encoding="utf-8",
    )

    logger = logging.getLogger("test.list_validation")
    logger.handlers = []
    logger.addHandler(logging.NullHandler())

    result = run_validate_list_stage(
        cfg,
        research_file=research_file,
        logger=logger,
        log_path=cfg.paths.output_dir / "pipeline_validate.log",
    )

    assert result.processed == 4
    assert result.matched == 1
    assert result.archive_mismatch == 1
    assert result.missing_in_extract == 1
    assert result.unparseable == 1
    assert result.csv_path.exists()
    assert result.summary_path.exists()

    csv_first = result.csv_path.read_text(encoding="utf-8")
    summary_text = result.summary_path.read_text(encoding="utf-8")
    assert "archive_mismatch=1" in summary_text
    assert "unparseable=1" in summary_text

    result_repeat = run_validate_list_stage(
        cfg,
        research_file=research_file,
        logger=logger,
        log_path=cfg.paths.output_dir / "pipeline_validate_second.log",
    )
    csv_second = result_repeat.csv_path.read_text(encoding="utf-8")
    assert csv_first == csv_second

    after_manifest = [row.to_row() for row in read_manifest(cfg.discovery.approved_manifest)]
    assert before_manifest == after_manifest
