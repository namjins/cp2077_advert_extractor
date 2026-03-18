"""Image validation for edited textures before finalization.

Checks that user-supplied edits are readable images with dimensions and alpha
channels matching the original.  This prevents WolvenKit import failures and
in-game rendering glitches from mismatched texture properties.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .models import AssetRecord


@dataclass(slots=True)
class ImageMeta:
    width: int
    height: int
    has_alpha: bool


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    messages: list[str]
    image: ImageMeta | None


def inspect_image(path: Path) -> ImageMeta:
    try:
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            has_alpha = _image_has_alpha(image)
            return ImageMeta(width=width, height=height, has_alpha=has_alpha)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"Unreadable image: {path} ({exc})") from exc


def validate_edited_asset(
    asset: AssetRecord,
    edited_path: Path,
    *,
    preserve_dimensions: bool,
    preserve_alpha: bool,
) -> ValidationResult:
    """Check that a user-edited image is compatible with the original texture.

    Validation checks (each controlled by a config flag):
      - preserve_dimensions: edited image must match original width x height.
        Skipped if the original dimensions are unknown (width/height = 0,
        meaning the texture hasn't been exported yet).
      - preserve_alpha: edited image must have the same alpha channel
        presence as the original.

    Returns a ValidationResult with ok=True if all checks pass, or a list
    of human-readable failure messages otherwise.
    """
    messages: list[str] = []

    if not edited_path.exists():
        messages.append(f"Edited file missing: {edited_path}")
        return ValidationResult(ok=False, messages=messages, image=None)

    try:
        edited_meta = inspect_image(edited_path)
    except ValueError as exc:
        messages.append(str(exc))
        return ValidationResult(ok=False, messages=messages, image=None)

    # Dimension check: only compare when we have known original dimensions.
    if preserve_dimensions and asset.width > 0 and asset.height > 0:
        if (edited_meta.width, edited_meta.height) != (asset.width, asset.height):
            messages.append(
                "Dimension mismatch: "
                f"expected {asset.width}x{asset.height}, "
                f"got {edited_meta.width}x{edited_meta.height}"
            )

    # Alpha check: mismatched alpha can cause transparency artifacts in-game.
    if preserve_alpha and edited_meta.has_alpha != asset.has_alpha:
        messages.append(
            "Alpha channel mismatch: "
            f"expected has_alpha={asset.has_alpha}, "
            f"got has_alpha={edited_meta.has_alpha}"
        )

    return ValidationResult(ok=not messages, messages=messages, image=edited_meta)


def _image_has_alpha(image: Image.Image) -> bool:
    """Detect whether an image has an alpha channel.

    Two cases:
      1. Explicit alpha band — modes like RGBA, LA have "A" in getbands().
      2. Palette transparency — mode "P" (palettized) images can carry
         transparency via the "transparency" info key rather than a
         separate alpha band.  This is common in indexed-color PNGs.
    """
    if "A" in image.getbands():
        return True
    if image.mode == "P" and "transparency" in image.info:
        return True
    return False
