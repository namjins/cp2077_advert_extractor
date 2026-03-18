"""WolvenKit CLI wrapper — all interaction with the WolvenKit binary goes through here.

WolvenKit (https://wiki.redmodding.org/wolvenkit) is the de-facto modding tool
for Cyberpunk 2077.  This module wraps five CLI operations:

  archiveinfo --diff  — list every file inside a .archive (JSON output)
  unbundle            — extract raw .xbm files from a .archive
  export              — convert .xbm to an editable format (.tga, .png, etc.)
  import              — convert an edited image back into an .xbm
  pack                — assemble a directory tree into a new .archive

Design decisions:
  - Every operation runs in a temporary directory (tempfile.TemporaryDirectory)
    so half-finished output never pollutes the real workspace.  Only the final
    result is copied to the caller's destination path.
  - The Executor callable (defaults to subprocess.run) lets tests inject a
    FakeRunner that records commands without touching the filesystem or the
    real WolvenKit binary.  See tests/test_wolvenkit_commands.py.
  - Exit code 3 is treated as a non-fatal warning (Oodle DLL missing) because
    WolvenKit still produces valid output; the Oodle codec only affects
    certain compression paths.
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
# Default is subprocess.run (see _default_executor); tests supply a spy/fake
# that records calls and returns canned results.  This keeps WolvenKit CLI
# integration tests fast and deterministic.
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

        # archiveinfo --diff emits a JSON object with a "Files" dict keyed by
        # numeric index.  Each value has a "Name" (or "FileName") string
        # containing the internal archive path (e.g. "base\\textures\\foo.xbm").
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
        # WolvenKit returns backslash paths; normalize pattern to match.
        pattern_text = pattern.replace("/", "\\") if pattern else None

        paths: list[str] = []
        for item in files_obj.values():
            if not isinstance(item, dict):
                continue
            # WolvenKit JSON may use "Name" or "FileName" depending on version.
            name = item.get("Name") or item.get("FileName")
            if not isinstance(name, str):
                continue

            # Normalize to backslash — WolvenKit's native separator.
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

        Two-step process inside a temp directory:
          1. ``unbundle`` — pull the raw .xbm out of the .archive
          2. ``export``   — convert the .xbm to an editable format (.tga/.png)

        Only the final converted image is copied to *output_file*; the temp
        directory (containing intermediate .xbm data) is cleaned up automatically.
        """
        output_file.parent.mkdir(parents=True, exist_ok=True)
        archive_file = self._resolve_archive_path(game_dir, archive_path)
        if not archive_file.exists():
            raise WolvenKitError(f"Archive not found: {archive_file}")

        rel_path = self._relative_path(relative_texture_path)
        # Escape the path for use as a --regex filter so unbundle only
        # extracts the single texture we need (faster than extracting all).
        regex = re.escape(str(rel_path).replace("/", "\\"))

        with tempfile.TemporaryDirectory(prefix="adtex_export_") as tmp:
            tmp_root = Path(tmp)
            unbundle_dir = tmp_root / "unbundle"
            export_dir = tmp_root / "export"
            export_dir.mkdir(parents=True, exist_ok=True)

            # Step 1: unbundle — extract the raw .xbm from the game archive.
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

            # WolvenKit may nest the .xbm under a different subdirectory
            # structure than expected.  Try the exact relative path first,
            # then fall back to a recursive glob by filename.
            extracted = unbundle_dir / rel_path
            if not extracted.exists():
                candidates = list(unbundle_dir.rglob(rel_path.name))
                if not candidates:
                    raise WolvenKitError(
                        "Unbundle did not produce expected file for "
                        f"{relative_texture_path} from {archive_file}"
                    )
                extracted = candidates[0]

            # Step 2: export — convert .xbm to editable format.
            # -gp (game path) is required so WolvenKit can resolve internal
            # texture references.  --uext sets the output extension.
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

            # WolvenKit usually names the export <stem>.<uext>, but in some
            # edge cases the output path differs.  Fall back to a glob search
            # if the expected filename isn't present.
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

            # Copy the final converted image out of the temp dir to the
            # caller's destination.  shutil.copy2 preserves metadata.
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

        Uses the documented WolvenKit CLI workflow::

            WolvenKit.CLI import -p <source_dir> -k

        Steps:
          1. Unbundle the original ``.xbm`` from the game archive into a temp
             source tree at the correct relative path.
          2. Place the edited image alongside it (same dir, same stem,
             different extension).
          3. Run ``import -p <source_dir> -k`` — the ``-k`` flag tells
             WolvenKit to apply the new pixels back to the **existing**
             ``.xbm``, preserving its metadata (IsGamma, TextureGroup,
             compression, etc.).
          4. Copy the reimported ``.xbm`` to the packed staging tree.

        This avoids the washed-out / white texture problem caused by WolvenKit
        guessing the wrong color-space settings when creating a fresh .xbm.
        """
        if not edited_file.exists():
            raise WolvenKitError(f"Edited file not found: {edited_file}")

        packed_root.mkdir(parents=True, exist_ok=True)
        rel_path = self._relative_path(relative_texture_path)

        archive_file = self._resolve_archive_path(game_dir, archive_path)
        if not archive_file.exists():
            raise WolvenKitError(f"Archive not found: {archive_file}")

        with tempfile.TemporaryDirectory(prefix="adtex_import_") as tmp:
            tmp_root = Path(tmp)
            source_dir = tmp_root / "source"

            # Step 1: unbundle the original .xbm into the source tree.
            regex = re.escape(str(rel_path).replace("/", "\\"))
            self._run(
                [
                    str(self.cli_path),
                    "unbundle",
                    str(archive_file),
                    "-o",
                    str(source_dir),
                    "--regex",
                    regex,
                ],
                logger,
            )

            original_xbm = source_dir / rel_path
            if not original_xbm.exists():
                candidates = list(source_dir.rglob(rel_path.name))
                if not candidates:
                    raise WolvenKitError(
                        "Unbundle did not produce expected .xbm for "
                        f"{relative_texture_path} from {archive_file}"
                    )
                original_xbm = candidates[0]

            # Step 2: place edited image alongside the .xbm (same dir,
            # same stem, different extension) so WolvenKit can match them.
            raw_dest = original_xbm.with_suffix(f".{uext}")
            shutil.copy2(edited_file, raw_dest)
            logger.info(
                "Staged .xbm + edited .%s in %s for import -k",
                uext,
                original_xbm.parent,
            )

            # Step 3: import -p <source_dir> -k
            # -p  = process the folder (WolvenKit matches raw files to .xbm)
            # -k  = keep/apply to existing .xbm, preserving its metadata
            self._run(
                [
                    str(self.cli_path),
                    "import",
                    "-p",
                    str(source_dir),
                    "-k",
                ],
                logger,
            )

            # Step 4: copy the reimported .xbm to the packed staging tree.
            out_dir = packed_root / rel_path.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            reimported = source_dir / rel_path
            if not reimported.exists():
                reimported = original_xbm
            shutil.copy2(reimported, out_dir / rel_path.name)

    def pack_archive(
        self,
        *,
        packed_root: Path,
        output_archive: Path,
        logger: logging.Logger,
    ) -> None:
        """Pack a directory of imported .xbm files into a single .archive.

        WolvenKit ``pack`` names the output ``<source_dir_name>.archive``
        (e.g. ``packed.archive``).  After packing, we rename the generated
        file to the caller's desired *output_archive* path.

        Fallback: if the expected name doesn't appear (WolvenKit version
        difference), we grab the most-recently-modified .archive in the
        output directory.
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

        # WolvenKit names the output after the source directory.
        generated = output_archive.parent / f"{packed_root.name}.archive"
        if generated.exists():
            generated.replace(output_archive)
            return

        # Fallback: pick the newest .archive if the expected name wasn't produced.
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

    # WolvenKit CLI sometimes returns non-zero exit codes for warnings
    # (e.g., exit 3 when Oodle DLL is missing) even though the operation
    # succeeded.  These are treated as warnings, not failures.
    _WARNING_EXIT_CODES: frozenset[int] = frozenset({3})

    _OODLE_WARNING = "Oodle couldn't be loaded"

    def _run(self, cmd: Sequence[str], logger: logging.Logger) -> None:
        """Execute a WolvenKit command and log its output.

        Delegates to _run_capture for actual execution and exit-code handling;
        this wrapper just streams stdout/stderr to the logger.
        """
        completed = self._run_capture(cmd, logger)
        if completed.stdout.strip():
            logger.info("WolvenKit stdout: %s", completed.stdout.strip())
        if completed.stderr.strip():
            logger.warning("WolvenKit stderr: %s", completed.stderr.strip())

    def _run_capture(
        self, cmd: Sequence[str], logger: logging.Logger
    ) -> subprocess.CompletedProcess[str]:
        """Execute a WolvenKit command and return the CompletedProcess.

        Handles three cases:
          - Exit code 0: success.
          - Exit code in _WARNING_EXIT_CODES (e.g. 3): log warning but treat
            as success, because WolvenKit still produces valid output (the
            Oodle DLL warning is the most common trigger).
          - Any other non-zero code: raise WolvenKitError with full output
            for diagnosis.
        """
        logger.info("WolvenKit command: %s", " ".join(cmd))
        try:
            completed = self.executor(cmd)
        except FileNotFoundError as exc:
            raise WolvenKitError(
                "WolvenKit CLI not found. Check [wolvenkit].cli_path in config.toml."
            ) from exc

        # Detect the Oodle warning early so we can give a specific, actionable
        # message regardless of the exit code.
        if self._OODLE_WARNING in completed.stdout:
            logger.warning(
                "WolvenKit: Oodle DLL not found — colors may be corrupted. "
                "Copy oo2ext_7_win64.dll from <game>/bin/x64/ to your "
                "WolvenKit CLI directory to fix this."
            )

        if completed.returncode != 0:
            if completed.returncode in self._WARNING_EXIT_CODES:
                logger.warning(
                    "WolvenKit exited with code %d (treated as warning): %s",
                    completed.returncode,
                    " ".join(cmd),
                )
            else:
                raise WolvenKitError(
                    "WolvenKit command failed "
                    f"(exit {completed.returncode}): {' '.join(cmd)}\n"
                    f"stdout:\n{completed.stdout}\n"
                    f"stderr:\n{completed.stderr}"
                )

        return completed

    @staticmethod
    def _resolve_archive_path(game_dir: Path, archive_path: str) -> Path:
        """Turn an archive_path into an absolute Path.

        If archive_path is already absolute (rare — e.g. user-specified full
        path), return it directly.  Otherwise treat it as relative to game_dir
        (the normal case: "archive/pc/content/basegame_4_gamedata.archive").
        """
        candidate = Path(archive_path)
        if candidate.is_absolute():
            return candidate
        # Normalize backslashes to forward slashes for cross-platform Path joining.
        normalized = archive_path.replace("\\", "/")
        return game_dir / Path(normalized)

    @staticmethod
    def _relative_path(value: str) -> Path:
        """Normalize a relative texture path (backslash -> forward slash) for Path use."""
        return Path(value.replace("\\", "/"))

    @staticmethod
    def _default_executor(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Default executor — runs the command as a subprocess.

        capture_output=True captures stdout/stderr for logging and error
        reporting.  check=False lets us handle exit codes ourselves (see
        _run_capture) rather than raising CalledProcessError.
        """
        return subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            check=False,
        )
