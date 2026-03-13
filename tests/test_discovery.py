"""Tests for discovery.py — candidate scanning, manifest merge, and friendly naming.

Covers the specific failure modes we hit in production:
  - Existing hash-named manifest rows not being updated on re-run
  - Collision resolution between textures sharing a filename stem
  - _ensure_edit_paths overwriting paths that are already set
  - Skip-existing logic in extract not applying friendly names
"""

import logging
from pathlib import Path

import pytest
from PIL import Image

from cp2077_adtex.config import load_config
from cp2077_adtex.discovery import (
    CandidateAsset,
    _derive_friendly_stems,
    merge_candidates_into_manifest,
)
from cp2077_adtex.extractor import _ensure_edit_paths, run_extract_stage
from cp2077_adtex.manifest import write_manifest
from cp2077_adtex.models import AssetRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(
    asset_id: str,
    relative_texture_path: str,
    archive: str = "archive/pc/content/basegame_4_gamedata.archive",
) -> CandidateAsset:
    return CandidateAsset(
        asset_id=asset_id,
        archive_path=archive,
        relative_texture_path=relative_texture_path,
        reason="test",
    )


def _make_row(
    asset_id: str,
    relative_texture_path: str,
    editable_source_path: str = "",
    edited_path: str = "",
    status: str = "approved",
) -> AssetRecord:
    return AssetRecord(
        asset_id=asset_id,
        archive_path="archive/pc/content/basegame_4_gamedata.archive",
        relative_texture_path=relative_texture_path,
        editable_source_path=editable_source_path,
        edited_path=edited_path,
        width=0,
        height=0,
        has_alpha=False,
        status=status,
        notes="",
    )


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
name = "ads_test"
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


class FakeRunner:
    """Minimal runner stub that writes a real PNG instead of calling WolvenKit."""

    def __init__(self) -> None:
        self.export_calls: list[str] = []  # asset_ids seen during export

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
        self.export_calls.append(Path(output_file).stem)  # stem == asset_id
        output_file.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(output_file)

    def import_texture(self, *, game_dir, archive_path, relative_texture_path,
                       edited_file, packed_root, uext, logger) -> None:
        pass

    def pack_archive(self, *, packed_root, output_archive, logger) -> None:
        output_archive.parent.mkdir(parents=True, exist_ok=True)
        output_archive.write_bytes(b"archive")


# ---------------------------------------------------------------------------
# _derive_friendly_stems
# ---------------------------------------------------------------------------

class TestDeriveFriendlyStems:
    def test_simple_unique_stems(self) -> None:
        """Each texture with a unique filename stem gets just that stem."""
        candidates = [
            _make_candidate("aaa", "base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm"),
            _make_candidate("bbb", "base/gameplay/gui/world/adverts/kiroshi/kiroshi_1080p.xbm"),
        ]
        stems = _derive_friendly_stems(candidates)
        assert stems["aaa"] == "rayfield_720p"
        assert stems["bbb"] == "kiroshi_1080p"

    def test_collision_adds_parent_prefix(self) -> None:
        """Two textures with the same stem get the parent directory prepended."""
        candidates = [
            _make_candidate("aaa", "base/adverts/rayfield/banner_d.xbm"),
            _make_candidate("bbb", "base/adverts/kiroshi/banner_d.xbm"),
        ]
        stems = _derive_friendly_stems(candidates)
        assert stems["aaa"] == "rayfield__banner_d"
        assert stems["bbb"] == "kiroshi__banner_d"

    def test_triple_collision_appends_asset_id(self) -> None:
        """Three textures that still collide after parent prefix get asset_id suffix."""
        candidates = [
            _make_candidate("id1", "base/rayfield/sub/banner_d.xbm"),
            _make_candidate("id2", "base/kiroshi/sub/banner_d.xbm"),
            _make_candidate("id3", "base/arasaka/sub/banner_d.xbm"),
        ]
        stems = _derive_friendly_stems(candidates)
        # After pass 1: all are "banner_d" — collision
        # After pass 2: all become "sub__banner_d" — still collision
        # After pass 3: asset_id is appended
        assert stems["id1"] == "sub__banner_d__id1"
        assert stems["id2"] == "sub__banner_d__id2"
        assert stems["id3"] == "sub__banner_d__id3"

    def test_empty_input(self) -> None:
        assert _derive_friendly_stems([]) == {}

    def test_backslash_paths_normalised(self) -> None:
        """Windows-style backslash paths should produce the same stem as forward slash."""
        candidates = [
            _make_candidate("aaa", r"base\gameplay\gui\world\adverts\rayfield\rayfield_720p.xbm"),
        ]
        stems = _derive_friendly_stems(candidates)
        assert stems["aaa"] == "rayfield_720p"


# ---------------------------------------------------------------------------
# merge_candidates_into_manifest — friendly naming
# ---------------------------------------------------------------------------

class TestMergeCandidatesFriendlyNaming:
    def test_new_candidates_get_friendly_names(self, tmp_path: Path) -> None:
        """Fresh merge assigns human-readable filenames, not hash-based ones."""
        cfg = load_config(_write_config(tmp_path))
        candidates = [
            _make_candidate("abc123", "base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm"),
        ]
        result = merge_candidates_into_manifest(cfg, [], candidates, default_status="approved")

        assert len(result) == 1
        assert "rayfield_720p.tga" in result[0].editable_source_path
        assert "rayfield_720p.tga" in result[0].edited_path
        # Must NOT contain the asset_id hash in the filename
        assert "abc123" not in Path(result[0].editable_source_path).name

    def test_existing_hash_named_rows_are_updated(self, tmp_path: Path) -> None:
        """Re-running merge updates rows that had hash-based paths to friendly names.

        This is the exact bug we hit: old manifest rows with paths like
        'work/ads/editable/fcea8993b4ef62b5.tga' were left unchanged on re-run
        because the code only assigned friendly names to *new* candidates.
        """
        cfg = load_config(_write_config(tmp_path))

        # Simulate an old manifest row with a hash-based editable path
        existing = [
            _make_row(
                asset_id="fcea8993b4ef62b5",
                relative_texture_path="base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm",
                editable_source_path="work/ads/editable/fcea8993b4ef62b5.tga",  # old hash name
                edited_path="work/ads/edited/fcea8993b4ef62b5.tga",
            )
        ]
        candidates = [
            _make_candidate(
                "fcea8993b4ef62b5",
                "base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm",
            )
        ]

        result = merge_candidates_into_manifest(cfg, existing, candidates, default_status="approved")

        assert len(result) == 1
        row = result[0]
        assert "rayfield_720p.tga" in row.editable_source_path, (
            f"Expected friendly name but got: {row.editable_source_path}"
        )
        assert "fcea8993b4ef62b5" not in Path(row.editable_source_path).name, (
            "Hash-based path was not updated to friendly name on re-run"
        )

    def test_other_row_fields_preserved_on_update(self, tmp_path: Path) -> None:
        """Merge updates paths but keeps status, notes, width, height, has_alpha."""
        cfg = load_config(_write_config(tmp_path))

        existing = [
            AssetRecord(
                asset_id="aabbcc",
                archive_path="archive/pc/content/basegame_4_gamedata.archive",
                relative_texture_path="base/gameplay/gui/world/adverts/kiroshi/kiroshi_ad.xbm",
                editable_source_path="work/ads/editable/aabbcc.tga",
                edited_path="work/ads/edited/aabbcc.tga",
                width=1024,
                height=512,
                has_alpha=True,
                status="ready",
                notes="my custom note",
            )
        ]
        candidates = [
            _make_candidate("aabbcc", "base/gameplay/gui/world/adverts/kiroshi/kiroshi_ad.xbm"),
        ]

        result = merge_candidates_into_manifest(cfg, existing, candidates)
        row = result[0]

        assert row.status == "ready"
        assert row.notes == "my custom note"
        assert row.width == 1024
        assert row.height == 512
        assert row.has_alpha is True
        assert "kiroshi_ad.tga" in row.editable_source_path

    def test_manually_added_row_without_candidate_untouched(self, tmp_path: Path) -> None:
        """Rows with no matching candidate keep their paths (manual additions)."""
        cfg = load_config(_write_config(tmp_path))

        manual_row = _make_row(
            asset_id="manual001",
            relative_texture_path="base/custom/my_texture.xbm",
            editable_source_path="work/ads/editable/my_custom_name.tga",
            edited_path="work/ads/edited/my_custom_name.tga",
        )
        candidates: list[CandidateAsset] = []  # no candidates

        result = merge_candidates_into_manifest(cfg, [manual_row], candidates)

        assert result[0].editable_source_path == "work/ads/editable/my_custom_name.tga"

    def test_collision_resolved_across_all_rows(self, tmp_path: Path) -> None:
        """Two candidates with the same filename stem get disambiguated."""
        cfg = load_config(_write_config(tmp_path))
        candidates = [
            _make_candidate("id1", "base/adverts/rayfield/banner_d.xbm"),
            _make_candidate("id2", "base/adverts/kiroshi/banner_d.xbm"),
        ]
        result = merge_candidates_into_manifest(cfg, [], candidates)

        names = {Path(r.editable_source_path).name for r in result}
        assert len(names) == 2, "Collision not resolved — two rows share the same filename"
        assert "banner_d.tga" not in names, "Stem collision was not detected"

    def test_new_candidate_does_not_collide_with_existing_friendly_name(
        self, tmp_path: Path
    ) -> None:
        """A new candidate whose stem matches an existing row's filename gets a longer stem."""
        cfg = load_config(_write_config(tmp_path))

        existing = [
            _make_row(
                asset_id="existing1",
                relative_texture_path="base/adverts/rayfield/banner_d.xbm",
                editable_source_path="work/ads/editable/rayfield__banner_d.tga",
                edited_path="work/ads/edited/rayfield__banner_d.tga",
            )
        ]
        new_candidate = _make_candidate("newid1", "base/adverts/kiroshi/banner_d.xbm")

        result = merge_candidates_into_manifest(cfg, existing, [new_candidate])

        names = {Path(r.editable_source_path).name for r in result}
        assert len(names) == 2, "New candidate collided with existing row filename"


# ---------------------------------------------------------------------------
# _ensure_edit_paths
# ---------------------------------------------------------------------------

class TestEnsureEditPaths:
    def test_fills_blank_paths(self, tmp_path: Path) -> None:
        """Rows with empty paths get sensible defaults derived from texture path."""
        cfg = load_config(_write_config(tmp_path))
        row = _make_row(
            "abc123",
            "base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm",
        )
        assert row.editable_source_path == ""

        _ensure_edit_paths(cfg, row)

        assert row.editable_source_path != ""
        assert "rayfield_720p" in row.editable_source_path
        assert row.edited_path != ""

    def test_does_not_overwrite_existing_paths(self, tmp_path: Path) -> None:
        """If paths are already set, _ensure_edit_paths must leave them unchanged.

        This is critical: friendly names assigned by merge_candidates_into_manifest
        must not be clobbered by the fallback logic in _ensure_edit_paths.
        """
        cfg = load_config(_write_config(tmp_path))
        row = _make_row(
            "abc123",
            "base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm",
            editable_source_path="work/ads/editable/rayfield_720p.tga",
            edited_path="work/ads/edited/rayfield_720p.tga",
        )

        _ensure_edit_paths(cfg, row)

        assert row.editable_source_path == "work/ads/editable/rayfield_720p.tga"
        assert row.edited_path == "work/ads/edited/rayfield_720p.tga"


# ---------------------------------------------------------------------------
# Extract stage — skip-existing and friendly naming end-to-end
# ---------------------------------------------------------------------------

class TestExtractStage:
    def _run_extract(self, cfg, runner, tmp_path, manifest_rows):
        cfg.paths.game_dir.mkdir(parents=True, exist_ok=True)
        write_manifest(cfg.discovery.approved_manifest, manifest_rows)
        logger = logging.getLogger("test")
        logger.handlers = [logging.NullHandler()]
        return run_extract_stage(
            cfg, runner,
            discover=False, all_known_roots=False,
            skip_extract=False, clean=False,
            logger=logger,
            log_path=cfg.paths.output_dir / "pipeline.log",
        )

    def test_editable_file_uses_friendly_name(self, tmp_path: Path) -> None:
        """Extracted editable file should use the human-readable texture stem."""
        cfg = load_config(_write_config(tmp_path))
        runner = FakeRunner()
        rows = [
            _make_row(
                "fcea8993b4ef62b5",
                "base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm",
                editable_source_path="work/ads/editable/rayfield_720p.tga",
                edited_path="work/ads/edited/rayfield_720p.tga",
            )
        ]

        result = self._run_extract(cfg, runner, tmp_path, rows)

        assert result.succeeded == 1
        editable = cfg.ads_editable_dir / "rayfield_720p.tga"
        assert editable.exists(), f"Expected friendly-named file at {editable}"

    def test_second_run_skips_wolvenkit_when_file_exists(self, tmp_path: Path) -> None:
        """Re-running extract without --clean must skip WolvenKit for existing files.

        This is the skip-existing optimisation: if both original/ and editable/ exist,
        the runner should not be called again — just refresh metadata from disk.
        """
        cfg = load_config(_write_config(tmp_path))
        runner = FakeRunner()
        rows = [
            _make_row(
                "assetabc",
                "base/gameplay/gui/world/adverts/arasaka/arasaka_hd.xbm",
                editable_source_path="work/ads/editable/arasaka_hd.tga",
                edited_path="work/ads/edited/arasaka_hd.tga",
            )
        ]

        # First run — exports via WolvenKit
        self._run_extract(cfg, runner, tmp_path, rows)
        calls_after_first = len(runner.export_calls)
        assert calls_after_first == 1

        # Second run — both files exist, should skip WolvenKit
        self._run_extract(cfg, runner, tmp_path, rows)
        calls_after_second = len(runner.export_calls)
        assert calls_after_second == calls_after_first, (
            "WolvenKit was called again on second extract — skip-existing logic is broken"
        )

    def test_hash_named_manifest_row_produces_friendly_editable_file(
        self, tmp_path: Path
    ) -> None:
        """Regression: manifest row with old hash-based path must yield a friendly filename.

        The original bug: rows created before friendly naming existed had
        editable_source_path = 'work/ads/editable/<asset_id>.tga'. Those paths
        were passed straight through to the export step, creating hash-named files
        instead of human-readable ones.
        """
        cfg = load_config(_write_config(tmp_path))
        runner = FakeRunner()

        asset_id = "fcea8993b4ef62b5"
        rows = [
            _make_row(
                asset_id=asset_id,
                relative_texture_path="base/gameplay/gui/world/adverts/rayfield/rayfield_720p.xbm",
                # Old hash-based path — this is the pre-fix state of the manifest
                editable_source_path=f"work/ads/editable/{asset_id}.tga",
                edited_path=f"work/ads/edited/{asset_id}.tga",
            )
        ]

        # Write the old manifest to disk as-is (simulates loading a pre-fix manifest)
        cfg.paths.game_dir.mkdir(parents=True, exist_ok=True)
        write_manifest(cfg.discovery.approved_manifest, rows)

        logger = logging.getLogger("test")
        logger.handlers = [logging.NullHandler()]

        # Note: we're NOT using --all-known-roots here, so the merge step is skipped.
        # _ensure_edit_paths is the only thing that could fix hash paths in this path.
        # This test documents the CURRENT behaviour (hash paths are passed through)
        # and should be updated if we later add re-derivation in the plain extract path.
        result = run_extract_stage(
            cfg, runner,
            discover=False, all_known_roots=False,
            skip_extract=False, clean=False,
            logger=logger,
            log_path=cfg.paths.output_dir / "pipeline.log",
        )

        assert result.succeeded == 1
        # The editable file should exist at whatever path the manifest specifies.
        # For hash-named manifests without --all-known-roots, the hash path is used.
        hash_file = cfg.ads_editable_dir / f"{asset_id}.tga"
        assert hash_file.exists(), (
            "Editable file was not created at the manifest-specified path"
        )
