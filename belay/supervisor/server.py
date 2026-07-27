"""The local supervisor (spec ARCH-001/002/006/007): a persistent,
authenticated process holding the `ApprovalQueue`'s connection warm (so a
hook decision doesn't pay Python interpreter cold-start on every single tool
call) and providing duplicate-event idempotency across the lifetime of one
supervisor process.

Listens on a `multiprocessing.connection` address (a Windows named pipe or a
POSIX Unix domain socket -- never an unauthenticated TCP port, spec
ARCH-002) with an installation-scoped `authkey` (spec ARCH-003/004,
`belay/supervisor/auth.py`); `multiprocessing.connection` performs an
HMAC-based challenge-response handshake using that key before any payload is
exchanged, so the token itself never crosses the wire.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

# `AuthenticationError` is defined in `multiprocessing.context` and re-exported
# by `multiprocessing.connection` at runtime, but typeshed's stub for
# `multiprocessing.connection` doesn't declare it -- importing from its actual
# home module keeps mypy happy without a `type: ignore`.
from multiprocessing.connection import Listener
from multiprocessing.context import AuthenticationError
from typing import Any

from belay.approvals.queue import ApprovalQueue
from belay.hooks import claude_code_adapter, gate
from belay.hooks.gate import GateDecision
from belay.supervisor.addressing import SupervisorIdentity
from belay.supervisor.auth import load_or_create_authkey
from belay.supervisor.protocol import HookEvent, SupervisorRequest, SupervisorResponse

logger = logging.getLogger("belay.supervisor")

NormalizeFn = Callable[..., HookEvent]
RenderFn = Callable[[GateDecision], dict[str, Any]]

#: One entry per supported host: (normalize raw payload -> HookEvent, render
#: GateDecision -> that host's expected response JSON). Adding a host means
#: adding one entry here -- the gate's own decision logic never changes.
_ADAPTERS: dict[str, tuple[NormalizeFn, RenderFn]] = {
    "claude-code": (claude_code_adapter.normalize, claude_code_adapter.render_response),
}


def _unknown_host_response(host: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "unknown",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"belay: unsupported host {host!r} -- denying rather "
            "than silently allowing an unrecognized adapter",
        }
    }


class Supervisor:
    def __init__(self, identity: SupervisorIdentity, db_path: str) -> None:
        self._identity = identity
        self._queue = ApprovalQueue(db_url=f"sqlite:///{db_path}")
        #: Duplicate event IDs MUST be idempotent (spec ARCH-006). In-memory
        #: is sufficient: the underlying decision is already idempotent by
        #: construction (deterministic classification + the durable
        #: ApprovalQueue lookup by plan_id), so losing this cache on
        #: restart never changes the *correctness* of a re-decided event --
        #: it only means a retried event after a restart is re-evaluated
        #: instead of instantly replayed, which produces the same answer.
        self._seen_events: dict[str, dict[str, Any]] = {}

    def handle_hook_event(self, host: str, raw_payload: dict[str, Any]) -> dict[str, Any]:
        adapter = _ADAPTERS.get(host)
        if adapter is None:
            return _unknown_host_response(host)
        normalize, render = adapter

        try:
            event = normalize(raw_payload, installation_id=self._identity.install_id)
        except (ValueError, KeyError) as exc:
            return {
                "hookSpecificOutput": {
                    "hookEventName": raw_payload.get("hook_event_name", "unknown"),
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"belay: could not normalize event: {exc}",
                }
            }

        if event.event_id and event.event_id in self._seen_events:
            return self._seen_events[event.event_id]

        decision = gate.evaluate(event, self._queue)
        response = render(decision)
        if event.event_id:
            self._seen_events[event.event_id] = response
        return response

    def serve_forever(self) -> None:
        authkey = load_or_create_authkey(self._identity.authkey_path)
        with Listener(self._identity.address, authkey=authkey) as listener:
            logger.info("belay supervisor listening on %s", self._identity.address)
            while True:
                try:
                    conn = listener.accept()
                except AuthenticationError:
                    # A connection attempt with the wrong (or no) authkey.
                    # This MUST NOT be allowed to kill the supervisor --
                    # otherwise anyone who can merely reach the pipe/socket
                    # (not even the right key, just a connection attempt)
                    # could take down protection for every legitimate hook
                    # call after it. Reject and keep serving.
                    logger.warning("rejected a connection with an invalid authkey")
                    continue
                except OSError:
                    logger.exception("error accepting a connection")
                    continue

                try:
                    if self._handle_connection(conn) == "shutdown":
                        logger.info("belay supervisor shutting down on request")
                        return
                finally:
                    conn.close()

    # `listener.accept()` returns `Connection | PipeConnection` (the latter
    # Windows-only, for named pipes) -- typeshed doesn't expose a common
    # base type for both that's convenient to annotate with, hence `Any`
    # rather than fighting the stdlib's own cross-platform typing gap.
    def _handle_connection(self, conn: Any) -> str | None:
        """Returns `"shutdown"` if this connection asked the supervisor to
        stop (after replying, so the client isn't left waiting on a
        connection that was simply dropped). Any I/O error on `conn` --
        the peer disconnecting, a broken pipe mid-response, anything --
        is swallowed here: a misbehaving or vanished client must never
        take the supervisor down for every *other* connection."""
        try:
            return self._handle_request(conn)
        except (OSError, EOFError):
            logger.warning("connection error while handling a request", exc_info=True)
            return None

    def _handle_request(self, conn: Any) -> str | None:
        try:
            raw = conn.recv()
        except EOFError:
            return None  # peer connected and disconnected without sending anything -- benign

        request: SupervisorRequest | None
        try:
            request = SupervisorRequest(**raw) if isinstance(raw, dict) else None
        except TypeError:
            request = None

        if request is None:
            conn.send(SupervisorResponse(ok=False, error="malformed request").to_wire())
            return None

        if request.kind == "ping":
            conn.send(SupervisorResponse(ok=True, payload={"pong": True}).to_wire())
            return None

        if request.kind == "shutdown":
            conn.send(SupervisorResponse(ok=True, payload={"stopping": True}).to_wire())
            return "shutdown"

        if request.kind == "hook_event" and isinstance(request.event, dict):
            host = str(request.event.get("_host", ""))
            payload = {k: v for k, v in request.event.items() if k != "_host"}
            try:
                result = self.handle_hook_event(host, payload)
                conn.send(SupervisorResponse(ok=True, payload=result).to_wire())
            except Exception as exc:  # never let a bad event kill the supervisor
                logger.exception("error handling hook event")
                conn.send(SupervisorResponse(ok=False, error=str(exc)).to_wire())
            return None

        conn.send(
            SupervisorResponse(ok=False, error=f"unknown request kind {request.kind!r}").to_wire()
        )
        return None
