"""Real test evidence: run the *declared* test, not any command that happens to pass.

The gap this closes (found in three rounds of review):

1. `_belay_test_ref` is a bare string the agent itself supplies --
   `tests/fake.py::test_ok` shows up as "proven" whether or not that test
   exists or passes.
2. Fixing (1) with a free-form `--cmd` only proved "some command exited
   0", not "the declared test passed" -- an agent could self-attest by
   pairing a fake ref with an unrelated passing command.
3. Fixing (2) by building the runner's command from `test_ref` via
   `str.format` into a `shell=True` string was itself a shell-injection
   hole: `test_ref` is agent-supplied, so `"tests/x.py::test_y; true"`
   (or `&&`, `&`, backticks, `$()` on POSIX; `&`/`|` under cmd.exe on
   Windows) ran arbitrary shell syntax appended after the intended
   command, and a passing injected command recorded a passing result
   under `mode="test"` with no test having run at all -- reproduced with
   pytest not even installed.

Fix: `RUNNERS` maps to an **argv list**, and every runner invocation uses
`subprocess.run(argv, shell=False, ...)` -- `test_ref` is passed as a
single argv element, never concatenated into or interpreted as shell
syntax. There is no string for shell metacharacters to escape out of.

Also captures real git context (HEAD SHA, tree hash, dirty flag) and who
ran the verification -- a test result with no record of what tree it ran
against is not reproducible evidence. If the tree is dirty, the recorded
`tree_hash` reflects HEAD, not the actual modified working tree the test
ran against -- `belay causal`/`export-pr` label this case
"VERIFIED ON DIRTY TREE" rather than a plain VERIFIED, since it isn't
fully reproducible from the recorded hash alone.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass, field

RUNNERS: dict[str, list[str]] = {
    "pytest": ["pytest"],
    "jest": ["npx", "jest", "-t"],
    "go": ["go", "test", "-run"],
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


def _run_shell(cmd: str, cwd: str | None, timeout: float) -> tuple[int, str]:
    """Run an operator-typed shell string. Never used for agent-supplied `test_ref`."""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return -1, (stdout + stderr) or "(timed out)"


def _run_argv(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str]:
    """Run an argv list with `shell=False` -- no shell ever parses `argv`'s elements,
    so nothing in them (agent-supplied `test_ref` included) can inject shell syntax."""
    try:
        result = subprocess.run(
            argv, shell=False, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return -1, (stdout + stderr) or "(timed out)"
    except FileNotFoundError as exc:
        return 127, f"(runner not found: {exc})"


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
    """Run an operator-typed shell command. Recorded as `mode: "command"` -- never
    treated as proof of a specific declared test, since nothing ties `cmd` to any
    `test_ref`. `cmd` must come from the human/CI running `verify-test`, never from
    agent-controlled data -- use `run_declared_test` for anything derived from
    `_belay_test_ref`."""
    started = time.monotonic()
    exit_code, output = _run_shell(cmd, cwd, timeout)
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
    """Run the test named by `test_ref` via `RUNNERS[runner] + [test_ref]`, executed
    with `shell=False` -- `test_ref` (agent-supplied, untrusted) is passed as a single
    argv element, never interpolated into or parsed as a shell string, so shell
    metacharacters in it (`;`, `&&`, `&`, backticks, `$()`, newlines) have no shell to
    be interpreted by. Recorded as `mode: "test"`, the only mode `belay causal`/
    `export-pr` will ever label VERIFIED."""
    if runner not in RUNNERS:
        raise ValueError(f"unknown runner {runner!r} (known: {', '.join(RUNNERS)})")
    argv = [*RUNNERS[runner], test_ref]
    started = time.monotonic()
    exit_code, output = _run_argv(argv, cwd, timeout)
    duration_ms = int((time.monotonic() - started) * 1000)
    output_hash = "sha256:" + hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()
    return TestRunResult(
        mode="test",
        cmd=" ".join(argv),
        test_ref=test_ref,
        exit_code=exit_code,
        output_hash=output_hash,
        duration_ms=duration_ms,
        passed=exit_code == 0,
        git=_git_context(cwd),
        verified_by=verified_by,
    )
