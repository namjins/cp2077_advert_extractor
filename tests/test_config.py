from pathlib import Path

from cp2077_adtex.config import load_config


def test_load_config_resolves_relative_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[wolvenkit]
cli_path = "./tools/WolvenKit.CLI.exe"

[paths]
game_dir = "./game"
work_dir = "./work"
output_dir = "./output"

[mod]
name = "ad_mod"
version = "1.0.0"
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

    cfg = load_config(config_path)

    assert cfg.wolvenkit.cli_path == (tmp_path / "tools" / "WolvenKit.CLI.exe").resolve()
    assert cfg.paths.work_dir == (tmp_path / "work").resolve()
    assert cfg.discovery.approved_manifest == (tmp_path / "assets_manifest.csv").resolve()
    assert cfg.textures.editable_format == "tga"
