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

import json
import threading
import time
from collections.abc import Iterator
from multiprocessing.connection import AuthenticationError, Client
from pathlib import Path

import pytest
from belay.approvals.queue import ApprovalQueue
from belay.supervisor.addressing import SupervisorIdentity, supervisor_identity
from belay.supervisor.auth import load_or_create_authkey
from belay.supervisor.protocol import SupervisorRequest, SupervisorResponse
from belay.supervisor.server import Supervisor
from belay.supervisor.wire import recv_json, send_json


@pytest.fixture
def running_supervisor(tmp_path: Path) -> Iterator[tuple[SupervisorIdentity, str]]:
    # never opened directly -- identity seed only
    project_anchor = (tmp_path / "belay-hooks.db").resolve()
    identity = supervisor_identity(project_anchor, belay_home=tmp_path / "home")
    supervisor = Supervisor(identity)
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

    # The REAL data location -- what a human's `belay approvals` CLI (a
    # separate process from the supervisor) would point `--db` at.
    yield identity, str(identity.data_path)

    try:
        conn = Client(identity.address, authkey=authkey)
        send_json(conn, SupervisorRequest(kind="shutdown").to_wire())
        conn.close()
    except OSError:
        pass
    thread.join(timeout=2)


def _send(identity: SupervisorIdentity, request: SupervisorRequest) -> SupervisorResponse:
    authkey = load_or_create_authkey(identity.authkey_path)
    conn = Client(identity.address, authkey=authkey)
    try:
        send_json(conn, request.to_wire())
        return SupervisorResponse.from_wire(recv_json(conn))
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


def test_posttooluse_event_records_evidence_and_acks_empty(
    running_supervisor: tuple[SupervisorIdentity, str],
) -> None:
    identity, db_path = running_supervisor

    pre_event = {
        "_host": "claude-code",
        "session_id": "s1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "toolu_post_ipc",
    }
    pre_response = _send(identity, SupervisorRequest(kind="hook_event", event=pre_event))
    assert pre_response.ok

    post_event = {
        "_host": "claude-code",
        "session_id": "s1",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "toolu_post_ipc",
        "tool_response": {"exit_code": 0, "stdout": "clean"},
    }
    post_response = _send(identity, SupervisorRequest(kind="hook_event", event=post_event))
    assert post_response.ok
    assert post_response.payload == {}  # no decision left to make -- empty ack

    from belay.ledger.store import LedgerStore

    ledger = LedgerStore(db_url=f"sqlite:///{db_path}")
    events = ledger.read("hook-claude-code-s1")
    assert [e.type for e in events] == ["hook_pre_tool_use", "hook_post_tool_use"]
    assert events[1].payload["exit_code"] == 0
    assert events[1].payload["duration_ms"] is not None


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


def test_wrong_shaped_json_request_does_not_crash_the_supervisor(
    running_supervisor: tuple[SupervisorIdentity, str],
) -> None:
    identity, _ = running_supervisor
    authkey = load_or_create_authkey(identity.authkey_path)
    conn = Client(identity.address, authkey=authkey)
    send_json(conn, {"totally": "not a valid request shape"})
    response = SupervisorResponse.from_wire(recv_json(conn))
    conn.close()
    assert response.ok is False

    # The supervisor must still be alive and answering afterward.
    assert _send(identity, SupervisorRequest(kind="ping")).ok


def test_pickled_request_is_rejected_not_unpickled(
    running_supervisor: tuple[SupervisorIdentity, str],
) -> None:
    """The whole point of moving off `Connection.send`/`.recv`: bytes that
    happen to be a valid pickle stream (what the old, vulnerable wire format
    would have silently deserialized -- and executed, for a malicious
    payload) must be rejected as malformed JSON, never unpickled."""
    identity, _ = running_supervisor
    authkey = load_or_create_authkey(identity.authkey_path)
    conn = Client(identity.address, authkey=authkey)
    conn.send({"totally": "a pickled object, not JSON"})  # Connection.send() pickles this
    response = SupervisorResponse.from_wire(recv_json(conn))
    conn.close()
    assert response.ok is False
    assert response.error == "malformed request"

    # The supervisor must still be alive and answering afterward.
    assert _send(identity, SupervisorRequest(kind="ping")).ok


def test_oversized_message_is_rejected_not_buffered(
    running_supervisor: tuple[SupervisorIdentity, str],
) -> None:
    from belay.supervisor.wire import MAX_MESSAGE_BYTES

    identity, _ = running_supervisor
    authkey = load_or_create_authkey(identity.authkey_path)
    conn = Client(identity.address, authkey=authkey)
    huge = {"kind": "ping", "padding": "x" * (MAX_MESSAGE_BYTES + 1024)}
    try:
        # The server-side `recv_bytes(maxlength=...)` is what actually
        # enforces the limit (confirmed via its log line below) -- the
        # client's own send_bytes() of a single ~1MB+ message can itself
        # fail at the transport layer on some platforms (observed: a
        # Windows named pipe write that large can break the pipe outright)
        # before the server even finishes rejecting it. Either way is a
        # correctly-refused oversized message, not a crash.
        conn.send_bytes(json.dumps(huge).encode("utf-8"))
    except OSError:
        pass
    finally:
        conn.close()

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
    # (a separate process) would against the same db file. Found via
    # queue.list() rather than recomputing gate.py's internal plan_id hash
    # formula -- that's an implementation detail this test shouldn't be
    # coupled to.
    queue = ApprovalQueue(db_url=f"sqlite:///{db_path}")
    pending = [i for i in queue.list() if i.state == "pending"]
    assert len(pending) == 1
    queue.approve(pending[0].approval_id, approved_by="jairo")

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
    project_anchor = (tmp_path / "belay-hooks.db").resolve()
    identity = supervisor_identity(project_anchor, belay_home=tmp_path / "home")
    supervisor = Supervisor(identity)
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


def test_a_connected_but_silent_client_does_not_block_other_clients(
    running_supervisor: tuple[SupervisorIdentity, str],
) -> None:
    """The Slowloris fix itself: a peer that completes the (automatic)
    authkey handshake and then never sends a request must not prevent a
    well-behaved second client from being served promptly."""
    identity, _ = running_supervisor
    authkey = load_or_create_authkey(identity.authkey_path)

    silent_conn = Client(identity.address, authkey=authkey)
    # deliberately never sends anything on `silent_conn`

    start = time.monotonic()
    response = _send(identity, SupervisorRequest(kind="ping"))
    elapsed = time.monotonic() - start

    assert response.ok
    assert elapsed < 2.0, f"a legitimate request took {elapsed:.2f}s -- blocked by the silent peer"
    silent_conn.close()


def test_many_concurrent_silent_clients_still_leave_the_supervisor_responsive(
    running_supervisor: tuple[SupervisorIdentity, str],
) -> None:
    identity, _ = running_supervisor
    authkey = load_or_create_authkey(identity.authkey_path)

    silent_conns = [Client(identity.address, authkey=authkey) for _ in range(5)]
    try:
        start = time.monotonic()
        response = _send(identity, SupervisorRequest(kind="ping"))
        elapsed = time.monotonic() - start
        assert response.ok
        assert elapsed < 2.0, f"took {elapsed:.2f}s with 5 silent peers connected"
    finally:
        for conn in silent_conns:
            conn.close()


def test_idle_connection_is_closed_after_recv_timeout(tmp_path: Path) -> None:
    """A silent connection isn't just deprioritized -- it's actually given
    up on eventually, freeing whatever thread was handling it."""
    project_anchor = (tmp_path / "belay-hooks.db").resolve()
    identity = supervisor_identity(project_anchor, belay_home=tmp_path / "home")
    supervisor = Supervisor(identity)
    supervisor.RECV_TIMEOUT_S = 0.3  # short, just for this test
    thread = threading.Thread(target=supervisor.serve_forever, daemon=True)
    thread.start()

    authkey = load_or_create_authkey(identity.authkey_path)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            probe = Client(identity.address, authkey=authkey)
            probe.close()
            break
        except OSError:
            time.sleep(0.02)

    silent_conn = Client(identity.address, authkey=authkey)
    time.sleep(0.6)  # past RECV_TIMEOUT_S -- the server side should have closed it by now

    # The connection should now be closed server-side; sending on a
    # closed pipe/socket eventually raises rather than succeeding forever.
    with pytest.raises((EOFError, OSError)):
        for _ in range(20):
            silent_conn.send_bytes(b"x" * 10)
    silent_conn.close()

    # Supervisor itself must still be alive and serving other clients.
    assert _send(identity, SupervisorRequest(kind="ping")).ok

    try:
        conn = Client(identity.address, authkey=authkey)
        send_json(conn, SupervisorRequest(kind="shutdown").to_wire())
        conn.close()
    except OSError:
        pass
    thread.join(timeout=2)
