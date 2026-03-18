"""Configuration loading and validation for the ad texture pipeline.

Reads config.toml and produces a fully-resolved PipelineConfig object.
All relative paths in the TOML are resolved against the directory containing
config.toml (base_dir), so the project can live anywhere on disk.

Work directory layout managed by PipelineConfig properties:
    work/ads/original/   — raw .xbm exports from WolvenKit (untouched copies)
    work/ads/editable/   — converted .tga/.png files the user can open in an editor
    work/ads/edited/     — user places modified versions here
    work/ads/packed/     — WolvenKit import staging tree for re-packing
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    """Raised when config.toml is invalid or missing required fields."""


@dataclass(slots=True)
class WolvenKitConfig:
    cli_path: Path


@dataclass(slots=True)
class PathsConfig:
    game_dir: Path
    work_dir: Path
    output_dir: Path


@dataclass(slots=True)
class ModConfig:
    name: str
    version: str
    description: str


@dataclass(slots=True)
class TexturesConfig:
    editable_format: str
    preserve_dimensions: bool
    preserve_alpha: bool


@dataclass(slots=True)
class DiscoveryConfig:
    mode: str
    approved_manifest: Path
    candidate_report: Path


@dataclass(slots=True)
class PerformanceConfig:
    workers: int


@dataclass(slots=True)
class PipelineConfig:
    """Top-level config — aggregates all sections and provides derived paths."""
    config_path: Path
    base_dir: Path
    wolvenkit: WolvenKitConfig
    paths: PathsConfig
    mod: ModConfig
    textures: TexturesConfig
    discovery: DiscoveryConfig
    performance: PerformanceConfig

    @property
    def ads_root_dir(self) -> Path:
        return self.paths.work_dir / "ads"

    @property
    def ads_original_dir(self) -> Path:
        return self.ads_root_dir / "original"

    @property
    def ads_editable_dir(self) -> Path:
        return self.ads_root_dir / "editable"

    @property
    def ads_edited_dir(self) -> Path:
        return self.ads_root_dir / "edited"

    @property
    def ads_packed_dir(self) -> Path:
        return self.ads_root_dir / "packed"

    @property
    def output_archive_path(self) -> Path:
        return self.paths.output_dir / "archive" / "pc" / "mod" / f"{self.mod.name}.archive"

    def resolve_user_path(self, raw: str | Path) -> Path:
        """Resolve a user-facing path (from manifest or config) to an absolute Path.

        Relative paths are resolved against base_dir (the directory containing
        config.toml), so the project is fully portable — move the directory
        and all relative paths still work.
        """
        path = Path(raw)
        if path.is_absolute():
            return path
        return (self.base_dir / path).resolve()

    def make_relative(self, path: Path) -> str:
        """Convert an absolute path to a posix-style string relative to base_dir.

        Used when storing paths in the manifest CSV — keeps the manifest
        portable across machines with different absolute prefixes.
        Falls back to the absolute path string if *path* is outside base_dir
        (e.g. on a different drive on Windows).
        """
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.base_dir.resolve()).as_posix()
        except ValueError:
            return str(resolved)


def load_config(config_path: str | Path) -> PipelineConfig:
    config_file = Path(config_path).resolve()
    if not config_file.exists():
        raise ConfigError(f"Config file not found: {config_file}")

    with config_file.open("rb") as handle:
        data = tomllib.load(handle)

    base_dir = config_file.parent.resolve()

    wolvenkit_data = _require_section(data, "wolvenkit")
    paths_data = _require_section(data, "paths")
    mod_data = _require_section(data, "mod")
    textures_data = _require_section(data, "textures")
    discovery_data = _require_section(data, "discovery")
    performance_data = _require_section(data, "performance")

    wolvenkit = WolvenKitConfig(
        cli_path=_resolve_path(base_dir, _require_value(wolvenkit_data, "cli_path"))
    )

    paths = PathsConfig(
        game_dir=_resolve_path(base_dir, _require_value(paths_data, "game_dir")),
        work_dir=_resolve_path(base_dir, _require_value(paths_data, "work_dir")),
        output_dir=_resolve_path(base_dir, _require_value(paths_data, "output_dir")),
    )

    mod = ModConfig(
        name=_require_non_empty_string(mod_data, "name"),
        version=_require_non_empty_string(mod_data, "version"),
        description=_require_non_empty_string(mod_data, "description"),
    )

    editable_format = str(textures_data.get("editable_format", "tga")).strip().lower()
    if not editable_format:
        raise ConfigError("[textures].editable_format cannot be empty")

    textures = TexturesConfig(
        editable_format=editable_format,
        preserve_dimensions=bool(textures_data.get("preserve_dimensions", True)),
        preserve_alpha=bool(textures_data.get("preserve_alpha", True)),
    )

    # Only "hybrid" mode is implemented in v1.  The config key exists so
    # future versions can add alternative discovery strategies (e.g. pure
    # regex, ML-based) without a breaking config change.
    mode = str(discovery_data.get("mode", "hybrid")).strip().lower()
    if mode != "hybrid":
        raise ConfigError(
            f"Unsupported discovery mode {mode!r}; this v1 requires 'hybrid'."
        )

    discovery = DiscoveryConfig(
        mode=mode,
        approved_manifest=_resolve_path(
            base_dir, str(discovery_data.get("approved_manifest", "./assets_manifest.csv"))
        ),
        candidate_report=_resolve_path(
            base_dir,
            str(discovery_data.get("candidate_report", "./candidate_assets.csv")),
        ),
    )

    workers_raw = int(performance_data.get("workers", 1))
    if workers_raw < 1:
        raise ConfigError("[performance].workers must be >= 1")

    performance = PerformanceConfig(workers=workers_raw)

    return PipelineConfig(
        config_path=config_file,
        base_dir=base_dir,
        wolvenkit=wolvenkit,
        paths=paths,
        mod=mod,
        textures=textures,
        discovery=discovery,
        performance=performance,
    )


def _resolve_path(base_dir: Path, raw: str) -> Path:
    """Resolve a config path value to an absolute Path.

    Relative paths (e.g. "./work") are resolved against base_dir (the
    directory containing config.toml).  Absolute paths pass through unchanged.
    """
    value = str(raw).strip()
    if not value:
        raise ConfigError("Path values cannot be empty")
    path = Path(value)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _require_section(root: dict, name: str) -> dict:
    section = root.get(name)
    if not isinstance(section, dict):
        raise ConfigError(f"Missing required section [{name}]")
    return section


def _require_value(section: dict, key: str) -> str:
    if key not in section:
        raise ConfigError(f"Missing required key: {key}")
    return str(section[key])


def _require_non_empty_string(section: dict, key: str) -> str:
    value = str(section.get(key, "")).strip()
    if not value:
        raise ConfigError(f"[{key}] must be a non-empty string")
    return value
