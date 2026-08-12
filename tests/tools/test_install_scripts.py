"""scripts/install.sh / scripts/install.ps1 -- the one-line installers advertised
in the README's Install section.

Syntax/sanity checks only (a real network `pip install git+https://...` isn't
run on every test invocation -- that was done manually, once, against the
actual pushed repo: `pip install --quiet "git+https://github.com/Jairogelpi/
belay-mcp.git@main"` into a throwaway venv, then `belay --help` confirmed the
console script works). What's checked here is what a normal `pytest` run can
catch cheaply and would otherwise only surface when someone actually runs the
installer against a fresh machine: shell/PowerShell syntax errors, and that
both scripts agree on the same source repo/behavior.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"


def test_install_sh_exists_and_is_executable() -> None:
    assert INSTALL_SH.is_file()
    import os

    if os.name == "posix":
        assert os.access(INSTALL_SH, os.X_OK)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_install_sh_has_valid_syntax() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    # Windows text-mode pipes translate LF to CRLF, which WSL Bash rejects.
    script = INSTALL_SH.read_bytes()
    result = subprocess.run(
        [bash, "-n"], input=script, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_install_sh_defaults_to_the_real_repo_and_main_branch() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'BELAY_REPO="${BELAY_REPO:-Jairogelpi/belay-mcp}"' in text
    assert 'BELAY_REF="${BELAY_REF:-main}"' in text
    assert "set -eu" in text


def _powershell_exe() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(_powershell_exe() is None, reason="no PowerShell interpreter on PATH")
def test_install_ps1_has_valid_syntax() -> None:
    exe = _powershell_exe()
    assert exe is not None
    script = (
        '$tokens = $null; $errs = $null; '
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_PS1}', [ref]$tokens, [ref]$errs) | Out-Null; "
        'if ($errs.Count -gt 0) { $errs | ForEach-Object { Write-Error $_ }; exit 1 }'
    )
    result = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_install_ps1_defaults_to_the_real_repo_and_main_branch() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert '{ $env:BELAY_REPO } else { "Jairogelpi/belay-mcp" }' in text
    assert '{ $env:BELAY_REF } else { "main" }' in text


def test_readme_advertises_both_one_liners() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "raw.githubusercontent.com/Jairogelpi/belay-mcp/main/scripts/install.sh" in readme
    assert "raw.githubusercontent.com/Jairogelpi/belay-mcp/main/scripts/install.ps1" in readme
