# cp2077_ad_texture_pipeline

Config-driven Python CLI pipeline for extracting and repackaging Cyberpunk 2077 in-world advertisement textures.

## What It Does

- Discovers candidate ad textures by scanning high-value base game and Phantom Liberty archives with WolvenKit `archiveinfo` (`hybrid` mode)
- Exports approved assets to editable `.tga` files
- Validates edited files (file readability, dimensions, alpha)
- Reimports valid edits into a packed staging tree
- Builds an archive mod and packages `output/<mod_name>.zip`
- Emits resumable logs and CSV/text audit outputs

## CLI

```bash
cp2077-adtex extract --config config.toml [--discover] [--skip-extract] [--clean]
cp2077-adtex discover-assets --config config.toml [--report-only]
cp2077-adtex finalize --config config.toml [--only-changed] [--skip-validate]
```

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .[dev]
```

## Configuration

Copy `config.toml.example` to `config.toml` and update paths.

Required sections:

- `[wolvenkit]` `cli_path`
- `[paths]` `game_dir`, `work_dir`, `output_dir`
- `[mod]` `name`, `version`, `description`
- `[textures]` `editable_format="tga"`, `preserve_dimensions`, `preserve_alpha`
- `[discovery]` `mode="hybrid"`, `approved_manifest`, `candidate_report`
- `[performance]` `workers`

Path values resolve relative to the config file location.

## Manifest Schema

`assets_manifest.csv` columns:

1. `asset_id`
2. `archive_path`
3. `relative_texture_path`
4. `editable_source_path`
5. `edited_path`
6. `width`
7. `height`
8. `has_alpha`
9. `status` (`approved|skipped|failed|ready`)
10. `notes`

## Workflow

1. Discover candidates:

```powershell
.\.venv\Scripts\python -m cp2077_adtex.cli discover-assets --config config.toml
```

2. Open `assets_manifest.csv` and set selected rows to `status=approved`.
3. Export editable assets:

```powershell
.\.venv\Scripts\python -m cp2077_adtex.cli extract --config config.toml
```

4. Edit files in `work/ads/editable/` and place final edits in `work/ads/edited/`.
5. Mark edited rows as `status=ready` in `assets_manifest.csv`.
6. Finalize package:

```powershell
.\.venv\Scripts\python -m cp2077_adtex.cli finalize --config config.toml
```

## Discovery Coverage

By default, discovery prioritizes these archives when present:

- `archive/pc/content/basegame_3_nightcity.archive`
- `archive/pc/content/basegame_4_gamedata.archive`
- `archive/pc/ep1/ep1_1_nightcity.archive`
- `archive/pc/ep1/ep1_2_gamedata.archive`

Regex targets include:

- `base\\environment\\decoration\\advertising\\...\\.xbm`
- `base\\gameplay\\gui\\world\\adverts\\...\\.xbm`
- proxy ad textures under `base\\worlds\\...\\proxy\\...advert...\\.xbm`

## Output Contracts

- `work/ads/original/` extracted originals
- `work/ads/editable/` editable exports (`.tga` by default)
- `work/ads/edited/` user-supplied edits
- `work/ads/packed/` import staging tree
- `output/<mod_name>.zip` with `archive/pc/mod/<mod_name>.archive`
- `output/asset_log.csv`
- `output/summary.txt`
- `output/pipeline_<timestamp>.log`

## Notes on WolvenKit CLI

This pipeline wraps WolvenKit CLI and uses:

- `archiveinfo --list` for candidate discovery
- `unbundle` + `export --uext` for extraction/export
- `import` for edited texture conversion back to `.xbm`
- `pack` for archive generation

If your installed WolvenKit CLI changes flags in a future version, update [src/cp2077_adtex/wolvenkit.py](src/cp2077_adtex/wolvenkit.py).

## Troubleshooting

- `WolvenKit CLI not found`: verify `[wolvenkit].cli_path`
- `discover-assets` takes time: it scans archive contents and can exceed a couple minutes on some installs
- `Approved manifest is empty`: run discovery or populate `assets_manifest.csv`
- Dimension/alpha validation failures: re-export and ensure edited files preserve required image metadata
- Pack skipped: no assets with `status=ready` imported successfully
