"""E22 Task 8: `scripts/smoke_connect.py`'s driver, run from inside the
normal dev test suite (against `python -m belay.cli.main` as the "belay
executable" -- CI's `wheel-smoke` job instead points it at a real,
wheel-installed `belay` console script). Requires `npx` (Node.js) on PATH
-- the driver spawns the real, pinned Filesystem MCP server -- and skips
cleanly if unavailable, matching this project's established pattern."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import smoke_connect  # noqa: E402

pytestmark = pytest.mark.slow


def _require_npx() -> None:
    if shutil.which("npx") is None:
        pytest.skip("npx (Node.js) not found on PATH -- needed to run the real filesystem server")


def test_smoke_connect_end_to_end(tmp_path: Path) -> None:
    _require_npx()
    smoke_connect.run_smoke(
        belay_cmd=[sys.executable, "-m", "belay.cli.main"],
        bin_dir=tmp_path / "bin",
        home=tmp_path / "home",
        project=tmp_path / "project",
    )  # must not raise


def test_smoke_connect_cli_entrypoint_returns_zero(tmp_path: Path) -> None:
    _require_npx()
    exit_code = smoke_connect.main(
        [
            "--belay", f"{sys.executable} -m belay.cli.main",
            "--bin-dir", str(tmp_path / "bin"),
            "--home", str(tmp_path / "home"),
            "--project", str(tmp_path / "project"),
        ]
    )
    assert exit_code == 0


def test_smoke_connect_fails_loudly_when_belay_command_is_broken(tmp_path: Path) -> None:
    """A sanity check on the driver itself: it must not report success (or
    hang) when the thing it's driving is broken -- proves this smoke test
    would actually catch a real regression, not just always pass."""
    _require_npx()
    with pytest.raises(smoke_connect.SmokeFailure):
        smoke_connect.run_smoke(
            belay_cmd=[sys.executable, "-c", "import sys; sys.exit(1)"],
            bin_dir=tmp_path / "bin",
            home=tmp_path / "home",
            project=tmp_path / "project",
        )


# --------------------------------------------------------------------------
# E23 Task 3: frozen-launch assertion
# --------------------------------------------------------------------------


def test_assert_frozen_launch_command_accepts_absolute_binary_with_run_config() -> None:
    smoke_connect.assert_frozen_launch_command(
        ["/opt/belay/dist-bin/belay", "run", "--config", "/tmp/belay.wrap.json"]
    )  # must not raise


def test_assert_frozen_launch_command_accepts_windows_exe() -> None:
    smoke_connect.assert_frozen_launch_command(
        [r"C:\dist-bin\belay.exe", "run", "--config", r"C:\proj\belay.wrap.json"]
    )  # must not raise


def test_assert_frozen_launch_command_rejects_empty_argv() -> None:
    with pytest.raises(smoke_connect.SmokeFailure, match="empty command"):
        smoke_connect.assert_frozen_launch_command([])


@pytest.mark.parametrize("interpreter", ["python", "python3", "py"])
def test_assert_frozen_launch_command_rejects_python_interpreter(interpreter: str) -> None:
    with pytest.raises(smoke_connect.SmokeFailure, match="python interpreter"):
        smoke_connect.assert_frozen_launch_command(
            [interpreter, "-m", "belay.cli.main", "run", "--config", "belay.wrap.json"]
        )


def test_assert_frozen_launch_command_rejects_belay_cli_main_module_argument() -> None:
    with pytest.raises(smoke_connect.SmokeFailure, match=r"belay\.cli\.main"):
        smoke_connect.assert_frozen_launch_command(
            ["/some/interpreter-shaped-path", "-m", "belay.cli.main", "run", "--config", "x"]
        )


def test_assert_frozen_launch_command_rejects_missing_run_or_config() -> None:
    with pytest.raises(smoke_connect.SmokeFailure, match=r"'run'/'--config'"):
        smoke_connect.assert_frozen_launch_command(["/opt/belay/dist-bin/belay", "--help"])


def test_smoke_connect_frozen_shim_registers_absolute_binary_not_python(tmp_path: Path) -> None:
    """Drives the real end-to-end smoke against `write_fake_frozen_belay`'s
    shim (which monkeypatches `sys.frozen`/`sys.executable` the same way a
    real PyInstaller binary's own bootloader effectively does) -- proves
    `--expect-frozen` passes for a genuinely frozen-shaped registration,
    without a real multi-minute PyInstaller build in the fast dev suite."""
    _require_npx()
    frozen_bin_dir = tmp_path / "frozen-bin"
    frozen_belay = smoke_connect.write_fake_frozen_belay(frozen_bin_dir)
    smoke_connect.run_smoke(
        belay_cmd=[str(frozen_belay)],
        bin_dir=tmp_path / "bin",
        home=tmp_path / "home",
        project=tmp_path / "project",
        expect_frozen=True,
    )  # must not raise


def test_smoke_connect_expect_frozen_catches_a_python_dash_m_regression(tmp_path: Path) -> None:
    """The other side of the previous test: proves `--expect-frozen` is not
    a vacuous check by pointing it at the genuinely non-frozen dev
    invocation (`python -m belay.cli.main`) -- which legitimately
    registers a python-dependent command -- and confirming that is
    correctly rejected."""
    _require_npx()
    with pytest.raises(smoke_connect.SmokeFailure, match="python interpreter"):
        smoke_connect.run_smoke(
            belay_cmd=[sys.executable, "-m", "belay.cli.main"],
            bin_dir=tmp_path / "bin",
            home=tmp_path / "home",
            project=tmp_path / "project",
            expect_frozen=True,
        )
