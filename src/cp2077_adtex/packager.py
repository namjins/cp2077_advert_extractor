from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

from .io_utils import ensure_dir

FIXED_ZIP_DT = (2020, 1, 1, 0, 0, 0)


def package_mod_archive(archive_path: Path, output_zip: Path, mod_name: str) -> Path:
    if not archive_path.exists():
        raise FileNotFoundError(f"Packed archive not found: {archive_path}")

    ensure_dir(output_zip.parent)
    archive_bytes = archive_path.read_bytes()

    arcname = f"archive/pc/mod/{mod_name}.archive"
    info = zipfile.ZipInfo(arcname)
    info.date_time = FIXED_ZIP_DT
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16

    with zipfile.ZipFile(output_zip, mode="w") as handle:
        handle.writestr(info, archive_bytes)

    return output_zip


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()
