"""Real test evidence: run a command for real, record what actually happened.

The gap this closes: `_belay_test_ref` (`belay/proxy/server.py`,
`belay/cli/causal.py`) is a bare string the agent itself supplies --
`tests/fake.py::test_ok` shows up as "proven" whether or not that test
exists or passes. Belay does not execute anything on the live safety path
(no LLM, no test runner, in `govern_and_execute`) -- but post-hoc, the same
way `belay export-pr` turns a committed session into a reviewable PR,
`belay verify-test` can actually run the claimed command and record the
real outcome: exit code, a hash of its combined output (not the output
itself -- could be large or carry secrets), and wall-clock duration.

Recorded as ledger event type `belay:test_verified` (colon-prefixed,
deliberately outside `belay.ledger.model.EVENT_TYPES` -- adoption/DX, not
one of spec §9.1's normative closed set; nothing validates `Event.type`
against that tuple at runtime, so this doesn't touch it). `belay causal`/
`belay export-pr` distinguish three tiers per step: *verified* (a
`belay:test_verified` event with `exit_code == 0` exists), *claimed* (only
a bare `_belay_test_ref` label, never actually run), *unproven* (neither).
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class TestRunResult:
    cmd: str
    exit_code: int
    output_hash: str
    duration_ms: int
    passed: bool


def run_test(cmd: str, cwd: str | None = None, timeout: float = 300.0) -> TestRunResult:
    """Actually execute `cmd` (shell) and capture its real outcome. Never guesses."""
    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        exit_code = result.returncode
        output = (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired as exc:
        exit_code = -1
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        output = (stdout + stderr) or "(timed out)"
    duration_ms = int((time.monotonic() - started) * 1000)
    output_hash = "sha256:" + hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()
    return TestRunResult(
        cmd=cmd,
        exit_code=exit_code,
        output_hash=output_hash,
        duration_ms=duration_ms,
        passed=exit_code == 0,
    )
