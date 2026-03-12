from pathlib import Path

from PIL import Image

from cp2077_adtex.models import AssetRecord
from cp2077_adtex.validation import validate_edited_asset


def _make_image(path: Path, size: tuple[int, int], mode: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new(mode, size, color=(255, 0, 0, 255) if "A" in mode else (255, 0, 0))
    image.save(path)


def test_validate_edited_asset_dimension_mismatch(tmp_path: Path) -> None:
    edited = tmp_path / "edited.tga"
    _make_image(edited, (256, 256), "RGBA")

    record = AssetRecord(
        asset_id="x",
        archive_path="a.archive",
        relative_texture_path="ad.xbm",
        editable_source_path="",
        edited_path=str(edited),
        width=128,
        height=128,
        has_alpha=True,
        status="ready",
        notes="",
    )

    result = validate_edited_asset(
        record,
        edited,
        preserve_dimensions=True,
        preserve_alpha=True,
    )

    assert result.ok is False
    assert any("Dimension mismatch" in message for message in result.messages)


def test_validate_edited_asset_alpha_mismatch(tmp_path: Path) -> None:
    edited = tmp_path / "edited_rgb.tga"
    _make_image(edited, (128, 128), "RGB")

    record = AssetRecord(
        asset_id="x",
        archive_path="a.archive",
        relative_texture_path="ad.xbm",
        editable_source_path="",
        edited_path=str(edited),
        width=128,
        height=128,
        has_alpha=True,
        status="ready",
        notes="",
    )

    result = validate_edited_asset(
        record,
        edited,
        preserve_dimensions=True,
        preserve_alpha=True,
    )

    assert result.ok is False
    assert any("Alpha channel mismatch" in message for message in result.messages)
