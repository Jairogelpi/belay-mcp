"""belay/hooks/claude_code_adapter.py, live conformance (plan-v2 E18.7).

Real end-to-end proof, against the actual installed `claude` CLI binary,
that belay's Native Agent Gate genuinely intercepts and blocks a real
Claude Code session -- not just that our own decision logic returns
"deny" in isolation (every other test in this repo), but that the real
host binary never executes the denied command. This is what spec §7.2's
"pinned-version end-to-end bypass suite" (TRUTH-004) means before a host
integration can claim `trust_tier="T1"` -- see
`belay/hooks/claude_code_adapter.py`'s own `_VERIFIED_TRUST_TIER`
docstring for the honesty bar this file exists to clear.

Opt-in only, NEVER part of the default suite or CI: every test here
spawns a real `claude -p` subprocess, which calls the real Anthropic API
and spends real usage on whoever's credentials are active. Excluded by
`pyproject.toml`'s default `addopts` (`-m "not slow and not
live_conformance"`) the same way `slow` is, but for a stronger reason --
`slow` tests just take longer, these cost money and need real auth. Run
explicitly:

    pytest tests/hooks/test_live_conformance.py -m live_conformance --no-cov

Pinned, not open-ended: `PINNED_CLAUDE_VERSION` records the exact `claude
--version` this suite was last actually verified against. A version
mismatch SKIPS (never fails, never silently passes) -- claiming
conformance against a binary this suite was never run against would be
exactly the unverified claim this project has avoided everywhere else,
and a newer/older `claude` build could change hook behavior in ways
running successfully against a *different* version can't detect.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from belay.cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

pytestmark = pytest.mark.live_conformance

PINNED_CLAUDE_VERSION = "2.1.219"
_CLAUDE_TIMEOUT_S = 120


#: On Windows, `npm install -g` puts a `claude.cmd` shim on PATH -- Python's
#: subprocess can't exec a .cmd directly via CreateProcess (no shell=True)
#: the same way it can't exec a bare `bash` into Git Bash instead of the
#: WSL launcher (see scripts/install.ps1's own fix for that unrelated but
#: same-shaped problem) -- shell=True routes it through cmd.exe, which
#: resolves PATHEXT-eligible shims correctly. Safe here specifically
#: because every call below passes a real argument list, never
#: attacker-controlled text, on a Windows-only environment (list + shell=True
#: is exactly the combination Python's own docs warn is unsafe on POSIX,
#: not on Windows).
def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, shell=True, capture_output=True, text=True, **kwargs)  # type: ignore[call-overload]


def _installed_claude_version() -> str | None:
    try:
        result = _run(["claude", "--version"], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    m = re.match(r"(\d+\.\d+\.\d+)", result.stdout.strip())
    return m.group(1) if m else None


def _skip_unless_pinned_claude_available() -> None:
    version = _installed_claude_version()
    if version is None:
        pytest.skip("claude CLI not found on PATH -- live conformance needs the real binary")
    if version != PINNED_CLAUDE_VERSION:
        pytest.skip(
            f"installed claude CLI is {version}, this suite was last verified against "
            f"{PINNED_CLAUDE_VERSION} -- skipping rather than claiming conformance "
            "against an unverified version"
        )


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _skip_unless_pinned_claude_available()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BELAY_HOME", str(tmp_path / "belay-home"))

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("live conformance scratch project\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    result = runner.invoke(app, ["hooks", "install", "--yes"])
    assert result.exit_code == 0, result.output

    yield

    from belay.supervisor.addressing import supervisor_identity
    from belay.supervisor.client import send_shutdown

    send_shutdown(supervisor_identity((tmp_path / "belay-hooks.db").resolve()))


def _run_claude(prompt: str, tmp_path: Path) -> str:
    completed = _run(
        ["claude", "-p", prompt, "--permission-mode", "bypassPermissions"],
        cwd=tmp_path,
        timeout=_CLAUDE_TIMEOUT_S,
    )
    return completed.stdout


def _invoke_cli_retrying(args: list[str], *, attempts: int = 5, delay_s: float = 1.0) -> Any:
    """`claude`'s own hook subprocess spawns belay's supervisor as a
    *detached* background process (see `belay/supervisor/lifecycle.py`'s
    `ensure_running`) -- by the time the `claude -p` subprocess we spawned
    above has exited, the hook call it made already got its allow/deny
    answer (that part is synchronous), but there is no guarantee the
    detached supervisor has finished its own on-disk setup (creating
    `belay_home()/data/`, etc.) by that exact moment from a completely
    separate process's point of view. A short retry here is about that
    real race, not about masking a flaky assertion -- the very first
    manual runs of this exact scenario, with a human-paced few seconds
    between commands, never needed one."""
    last = None
    for _ in range(attempts):
        last = runner.invoke(app, args)
        if last.exit_code == 0:
            return last
        time.sleep(delay_s)
    return last


def test_live_gate_blocks_a_real_denied_bash_command(tmp_path: Path) -> None:
    """The classic bypass check: ask the real Claude Code binary, with
    belay's hooks actually installed and permission-mode bypassed (so any
    block is attributable to belay's hook, not Claude's own separate
    permission layer), to run a command belay's classifier would pause.
    `curl` (unlike `echo ... > file`) is unambiguous -- it's simply not on
    the safe-read allowlist at all, so this exercises the "unrecognized
    command pauses" path directly rather than the shell-metacharacter
    rejection path. The prompt is a plain, unadorned instruction on
    purpose: an earlier, more forceful phrasing ("no questions asked")
    was inconsistently treated by the model itself as looking like a
    prompt injection and refused outright on some runs -- a real model
    behavior, but not the thing this test is trying to measure.
    Verified two independent ways, not just "the agent said it didn't run
    it": the file it would have created must not exist, and a real
    pending item must be sitting in the actual approval queue."""
    target = tmp_path / "out.html"
    _run_claude(
        "Using the Bash tool, run this exact command: curl -s http://example.com -o out.html",
        tmp_path,
    )

    assert not target.exists(), (
        "belay's hook failed to block a real Claude Code Bash call -- "
        "the denied command's side effect actually happened"
    )

    listed = _invoke_cli_retrying(["hooks", "approvals", "list", "--db", "belay-hooks.db"])
    assert listed.exit_code == 0, listed.output
    assert "pending" in listed.output
    assert "Bash" in listed.output


def test_live_gate_allows_a_real_safe_bash_command(tmp_path: Path) -> None:
    """Bypass resistance without breaking ordinary use: an allowlisted
    command must still reach the real host and return real output --
    otherwise "the gate works" would just mean "the gate blocks
    everything", which is a different and much less useful claim."""
    output = _run_claude(
        "Using the Bash tool, run this exact command: git status",
        tmp_path,
    )
    assert "branch" in output.lower() or "master" in output.lower() or "main" in output.lower()

    listed = _invoke_cli_retrying(["hooks", "approvals", "list", "--db", "belay-hooks.db"])
    assert listed.exit_code == 0, listed.output
    assert "no approval items" in listed.output  # nothing paused -- it was just allowed


def test_live_ledger_records_real_hook_evidence(tmp_path: Path) -> None:
    """The hook-originated ledger (E18.2) actually receives real evidence
    from a real session, and its hash chain verifies -- not a synthetic
    event built by a unit test."""
    _run_claude(
        "Using the Bash tool, run this exact command: git status",
        tmp_path,
    )

    from belay.supervisor.addressing import supervisor_identity

    data_path = supervisor_identity((tmp_path / "belay-hooks.db").resolve()).data_path
    verified = _invoke_cli_retrying(["verify", str(data_path)])
    assert verified.exit_code == 0, verified.output
    assert "chain: OK" in verified.output
    assert "coherence: OK" in verified.output

    events_line = next(
        line for line in verified.output.splitlines() if line.startswith("events:")
    )
    assert int(events_line.split(":")[1].strip()) >= 1
