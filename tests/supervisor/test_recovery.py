"""Recovery scenarios (ARCH-007, see
docs/adr/0020-extended-requirement-catalog.md; review point 8): abandoned
pipe/socket
after a hard kill, and durability across a restart. Spawns real `belay
supervisor serve` subprocesses (not threads) and kills them forcefully
(`Process.kill()` -- SIGKILL on POSIX, TerminateProcess on Windows, not a
graceful shutdown request) to actually exercise "the process died without
any cleanup", not just "serve_forever() returned normally".

What's NOT covered here, said plainly rather than assumed: a genuinely
abandoned POSIX AF_UNIX socket *file* left on disk after a hard kill (does a
later `Listener` bind cleanly reuse/replace it, or fail with "address
already in use"?) -- this suite runs on Windows, where the equivalent
resource is a named pipe that the OS reclaims immediately on process death
with no leftover file at all, so that specific POSIX behavior is untested
here. A symlink-substituted token path is also untested: creating symlinks
on Windows generally requires elevated privileges this environment doesn't
have.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from belay.approvals.queue import ApprovalQueue
from belay.supervisor.addressing import supervisor_identity
from belay.supervisor.lifecycle import ensure_running, is_listening


@pytest.fixture(autouse=True)
def _isolated_belay_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "belay-home"
    monkeypatch.setenv("BELAY_HOME", str(home))
    return home


def _spawn_supervisor(db_path: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "belay.cli.main", "supervisor", "serve", "--db", str(db_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_hard_killed_supervisor_is_detected_as_not_listening(tmp_path: Path) -> None:
    db_path = tmp_path / "belay-hooks.db"
    identity = supervisor_identity(db_path.resolve())

    proc = _spawn_supervisor(db_path)
    try:
        assert _wait_until(lambda: is_listening(identity)), "supervisor never came up"

        proc.kill()  # hard kill -- no graceful shutdown, no cleanup
        proc.wait(timeout=5)

        assert _wait_until(lambda: not is_listening(identity)), (
            "is_listening() still reports the hard-killed supervisor as reachable"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_a_fresh_supervisor_can_be_spawned_after_a_hard_kill(tmp_path: Path) -> None:
    """The practical consequence of the test above: `belay hooks run`'s own
    on-demand spawn (`ensure_running`) must recover cleanly -- not get
    stuck thinking a dead supervisor is still there, and not fail to bind
    the pipe/socket address because a previous (now-dead) process appeared
    to still own it."""
    db_path = tmp_path / "belay-hooks.db"
    identity = supervisor_identity(db_path.resolve())

    proc = _spawn_supervisor(db_path)
    try:
        assert _wait_until(lambda: is_listening(identity))
        proc.kill()
        proc.wait(timeout=5)
        assert _wait_until(lambda: not is_listening(identity))

        assert ensure_running(identity, str(db_path)) is True
        assert is_listening(identity)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        from belay.supervisor.client import send_shutdown

        send_shutdown(identity)


def test_pending_approval_survives_a_hard_kill_and_is_visible_to_a_fresh_supervisor(
    tmp_path: Path,
) -> None:
    """Durability across an unclean restart (ARCH-007): an approval
    queued by supervisor A must still be there -- not lost, not corrupted --
    for supervisor B (a fresh process, same identity) after A is killed
    without any graceful shutdown."""
    db_path = tmp_path / "belay-hooks.db"
    identity = supervisor_identity(db_path.resolve())

    proc_a = _spawn_supervisor(db_path)
    try:
        assert _wait_until(lambda: is_listening(identity))

        from belay.supervisor.client import send_hook_event

        payload = {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/recovery-test"},
            "tool_use_id": "toolu_recovery_1",
        }
        result = send_hook_event(identity, str(db_path), "claude-code", payload)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

        proc_a.kill()
        proc_a.wait(timeout=5)
        assert _wait_until(lambda: not is_listening(identity))
    finally:
        if proc_a.poll() is None:
            proc_a.kill()
            proc_a.wait(timeout=5)

    # The item must be readable directly from the durable store -- no
    # supervisor process involved at all here, proving it's the SQLite file
    # that's durable, not some in-memory state that happened to survive.
    queue = ApprovalQueue(db_url=f"sqlite:///{identity.data_path}")
    pending = [i for i in queue.list() if i.state == "pending"]
    assert len(pending) == 1
    approval_id = pending[0].approval_id

    # A fresh supervisor B must serve the retried identical event from that
    # same durable state, not silently lose it or re-decide from scratch.
    proc_b = _spawn_supervisor(db_path)
    try:
        assert _wait_until(lambda: is_listening(identity))

        from belay.supervisor.client import send_hook_event

        retry_payload = {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/recovery-test"},
            "tool_use_id": "toolu_recovery_1",  # SAME event_id as before the kill
        }
        retried = send_hook_event(identity, str(db_path), "claude-code", retry_payload)
        assert retried["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert approval_id in retried["hookSpecificOutput"]["permissionDecisionReason"]

        queue.approve(approval_id, approved_by="jairo")
        new_event_payload = dict(retry_payload)
        new_event_payload["tool_use_id"] = "toolu_recovery_2"  # a genuinely new event
        approved_result = send_hook_event(identity, str(db_path), "claude-code", new_event_payload)
        assert approved_result["hookSpecificOutput"]["permissionDecision"] == "allow"
    finally:
        from belay.supervisor.client import send_shutdown

        send_shutdown(identity)
        if proc_b.poll() is None:
            proc_b.wait(timeout=5)
