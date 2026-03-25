"""Final packaging — zip the .archive into an installable mod structure.

The output zip contains archive/pc/mod/<mod_name>.archive, which is the
standard layout expected by Cyberpunk 2077 mod managers (Vortex, manual install).
A fixed date_time is used in the zip entry for reproducible builds.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

from .io_utils import ensure_dir

# Fixed timestamp used for all zip entries so that the output is
# byte-for-byte reproducible regardless of when the build runs.
# This matters for mod distribution — users can verify they have the
# same file without false diffs from timestamp changes.
FIXED_ZIP_DT = (2020, 1, 1, 0, 0, 0)


def package_mod_archive(archive_path: Path, output_zip: Path, mod_name: str) -> Path:
    """Zip the .archive into an installable mod layout.

    The zip contains ``archive/pc/mod/<mod_name>.archive`` — the standard
    directory structure expected by Cyberpunk 2077 mod managers (Vortex) and
    manual install instructions ("extract to game root").

    Streams the archive file in 1 MB chunks to avoid loading the entire
    .archive into memory (packed archives can be hundreds of MB).
    """
    if not archive_path.exists():
        raise FileNotFoundError(f"Packed archive not found: {archive_path}")

    ensure_dir(output_zip.parent)

    arcname = f"archive/pc/mod/{mod_name}.archive"

    with zipfile.ZipFile(output_zip, mode="w") as zf:
        _add_archive_to_zip(zf, archive_path, arcname)

    return output_zip


def _add_archive_to_zip(zf: zipfile.ZipFile, archive_path: Path, arcname: str) -> None:
    """Stream a single .archive file into an open ZipFile."""
    info = zipfile.ZipInfo(arcname)
    info.date_time = FIXED_ZIP_DT
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16

    with zf.open(info, "w") as dest, archive_path.open("rb") as src:
        while chunk := src.read(1 << 20):
            dest.write(chunk)


def package_mod_bundles(archive_paths: list[Path], output_zip: Path) -> Path:
    """Zip multiple .archive files into one installable mod layout.

    Each archive is placed at ``archive/pc/mod/<stem>.archive`` inside the
    zip.  Used by ``--per-bundle`` mode where each texture set has its own
    .archive file.
    """
    if not archive_paths:
        raise ValueError("No archive files to package")

    for ap in archive_paths:
        if not ap.exists():
            raise FileNotFoundError(f"Packed archive not found: {ap}")

    ensure_dir(output_zip.parent)

    with zipfile.ZipFile(output_zip, mode="w") as zf:
        for ap in sorted(archive_paths):
            arcname = f"archive/pc/mod/{ap.stem}.archive"
            _add_archive_to_zip(zf, ap, arcname)

    return output_zip


def sha256_file(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file, reading in 8 KB chunks.

    Used by the --only-changed filter to detect whether an edited file
    actually differs from the editable source.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()
