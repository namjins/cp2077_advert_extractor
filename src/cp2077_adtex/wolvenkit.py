from __future__ import annotations

import logging
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Callable, Sequence


class WolvenKitError(RuntimeError):
    """Raised when a WolvenKit command fails."""


Executor = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class WolvenKitRunner:
    """Thin wrapper around WolvenKit CLI commands."""

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
        cmd = [str(self.cli_path), "archiveinfo", str(archive_file), "--list"]
        if pattern:
            cmd.extend(["--pattern", pattern])
        elif regex:
            cmd.extend(["--regex", regex])

        completed = self._run_capture(cmd, logger)

        paths: list[str] = []
        for line in completed.stdout.splitlines():
            entry = line.strip()
            if not entry:
                continue
            if entry.startswith("["):
                continue
            if "\\" not in entry and "/" not in entry:
                continue
            paths.append(entry.replace("/", "\\"))

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

            self._run(
                [
                    str(self.cli_path),
                    "unbundle",
                    str(archive_file),
                    "--outpath",
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
                    "--outpath",
                    str(export_dir),
                    "--gamepath",
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
        del game_dir, archive_path

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
                    "--outpath",
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
        if not packed_root.exists():
            raise WolvenKitError(f"Packed root does not exist: {packed_root}")

        output_archive.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                str(self.cli_path),
                "pack",
                str(packed_root),
                "--outpath",
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
