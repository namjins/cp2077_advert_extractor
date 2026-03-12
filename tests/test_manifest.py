from pathlib import Path

import pytest

from cp2077_adtex.manifest import read_manifest, write_manifest
from cp2077_adtex.models import AssetRecord, ManifestError


def test_manifest_roundtrip(tmp_path: Path) -> None:
    manifest_path = tmp_path / "assets_manifest.csv"

    rows = [
        AssetRecord(
            asset_id="asset_a",
            archive_path="archive/basegame_1.archive",
            relative_texture_path="base/textures/ad_01.xbm",
            editable_source_path="work/ads/editable/asset_a.tga",
            edited_path="work/ads/edited/asset_a.tga",
            width=1024,
            height=512,
            has_alpha=True,
            status="approved",
            notes="",
        )
    ]

    write_manifest(manifest_path, rows)
    loaded = read_manifest(manifest_path)

    assert len(loaded) == 1
    assert loaded[0].asset_id == "asset_a"
    assert loaded[0].width == 1024
    assert loaded[0].has_alpha is True


def test_manifest_rejects_missing_columns(tmp_path: Path) -> None:
    manifest_path = tmp_path / "assets_manifest.csv"
    manifest_path.write_text("asset_id,status\nabc,approved\n", encoding="utf-8")

    with pytest.raises(ManifestError):
        read_manifest(manifest_path)
