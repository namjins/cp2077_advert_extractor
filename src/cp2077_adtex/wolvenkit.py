"""WolvenKit CLI wrapper — all interaction with the WolvenKit binary goes through here.

WolvenKit (https://wiki.redmodding.org/wolvenkit) is the de-facto modding tool
for Cyberpunk 2077.  This module wraps four CLI commands:

  archiveinfo --diff  — list every file inside a .archive (JSON output)
  unbundle            — extract raw .xbm files from a .archive
  export              — convert .xbm to an editable format (.tga, .png, etc.)
  import              — convert an edited image back into an .xbm
  pack                — assemble a directory tree into a new .archive

Each method works in a temp directory to avoid polluting the workspace, then
copies only the final output file to the intended destination.

The Executor callable allows tests to inject a FakeRunner without touching the
filesystem or the real WolvenKit binary.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Callable, Sequence


class WolvenKitError(RuntimeError):
    """Raised when a WolvenKit command fails or produces unexpected output."""


# Pluggable executor type — accepts a command list, returns CompletedProcess.
# Default is subprocess.run; tests supply a fake that records calls.
Executor = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class WolvenKitRunner:
    """Thin wrapper around WolvenKit CLI commands.

    All methods accept a logger for tracing, and raise WolvenKitError on failure.
    """

    def __init__(self, cli_path: Path, executor: Executor | None = None) -> None:
        self.cli_path = cli_path
        self.executor = executor or self._default_executor

    def list_archive_files(
        self,
        *,
        archive_file: Path,
        logger: logging.Logger,
        regex: str | None = None,
        pattern: str | None = None,
    ) -> list[str]:
        """Run `archiveinfo --diff` and return every file path inside the archive.

        Optional regex/pattern filters are applied after parsing.  Paths are
        returned with backslash separators (matching WolvenKit's convention).
        """
        cmd = [str(self.cli_path), "archiveinfo", str(archive_file), "--diff"]
        completed = self._run_capture(cmd, logger)

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            head = completed.stdout[:400]
            raise WolvenKitError(
                f"archiveinfo --diff returned non-JSON output for {archive_file}: {head!r}"
            ) from exc

        files_obj = payload.get("Files", {})
        if not isinstance(files_obj, dict):
            return []

        regex_obj = re.compile(regex) if regex else None
        pattern_text = pattern.replace("/", "\\") if pattern else None

        paths: list[str] = []
        for item in files_obj.values():
            if not isinstance(item, dict):
                continue
            name = item.get("Name") or item.get("FileName")
            if not isinstance(name, str):
                continue

            normalized = name.replace("/", "\\")

            if pattern_text and not fnmatch.fnmatch(normalized, pattern_text):
                continue
            if regex_obj and not regex_obj.search(normalized):
                continue

            paths.append(normalized)

        return paths

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
        """Unbundle a .xbm from an archive and export it as an editable image.

        Steps: unbundle (archive -> raw .xbm) then export (.xbm -> .tga/.png).
        Both happen in a temp dir; only the final image is copied to output_file.
        """
        output_file.parent.mkdir(parents=True, exist_ok=True)
        archive_file = self._resolve_archive_path(game_dir, archive_path)
        if not archive_file.exists():
            raise WolvenKitError(f"Archive not found: {archive_file}")

        rel_path = self._relative_path(relative_texture_path)
        regex = re.escape(str(rel_path).replace("/", "\\"))

        with tempfile.TemporaryDirectory(prefix="adtex_export_") as tmp:
            tmp_root = Path(tmp)
            unbundle_dir = tmp_root / "unbundle"
            export_dir = tmp_root / "export"
            export_dir.mkdir(parents=True, exist_ok=True)

            self._run(
                [
                    str(self.cli_path),
                    "unbundle",
                    str(archive_file),
                    "-o",
                    str(unbundle_dir),
                    "--regex",
                    regex,
                ],
                logger,
            )

            extracted = unbundle_dir / rel_path
            if not extracted.exists():
                candidates = list(unbundle_dir.rglob(rel_path.name))
                if not candidates:
                    raise WolvenKitError(
                        "Unbundle did not produce expected file for "
                        f"{relative_texture_path} from {archive_file}"
                    )
                extracted = candidates[0]

            self._run(
                [
                    str(self.cli_path),
                    "export",
                    str(extracted),
                    "-o",
                    str(export_dir),
                    "-gp",
                    str(game_dir),
                    "--uext",
                    uext,
                ],
                logger,
            )

            expected = export_dir / f"{extracted.stem}.{uext}"
            if expected.exists():
                source_file = expected
            else:
                exported = sorted(export_dir.rglob(f"*.{uext}"))
                if not exported:
                    raise WolvenKitError(
                        f"Export failed to produce a .{uext} file for {relative_texture_path}"
                    )
                source_file = exported[0]

            shutil.copy2(source_file, output_file)

    def import_texture(
        self,
        *,
        game_dir: Path,
        archive_path: str,
        relative_texture_path: str,
        edited_file: Path,
        packed_root: Path,
        uext: str,
        logger: logging.Logger,
    ) -> None:
        """Convert an edited image back to .xbm and stage it for packing.

        The edited file is placed in a temp dir with the expected relative path
        structure, then `wkit import` converts it back.  The result lands in
        packed_root under the correct directory tree for `wkit pack`.
        """
        # game_dir and archive_path are accepted for interface symmetry with
        # export_texture (and the FakeRunner test double), but WolvenKit's
        # "import" command only needs the raw file and an output directory.
        _ = game_dir, archive_path

        if not edited_file.exists():
            raise WolvenKitError(f"Edited file not found: {edited_file}")

        packed_root.mkdir(parents=True, exist_ok=True)
        rel_path = self._relative_path(relative_texture_path)

        with tempfile.TemporaryDirectory(prefix="adtex_import_") as tmp:
            tmp_root = Path(tmp)
            staged_raw = tmp_root / "raw" / rel_path.with_suffix(f".{uext}")
            staged_raw.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(edited_file, staged_raw)

            out_dir = packed_root / rel_path.parent
            out_dir.mkdir(parents=True, exist_ok=True)

            self._run(
                [
                    str(self.cli_path),
                    "import",
                    str(staged_raw),
                    "-o",
                    str(out_dir),
                ],
                logger,
            )

    def pack_archive(
        self,
        *,
        packed_root: Path,
        output_archive: Path,
        logger: logging.Logger,
    ) -> None:
        """Pack a directory of imported .xbm files into a single .archive.

        WolvenKit names the output after the source directory, so we rename it
        to the desired output_archive path afterward.
        """
        if not packed_root.exists():
            raise WolvenKitError(f"Packed root does not exist: {packed_root}")

        output_archive.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                str(self.cli_path),
                "pack",
                str(packed_root),
                "-o",
                str(output_archive.parent),
            ],
            logger,
        )

        generated = output_archive.parent / f"{packed_root.name}.archive"
        if generated.exists():
            generated.replace(output_archive)
            return

        archives = sorted(
            output_archive.parent.glob("*.archive"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not archives:
            raise WolvenKitError(
                f"Pack completed but no archive file was found in {output_archive.parent}"
            )
        archives[0].replace(output_archive)

    def _run(self, cmd: Sequence[str], logger: logging.Logger) -> None:
        completed = self._run_capture(cmd, logger)
        if completed.stdout.strip():
            logger.info("WolvenKit stdout: %s", completed.stdout.strip())
        if completed.stderr.strip():
            logger.warning("WolvenKit stderr: %s", completed.stderr.strip())

    def _run_capture(
        self, cmd: Sequence[str], logger: logging.Logger
    ) -> subprocess.CompletedProcess[str]:
        logger.info("WolvenKit command: %s", " ".join(cmd))
        try:
            completed = self.executor(cmd)
        except FileNotFoundError as exc:
            raise WolvenKitError(
                "WolvenKit CLI not found. Check [wolvenkit].cli_path in config.toml."
            ) from exc

        if completed.returncode != 0:
            raise WolvenKitError(
                "WolvenKit command failed "
                f"(exit {completed.returncode}): {' '.join(cmd)}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )

        return completed

    @staticmethod
    def _resolve_archive_path(game_dir: Path, archive_path: str) -> Path:
        candidate = Path(archive_path)
        if candidate.is_absolute():
            return candidate
        normalized = archive_path.replace("\\", "/")
        return game_dir / Path(normalized)

    @staticmethod
    def _relative_path(value: str) -> Path:
        return Path(value.replace("\\", "/"))

    @staticmethod
    def _default_executor(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            check=False,
        )
