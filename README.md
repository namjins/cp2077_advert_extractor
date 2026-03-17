# cp2077_ad_texture_pipeline

Config-driven Python CLI pipeline for extracting and repackaging Cyberpunk 2077 in-world advertisement textures.

## What It Does

- Discovers candidate ad textures from base game + Phantom Liberty archives
- Supports one-shot bulk extraction from known ad roots
- Validates external research lists against extracted assets
- Exports approved assets to editable `.tga` files
- Auto-promotes assets to `ready` when an edited file is found in `work/ads/edited/`
- Validates edited files (file readability, dimensions, alpha)
- Reimports valid edits into a packed staging tree
- Builds an archive mod and packages `output/<mod_name>.zip`
- Uses worker-based parallelism and rich progress bars for discovery/extract/finalize

## CLI

```powershell
python -m cp2077_adtex extract         --config config.toml [--discover] [--all-known-roots] [--skip-extract] [--clean] [--force]
python -m cp2077_adtex discover-assets --config config.toml [--report-only]
python -m cp2077_adtex validate-list   --config config.toml --research-file path/to/report.md
python -m cp2077_adtex finalize        --config config.toml [--only-changed] [--skip-validate]
```

## Installation

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
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

`[performance].workers` controls concurrency for archive scans, extraction, and finalize import/comparison work.

## Standard Workflow

1. Fresh run — discover, approve, and extract all known ad roots:

```powershell
.venv\Scripts\Activate.ps1; python -m cp2077_adtex extract --config config.toml --all-known-roots --clean --force
```

2. Edit files from `work/ads/editable/` (e.g. `rayfield_720p.tga`) and place your finished edits in `work/ads/edited/`.

3. Finalize — assets with a file in `edited/` are promoted to `ready` automatically:

```powershell
.venv\Scripts\Activate.ps1; python -m cp2077_adtex finalize --config config.toml --only-changed
```

## Validate Research List

To compare a research markdown report against extracted assets:

```powershell
.venv\Scripts\Activate.ps1; python -m cp2077_adtex validate-list --config config.toml --research-file path\to\report.md
```

This produces:

- `output/list_validation.csv` with row-level statuses (`matched`, `missing_in_extract`, `archive_mismatch`, `unparseable`)
- `output/list_validation_summary.txt` with totals and top warnings

## Discovery Coverage

Prioritized archives when present:

- `archive/pc/content/basegame_3_nightcity.archive`
- `archive/pc/content/basegame_4_gamedata.archive`
- `archive/pc/ep1/ep1_1_nightcity.archive`
- `archive/pc/ep1/ep1_2_gamedata.archive`

Known asset roots:

- `base\\environment\\decoration\\advertising\\...\\*.xbm`
- `base\\gameplay\\gui\\world\\adverts\\...\\*.xbm`
- proxy ad textures under `base\\worlds\\...\\proxy\\...advert...\\*.xbm`

## Output Contracts

- `work/ads/original/` extracted originals (named by asset_id hash)
- `work/ads/editable/` editable exports with human-readable names (e.g. `rayfield_720p.tga`)
- `work/ads/edited/` user-supplied edits (same naming as `editable/`)
- `work/ads/packed/` import staging tree

Editable and edited filenames are derived from the in-archive texture path.
When two textures share the same filename stem (e.g. two different `banner_d.xbm` from different directories), the parent directory is prepended (`signage__banner_d.tga`).
The manifest (`assets_manifest.csv`) maps each friendly filename back to the correct archive path for repacking.

- `output/<mod_name>.zip` with `archive/pc/mod/<mod_name>.archive`
- `output/asset_log.csv`
- `output/summary.txt`
- `output/pipeline_<timestamp>.log`
- `output/list_validation.csv`
- `output/list_validation_summary.txt`

## Troubleshooting

- `WolvenKit CLI not found`: verify `[wolvenkit].cli_path`
- Empty manifest: run `extract --all-known-roots`, run discovery, or provide manifest rows
- Dimension/alpha validation failures: re-export and preserve required metadata
- Pack skipped: no assets with `status=ready` imported successfully
