"""Real test evidence: run the *declared* test, not any command that happens to pass.

The gap this closes (found in two rounds of review): `_belay_test_ref` is a
bare string the agent itself supplies -- `tests/fake.py::test_ok` shows up
as "proven" whether or not that test exists or passes. The first version
of `belay verify-test` fixed "ran nothing" but not "ran the *right*
thing": `--cmd` was arbitrary, so `_belay_test_ref="tests/fake.py::test_ok"`
plus `--cmd "python -c pass"` recorded a passing result under that step
with no relationship between the label and what actually ran -- an agent
with shell access could self-attest.

Fix: `run_declared_test` takes the step's *own* `test_ref` (read from the
ledger, not re-typed by whoever runs `verify-test`) and a `runner` template
(`{"pytest": "pytest {test_ref}", ...}`) -- the command is built
mechanically from data already on the step, never handed in free-form for
that path. A free-form `--cmd` still exists for cases with no
`_belay_test_ref` at all, but its result is recorded as `mode: "command"`
and `belay causal`/`export-pr` never call that "VERIFIED test" -- only
`mode: "test"` (built from the step's own ref) can be.

Also captures real git context (HEAD SHA, tree hash, dirty flag) and who
ran the verification -- a test result with no record of what tree it ran
against is not reproducible evidence.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass, field

RUNNERS: dict[str, str] = {
    "pytest": "pytest {test_ref}",
    "jest": "npx jest -t {test_ref}",
    "go": "go test -run {test_ref}",
}


@dataclass(frozen=True)
class GitContext:
    head: str | None
    tree_hash: str | None
    dirty: bool | None


@dataclass(frozen=True)
class TestRunResult:
    mode: str  # "test" (built from the step's own test_ref) or "command" (free-form --cmd)
    cmd: str
    test_ref: str | None
    exit_code: int
    output_hash: str
    duration_ms: int
    passed: bool
    git: GitContext = field(default_factory=lambda: GitContext(None, None, None))
    verified_by: str | None = None


def _run(cmd: str, cwd: str | None, timeout: float) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return -1, (stdout + stderr) or "(timed out)"


def _git_context(cwd: str | None) -> GitContext:
    def _git(*args: str) -> str | None:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else None

    head = _git("rev-parse", "HEAD")
    tree_hash = _git("rev-parse", "HEAD^{tree}")
    status = _git("status", "--porcelain")
    dirty = None if status is None else bool(status.strip())
    return GitContext(head=head, tree_hash=tree_hash, dirty=dirty)


def run_command(
    cmd: str, cwd: str | None = None, timeout: float = 300.0, verified_by: str | None = None
) -> TestRunResult:
    """Run an arbitrary command. Recorded as `mode: "command"` -- never treated as proof
    of a specific declared test, since nothing ties `cmd` to any `test_ref`."""
    started = time.monotonic()
    exit_code, output = _run(cmd, cwd, timeout)
    duration_ms = int((time.monotonic() - started) * 1000)
    output_hash = "sha256:" + hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()
    return TestRunResult(
        mode="command",
        cmd=cmd,
        test_ref=None,
        exit_code=exit_code,
        output_hash=output_hash,
        duration_ms=duration_ms,
        passed=exit_code == 0,
        git=_git_context(cwd),
        verified_by=verified_by,
    )


def run_declared_test(
    test_ref: str,
    runner: str,
    cwd: str | None = None,
    timeout: float = 300.0,
    verified_by: str | None = None,
) -> TestRunResult:
    """Run the test named by `test_ref`, with the command built mechanically from
    `RUNNERS[runner]` -- never a free-form string an operator could substitute
    something else into. Recorded as `mode: "test"`, the only mode `belay
    causal`/`export-pr` will ever label VERIFIED."""
    if runner not in RUNNERS:
        raise ValueError(f"unknown runner {runner!r} (known: {', '.join(RUNNERS)})")
    cmd = RUNNERS[runner].format(test_ref=test_ref)
    started = time.monotonic()
    exit_code, output = _run(cmd, cwd, timeout)
    duration_ms = int((time.monotonic() - started) * 1000)
    output_hash = "sha256:" + hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()
    return TestRunResult(
        mode="test",
        cmd=cmd,
        test_ref=test_ref,
        exit_code=exit_code,
        output_hash=output_hash,
        duration_ms=duration_ms,
        passed=exit_code == 0,
        git=_git_context(cwd),
        verified_by=verified_by,
    )
