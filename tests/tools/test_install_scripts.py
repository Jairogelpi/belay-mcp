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


def _msys_path(path: Path) -> str:
    """`C:/Users/...` -> `/c/Users/...` -- Git Bash (MSYS)'s own mount
    convention, not a plain drive-letter path with forward slashes (which it
    fails to resolve at all -- confirmed empirically on this Windows
    machine). No-op-ish on POSIX (no drive letter to rewrite)."""
    posix = path.as_posix()
    if len(posix) > 2 and posix[1] == ":":
        return f"/{posix[0].lower()}{posix[2:]}"
    return posix


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_install_sh_has_valid_syntax() -> None:
    # Resolve to the full path via shutil.which and pass THAT as argv[0] --
    # on Windows, a bare "bash" argv[0] goes through CreateProcess's own
    # search order (which includes System32) rather than shutil.which's
    # PATH-only search, and can silently resolve to the WSL bash.exe shim
    # instead of Git Bash -- confirmed on this machine: WSL's bash doesn't
    # understand "/c/..." MSYS-style paths and fails with a confusingly
    # identical "No such file or directory".
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(
        [bash, "-n", _msys_path(INSTALL_SH)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


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
