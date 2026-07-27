"""Thin IPC client used by `belay hooks run`: connects to (spawning if
necessary) the per-install supervisor, sends one hook event, returns its
decision JSON. Fails closed (spec 8.1: "Supervisor unavailable" / "Hook
timeout" both default to deny for a mutation) rather than ever leaving a
PreToolUse call without an answer.
"""

from __future__ import annotations

from multiprocessing.connection import Client
from typing import Any

from belay.supervisor.addressing import SupervisorIdentity
from belay.supervisor.auth import load_or_create_authkey
from belay.supervisor.lifecycle import ensure_running
from belay.supervisor.protocol import SupervisorRequest, SupervisorResponse

_RECV_TIMEOUT_S = 5.0


def _fail_closed_response(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"belay: {reason} -- failing closed "
            "(mutation denied), spec 8.1",
        }
    }


def send_hook_event(
    identity: SupervisorIdentity, db_path: str, host: str, raw_payload: dict[str, Any]
) -> dict[str, Any]:
    if not ensure_running(identity, db_path):
        return _fail_closed_response("supervisor unavailable")

    authkey = load_or_create_authkey(identity.authkey_path)
    try:
        conn = Client(identity.address, authkey=authkey)
    except OSError:
        return _fail_closed_response("supervisor unreachable")

    try:
        event_wire: dict[str, Any] = dict(raw_payload)
        event_wire["_host"] = host
        request = SupervisorRequest(kind="hook_event", event=event_wire)
        conn.send(request.to_wire())
        if not conn.poll(_RECV_TIMEOUT_S):
            return _fail_closed_response("hook timed out waiting for supervisor")
        raw_response = conn.recv()
    except (OSError, EOFError):
        return _fail_closed_response("lost connection to supervisor")
    finally:
        conn.close()

    response = SupervisorResponse.from_wire(raw_response)
    if not response.ok:
        return _fail_closed_response(f"supervisor error: {response.error}")
    return response.payload


def send_shutdown(identity: SupervisorIdentity) -> bool:
    """Ask a running supervisor to stop. Returns False (not an error) if
    none was reachable -- there's nothing to stop, which is the desired end
    state either way."""
    authkey = load_or_create_authkey(identity.authkey_path)
    try:
        conn = Client(identity.address, authkey=authkey)
    except OSError:
        return False
    try:
        conn.send(SupervisorRequest(kind="shutdown").to_wire())
        conn.poll(_RECV_TIMEOUT_S)
    except OSError:
        pass
    finally:
        conn.close()
    return True
