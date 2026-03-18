"""Tests that exercise the real WolvenKitRunner with a recording executor.

Unlike FakeRunner-based tests (which replace the entire runner), these tests
use WolvenKitRunner directly and inject a spy executor that records every CLI
command.  This catches regressions like passing invalid flags to WolvenKit —
something FakeRunner tests silently ignore.
"""

import logging
import subprocess
from pathlib import Path

from cp2077_adtex.wolvenkit import WolvenKitRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The only flags WolvenKit CLI accepts for each subcommand.
# If the runner builds a command with an unknown flag, the test fails.
ALLOWED_FLAGS = {
    "import": {"-p", "-k"},
    "export": {"-o", "-gp", "--uext"},
    "unbundle": {"-o", "--regex"},
    "pack": {"-o"},
    "archiveinfo": {"--diff"},
}

# Flags that MUST be present for a correct import command.
REQUIRED_IMPORT_FLAGS = {"-p", "-k"}


def _make_ok_executor(calls: list[list[str]]):
    """Return an executor that records calls and always succeeds."""

    def executor(cmd):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    return executor


def _logger() -> logging.Logger:
    log = logging.getLogger("test.wolvenkit_commands")
    log.handlers = []
    log.addHandler(logging.NullHandler())
    return log


def _extract_flags(cmd: list[str]) -> set[str]:
    """Return all flag-style arguments (starting with '-') from a command."""
    return {arg for arg in cmd if arg.startswith("-")}


def _get_subcommand(cmd: list[str]) -> str:
    """Return the WolvenKit subcommand (second element after the CLI path)."""
    return cmd[1]


def _make_import_executor(calls: list[list[str]]):
    """Executor that records calls and produces a fake .xbm for unbundle."""

    def executor(cmd):
        calls.append(list(cmd))
        subcmd = _get_subcommand(cmd)
        if subcmd == "unbundle":
            out_idx = cmd.index("-o") + 1
            xbm = Path(cmd[out_idx]) / "base/textures/ad.xbm"
            xbm.parent.mkdir(parents=True, exist_ok=True)
            xbm.write_bytes(b"xbm-data")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    return executor


def _run_import(tmp_path: Path, calls: list[list[str]]) -> None:
    """Helper: set up files and run import_texture."""
    runner = WolvenKitRunner(Path("wkit.exe"), _make_import_executor(calls))

    edited = tmp_path / "edited.tga"
    edited.write_bytes(b"pixels")
    archive = tmp_path / "game" / "archive.archive"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"archive")
    packed = tmp_path / "packed"

    runner.import_texture(
        game_dir=tmp_path / "game",
        archive_path="archive.archive",
        relative_texture_path="base/textures/ad.xbm",
        edited_file=edited,
        packed_root=packed,
        uext="tga",
        logger=_logger(),
    )


# ---------------------------------------------------------------------------
# import_texture
# ---------------------------------------------------------------------------


class TestImportTextureCommand:
    """Verify the CLI commands built by import_texture.

    import_texture runs two WolvenKit commands:
      1. unbundle — extract the original .xbm from the game archive
      2. import -p <dir> -k — reimport the edited image, preserving the
         original .xbm's metadata (IsGamma, TextureGroup, compression)
    """

    def test_import_issues_unbundle_then_import(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []
        _run_import(tmp_path, calls)

        assert len(calls) == 2
        assert _get_subcommand(calls[0]) == "unbundle"
        assert _get_subcommand(calls[1]) == "import"

    def test_import_uses_folder_mode_with_keep(self, tmp_path: Path) -> None:
        """Import must use -p (folder mode) and -k (keep existing .xbm metadata)."""
        calls: list[list[str]] = []
        _run_import(tmp_path, calls)

        import_cmd = calls[1]
        flags = _extract_flags(import_cmd)
        assert REQUIRED_IMPORT_FLAGS <= flags, (
            f"Import command missing required flags: "
            f"{REQUIRED_IMPORT_FLAGS - flags}. "
            f"The documented workflow requires 'import -p <dir> -k'."
        )

    def test_import_flags_are_allowed(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []
        _run_import(tmp_path, calls)

        for cmd in calls:
            subcmd = _get_subcommand(cmd)
            flags = _extract_flags(cmd)
            assert flags <= ALLOWED_FLAGS[subcmd], (
                f"Unexpected flags in {subcmd} command: "
                f"{flags - ALLOWED_FLAGS[subcmd]}"
            )

    def test_xbm_and_raw_colocated_in_source_tree(self, tmp_path: Path) -> None:
        """Both .xbm and edited raw must be in the same source tree for -k."""
        source_dir_at_import = []

        def executor(cmd):
            subcmd = _get_subcommand(cmd)
            if subcmd == "unbundle":
                out_idx = cmd.index("-o") + 1
                xbm = Path(cmd[out_idx]) / "base/textures/ad.xbm"
                xbm.parent.mkdir(parents=True, exist_ok=True)
                xbm.write_bytes(b"xbm-data")
            elif subcmd == "import":
                # -p value is the source directory
                p_idx = cmd.index("-p") + 1
                src = Path(cmd[p_idx])
                xbm_exists = (src / "base/textures/ad.xbm").exists()
                raw_exists = (src / "base/textures/ad.tga").exists()
                source_dir_at_import.append((xbm_exists, raw_exists))
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        runner = WolvenKitRunner(Path("wkit.exe"), executor)

        edited = tmp_path / "edited.tga"
        edited.write_bytes(b"pixels")
        archive = tmp_path / "game" / "archive.archive"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"archive")

        runner.import_texture(
            game_dir=tmp_path / "game",
            archive_path="archive.archive",
            relative_texture_path="base/textures/ad.xbm",
            edited_file=edited,
            packed_root=tmp_path / "packed",
            uext="tga",
            logger=_logger(),
        )

        assert source_dir_at_import == [(True, True)], (
            "Both .xbm and edited .tga must exist side-by-side in the "
            "source tree when 'import -p <dir> -k' runs"
        )

    def test_import_does_not_use_single_file_mode(self, tmp_path: Path) -> None:
        """Import must NOT pass a single file path — must use -p <dir> mode."""
        calls: list[list[str]] = []
        _run_import(tmp_path, calls)

        import_cmd = calls[1]
        # In single-file mode, the third element would be a file path (not a flag)
        # In folder mode, we have: wkit import -p <dir> -k
        assert "-p" in import_cmd, "Import must use -p (folder mode)"
        # The value after -p should be a directory, not a .tga file
        p_idx = import_cmd.index("-p") + 1
        p_value = import_cmd[p_idx]
        assert not p_value.endswith(".tga"), (
            "Import -p should point to a directory, not a single .tga file"
        )


# ---------------------------------------------------------------------------
# export_texture
# ---------------------------------------------------------------------------


class TestExportTextureCommand:
    """Verify the CLI commands built by export_texture."""

    def test_export_flags_are_allowed(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def executor(cmd):
            calls.append(list(cmd))
            subcmd = _get_subcommand(cmd)
            if subcmd == "unbundle":
                out_idx = cmd.index("-o") + 1
                xbm = Path(out_idx and cmd[out_idx]) / "base/textures/ad.xbm"
                xbm.parent.mkdir(parents=True, exist_ok=True)
                xbm.write_bytes(b"xbm-data")
            elif subcmd == "export":
                out_idx = cmd.index("-o") + 1
                tga = Path(cmd[out_idx]) / "ad.tga"
                tga.parent.mkdir(parents=True, exist_ok=True)
                tga.write_bytes(b"tga-data")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        runner = WolvenKitRunner(Path("wkit.exe"), executor)

        game_dir = tmp_path / "game"
        archive = game_dir / "archive.archive"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"archive")
        output = tmp_path / "output.tga"

        runner.export_texture(
            game_dir=game_dir,
            archive_path="archive.archive",
            relative_texture_path="base/textures/ad.xbm",
            output_file=output,
            uext="tga",
            logger=_logger(),
        )

        assert len(calls) == 2  # unbundle + export
        for cmd in calls:
            subcmd = _get_subcommand(cmd)
            flags = _extract_flags(cmd)
            assert flags <= ALLOWED_FLAGS[subcmd], (
                f"Unexpected flags in {subcmd} command: {flags - ALLOWED_FLAGS[subcmd]}"
            )


# ---------------------------------------------------------------------------
# pack_archive
# ---------------------------------------------------------------------------


class TestWarningExitCodes:
    """WolvenKit CLI sometimes returns non-zero exit codes for warnings
    (e.g., exit 3 when Oodle DLL is missing) even though the operation
    succeeded.  These should be treated as warnings, not failures.
    """

    def test_exit_code_3_treated_as_warning(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def executor(cmd):
            calls.append(list(cmd))
            subcmd = _get_subcommand(cmd)
            if subcmd == "unbundle":
                out_idx = cmd.index("-o") + 1
                xbm = Path(cmd[out_idx]) / "base/textures/ad.xbm"
                xbm.parent.mkdir(parents=True, exist_ok=True)
                xbm.write_bytes(b"xbm-data")
            return subprocess.CompletedProcess(
                cmd,
                returncode=3,
                stdout="[ 0: Warning     ] - Oodle couldn't be loaded.\n"
                "[ 0: Information ] - Imported 1/1 file(s)\n",
                stderr="",
            )

        runner = WolvenKitRunner(Path("wkit.exe"), executor)

        edited = tmp_path / "edited.tga"
        edited.write_bytes(b"pixels")
        archive = tmp_path / "game" / "archive.archive"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"archive")

        # Should NOT raise — exit code 3 is a warning
        runner.import_texture(
            game_dir=tmp_path / "game",
            archive_path="archive.archive",
            relative_texture_path="base/textures/ad.xbm",
            edited_file=edited,
            packed_root=tmp_path / "packed",
            uext="tga",
            logger=_logger(),
        )

    def test_non_warning_exit_code_still_raises(self, tmp_path: Path) -> None:
        import pytest
        from cp2077_adtex.wolvenkit import WolvenKitError

        def executor(cmd):
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="Error", stderr=""
            )

        runner = WolvenKitRunner(Path("wkit.exe"), executor)
        archive = tmp_path / "game" / "archive.archive"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"archive")

        with pytest.raises(WolvenKitError, match="exit 1"):
            runner.pack_archive(
                packed_root=tmp_path,
                output_archive=tmp_path / "out.archive",
                logger=_logger(),
            )


class TestPackArchiveCommand:
    def test_pack_flags_are_allowed(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def executor(cmd):
            calls.append(list(cmd))
            out_idx = cmd.index("-o") + 1
            archive = Path(cmd[out_idx]) / "packed.archive"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"archive")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        runner = WolvenKitRunner(Path("wkit.exe"), executor)

        packed = tmp_path / "packed"
        packed.mkdir()
        (packed / "dummy.xbm").write_bytes(b"xbm")
        output = tmp_path / "output" / "mod.archive"

        runner.pack_archive(
            packed_root=packed,
            output_archive=output,
            logger=_logger(),
        )

        assert len(calls) == 1
        flags = _extract_flags(calls[0])
        assert flags <= ALLOWED_FLAGS["pack"], (
            f"Unexpected flags in pack command: {flags - ALLOWED_FLAGS['pack']}"
        )
