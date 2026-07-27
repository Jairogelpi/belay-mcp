"""belay/supervisor/server.py + client.py: the IPC layer itself (auth
handshake, idempotency, shutdown), exercised via a real `Supervisor` running
in a background thread within the test process -- real sockets/pipes, real
`multiprocessing.connection` authentication, no mocks of the transport.

No real subprocess spawn here (that's covered end-to-end by
`tests/cli/test_hooks_lifecycle.py`'s `hooks_run` tests, which do spawn a
real detached `belay supervisor serve` process) -- these test the
server/client protocol logic in isolation, fast.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from multiprocessing.connection import AuthenticationError, Client
from pathlib import Path

import pytest
from belay.approvals.queue import ApprovalQueue
from belay.hooks.gate import _plan_id_for_command
from belay.supervisor.addressing import SupervisorIdentity, supervisor_identity
from belay.supervisor.auth import load_or_create_authkey
from belay.supervisor.protocol import SupervisorRequest, SupervisorResponse
from belay.supervisor.server import Supervisor


@pytest.fixture
def running_supervisor(tmp_path: Path) -> Iterator[tuple[SupervisorIdentity, str]]:
    db_path = str((tmp_path / "belay-hooks.db").resolve())
    identity = supervisor_identity(Path(db_path), belay_home=tmp_path / "home")
    supervisor = Supervisor(identity, db_path)
    thread = threading.Thread(target=supervisor.serve_forever, daemon=True)
    thread.start()

    authkey = load_or_create_authkey(identity.authkey_path)
    deadline = time.monotonic() + 5.0
    started = False
    while time.monotonic() < deadline:
        try:
            conn = Client(identity.address, authkey=authkey)
            conn.close()
            started = True
            break
        except OSError:
            time.sleep(0.02)
    if not started:
        pytest.fail("supervisor never started listening")

    yield identity, db_path

    try:
        conn = Client(identity.address, authkey=authkey)
        conn.send(SupervisorRequest(kind="shutdown").to_wire())
        conn.close()
    except OSError:
        pass
    thread.join(timeout=2)


def _send(identity: SupervisorIdentity, request: SupervisorRequest) -> SupervisorResponse:
    authkey = load_or_create_authkey(identity.authkey_path)
    conn = Client(identity.address, authkey=authkey)
    try:
        conn.send(request.to_wire())
        return SupervisorResponse.from_wire(conn.recv())
    finally:
        conn.close()


def test_ping(running_supervisor: tuple[SupervisorIdentity, str]) -> None:
    identity, _ = running_supervisor
    response = _send(identity, SupervisorRequest(kind="ping"))
    assert response.ok
    assert response.payload["pong"] is True


def test_wrong_authkey_is_rejected(running_supervisor: tuple[SupervisorIdentity, str]) -> None:
    identity, _ = running_supervisor
    with pytest.raises(AuthenticationError):
        Client(identity.address, authkey=b"wrong-key-entirely-different-length")


def test_hook_event_for_known_host_round_trips(
    running_supervisor: tuple[SupervisorIdentity, str],
) -> None:
    identity, _ = running_supervisor
    event = {
        "_host": "claude-code",
        "session_id": "s1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "toolu_1",
    }
    response = _send(identity, SupervisorRequest(kind="hook_event", event=event))
    assert response.ok
    assert response.payload["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_hook_event_for_unknown_host_is_denied(
    running_supervisor: tuple[SupervisorIdentity, str],
) -> None:
    identity, _ = running_supervisor
    event = {"_host": "some-unsupported-host", "hook_event_name": "PreToolUse"}
    response = _send(identity, SupervisorRequest(kind="hook_event", event=event))
    assert response.ok
    out = response.payload["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "unsupported host" in out["permissionDecisionReason"]


def test_malformed_request_does_not_crash_the_supervisor(
    running_supervisor: tuple[SupervisorIdentity, str],
) -> None:
    identity, _ = running_supervisor
    authkey = load_or_create_authkey(identity.authkey_path)
    conn = Client(identity.address, authkey=authkey)
    conn.send({"totally": "not a valid request shape"})
    response = SupervisorResponse.from_wire(conn.recv())
    conn.close()
    assert response.ok is False

    # The supervisor must still be alive and answering afterward.
    assert _send(identity, SupervisorRequest(kind="ping")).ok


def test_duplicate_event_id_returns_cached_response_even_after_approval(
    running_supervisor: tuple[SupervisorIdentity, str],
) -> None:
    """Idempotency (spec ARCH-006): the exact same event_id retried must get
    the exact same answer, not be silently re-decided against state that
    has since changed underneath it (here: a human approving the item
    between the two retries)."""
    identity, db_path = running_supervisor
    command = "rm -rf /tmp/dup-test"
    event = {
        "_host": "claude-code",
        "session_id": "s1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": "toolu_dup",
    }

    first = _send(identity, SupervisorRequest(kind="hook_event", event=event))
    assert first.payload["hookSpecificOutput"]["permissionDecision"] == "deny"

    # Approve the underlying item out-of-band, as `belay approvals approve`
    # (a separate process) would against the same db file.
    queue = ApprovalQueue(db_url=f"sqlite:///{db_path}")
    item = queue.for_plan(_plan_id_for_command(command))
    assert item is not None
    queue.approve(item.approval_id, approved_by="jairo")

    # Same event_id again: must still return the ORIGINAL (deny) response,
    # not re-evaluate and see the now-approved state.
    second = _send(identity, SupervisorRequest(kind="hook_event", event=event))
    assert second.payload == first.payload

    # A genuinely NEW event_id for the identical command, though, must see
    # the approval -- idempotency is per-event, not a blanket cache on the
    # command string.
    retry_event = dict(event)
    retry_event["tool_use_id"] = "toolu_dup_retry"
    retried = _send(identity, SupervisorRequest(kind="hook_event", event=retry_event))
    assert retried.payload["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_shutdown_stops_serve_forever(tmp_path: Path) -> None:
    db_path = str((tmp_path / "belay-hooks.db").resolve())
    identity = supervisor_identity(Path(db_path), belay_home=tmp_path / "home")
    supervisor = Supervisor(identity, db_path)
    thread = threading.Thread(target=supervisor.serve_forever, daemon=True)
    thread.start()

    authkey = load_or_create_authkey(identity.authkey_path)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            conn = Client(identity.address, authkey=authkey)
            conn.close()
            break
        except OSError:
            time.sleep(0.02)

    response = _send(identity, SupervisorRequest(kind="shutdown"))
    assert response.ok
    thread.join(timeout=2)
    assert not thread.is_alive()
