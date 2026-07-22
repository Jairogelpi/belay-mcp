"""`belay-conformance` console script (plan.md E8 exit criterion)."""

from __future__ import annotations

import subprocess

import pytest
from conformance.cli import app
from typer.testing import CliRunner

runner = CliRunner()


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "conformance" in result.stdout.lower()


@pytest.mark.slow  # spins up a full nested pytest run over the L1-L3 scenarios
def test_run_against_belay_target_in_process_reports_l3_pass() -> None:
    result = runner.invoke(app, ["run", "--target", "belay", "--level", "3"])
    assert result.exit_code == 0, result.stdout
    assert "L3 PASSED" in result.stdout


@pytest.mark.slow
def test_belay_conformance_console_script_runs_as_a_real_subprocess() -> None:
    """The literal exit criterion (plan.md E8-d): `pip install ... && belay-conformance
    run --target belay --level 3` works, run for real as its own process -- not asserted
    to work, actually invoked."""
    completed = subprocess.run(
        ["belay-conformance", "run", "--target", "belay", "--level", "3"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "L3 PASSED" in completed.stdout
