"""The local supervisor (spec ARCH-001/002/006/007): a persistent,
authenticated process holding the `ApprovalQueue`'s connection warm (so a
hook decision doesn't pay Python interpreter cold-start on every single tool
call) and providing duplicate-event idempotency that survives a restart
(`belay/supervisor/idempotency.py` -- SQLite-backed, not an in-memory dict:
a P0 review correctly found that losing an in-memory cache on restart could
re-decide a retried event against changed approval state).

Listens on a `multiprocessing.connection` address (a Windows named pipe or a
POSIX Unix domain socket -- never an unauthenticated TCP port, spec
ARCH-002) with an installation-scoped `authkey` (spec ARCH-003/004,
`belay/supervisor/auth.py`); `multiprocessing.connection` performs an
HMAC-based challenge-response handshake using that key before any payload is
exchanged, so the token itself never crosses the wire.

All durable state (`ApprovalRow`, `HookEventRow`) lives in
`identity.data_path`, under `belay_home()` -- never inside the project this
supervisor is gating. A prior version defaulted to a project-local
`belay-hooks.db`, which a P0 review correctly flagged: an agent with
ordinary project-directory write access could reach that file directly and
edit its own approval state by hand.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

# `AuthenticationError` is defined in `multiprocessing.context` and re-exported
# by `multiprocessing.connection` at runtime, but typeshed's stub for
# `multiprocessing.connection` doesn't declare it -- importing from its actual
# home module keeps mypy happy without a `type: ignore`.
from multiprocessing.connection import Listener
from multiprocessing.context import AuthenticationError
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from belay.approvals.queue import ApprovalQueue
from belay.hooks import claude_code_adapter, gate
from belay.hooks.file_snapshot import SnapshotStore
from belay.hooks.gate import GateDecision
from belay.ledger.store import LedgerStore
from belay.supervisor.addressing import SupervisorIdentity, belay_home
from belay.supervisor.auth import load_or_create_authkey
from belay.supervisor.idempotency import IdempotencyStore, content_digest, event_key
from belay.supervisor.protocol import HookEvent, SupervisorRequest, SupervisorResponse
from belay.supervisor.wire import WireError, recv_json, send_json

logger = logging.getLogger("belay.supervisor")

NormalizeFn = Callable[..., HookEvent]
#: `None` for PostToolUse -- there's no allow/deny decision to render, only
#: an acknowledgement that the result was recorded.
RenderFn = Callable[[GateDecision | None], dict[str, Any]]

#: One entry per supported host: (normalize raw payload -> HookEvent, render
#: GateDecision (or None, for a post-result ack) -> that host's expected
#: response JSON). Adding a host means adding one entry here -- the gate's
#: own decision logic never changes.
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


def _collision_response(hook_event_name: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "permissionDecision": "deny",
            "permissionDecisionReason": "belay: event id collision -- the same event id/session/"
            "phase was already decided with different content; denying rather than trusting "
            "either version",
        }
    }


#: Sentinel telling a worker thread to stop pulling from the connection
#: queue and return -- distinct from `None`, which is a legitimate (if never
#: actually produced) queue item type otherwise.
_STOP = object()


class Supervisor:
    #: How many *accepting* threads and how many *handling* threads run at
    #: once (2x this many daemon threads total). Bounds how many stalled
    #: peers can occupy the supervisor at a time, so a handful can never
    #: block it entirely. Confirmed empirically (see commit history) that
    #: `multiprocessing.connection.Listener.accept()` can safely be called
    #: concurrently from multiple threads on both a POSIX Unix socket and a
    #: Windows named pipe -- a stuck handshake in one thread does not block
    #: `accept()` calls running in the others.
    MAX_WORKERS = 8
    #: How long a connected-and-authenticated peer has to actually send a
    #: request before this connection is given up on. Protects against a
    #: peer that completes the (automatic, sub-second) authkey handshake
    #: and then simply never sends anything -- a local Slowloris.
    RECV_TIMEOUT_S = 10.0

    def __init__(self, identity: SupervisorIdentity) -> None:
        self._identity = identity
        identity.data_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{identity.data_path}", future=True)
        self._queue = ApprovalQueue(engine=engine)
        self._idempotency = IdempotencyStore(engine)
        self._ledger = LedgerStore(engine=engine)
        self._snapshots = SnapshotStore(engine, belay_home() / "snapshots")
        #: PreToolUse's monotonic timestamp, keyed by event_id, so the
        #: matching PostToolUse call (a SEPARATE `belay hooks run`
        #: invocation -- there's no other shared state between them) can
        #: compute a real duration. In-memory only, deliberately: losing an
        #: entry across a supervisor restart just means that one event's
        #: `duration_ms` comes back `None` -- an honest gap, not a
        #: correctness problem the way losing idempotency state would be
        #: (see `belay/supervisor/idempotency.py`).
        self._pre_times: dict[str, int] = {}
        self._pre_times_lock = threading.Lock()

    def _decide_pre(self, event: HookEvent) -> GateDecision:
        if event.surface == "shell" and event.tool_name == "Bash":
            return gate.evaluate(event, self._queue)
        if event.surface == "file":
            return gate.evaluate_file_edit(event, self._queue, self._snapshots)
        return gate.evaluate(event, self._queue)  # its own guard denies non-Bash/non-pre uniformly

    def _decide(self, event: HookEvent, render: RenderFn) -> dict[str, Any]:
        if event.phase == "pre":
            decision = self._decide_pre(event)
            self._ledger.append(
                gate.ledger_session_id(event),
                "hook_pre_tool_use",
                gate.pre_event_evidence(event, decision),
            )
            if event.event_id:
                with self._pre_times_lock:
                    self._pre_times[event.event_id] = event.monotonic_ns
            return render(decision)

        if event.phase == "post":
            duration_ms = None
            if event.event_id:
                with self._pre_times_lock:
                    pre_ns = self._pre_times.pop(event.event_id, None)
                if pre_ns is not None:
                    duration_ms = (event.monotonic_ns - pre_ns) / 1_000_000
            if event.surface == "file" and event.event_id:
                path_str = gate.file_path_from_args(event.args)
                if path_str is not None:
                    self._snapshots.record_after(event.event_id, Path(path_str))
            self._ledger.append(
                gate.ledger_session_id(event),
                "hook_post_tool_use",
                gate.post_event_evidence(event, duration_ms=duration_ms),
            )
            return render(None)

        # Not reachable while Phase is exactly "pre" | "post" (protocol.py),
        # but never guess/crash on a phase this supervisor doesn't know
        # about yet -- deny explicitly, matching gate.evaluate()'s own
        # default for anything other than "pre".
        reason = f"belay: {event.phase}-phase events are not yet handled"
        return render(GateDecision("deny", reason))

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

        if not event.event_id:
            # No durable key can be formed without one -- gate.evaluate()
            # denies this itself (ambiguous identity, spec §7.1), every
            # time, so there's nothing useful to cache anyway.
            return self._decide(event, render)

        key = event_key(event.installation_id, event.host, event.host_session_id,
                         event.event_id, event.phase)
        digest = content_digest(event)

        cached = self._idempotency.get(key)
        if cached is not None:
            if cached.request_digest != digest:
                return _collision_response(event.phase)
            return cached.response

        response = self._decide(event, render)
        winner = self._idempotency.record_if_absent(key, digest, response)
        if winner.request_digest != digest:
            # Lost a race against a DIFFERENT-content event with the same
            # key (only possible under real concurrency) -- same collision
            # handling as the non-racing case above.
            return _collision_response(event.phase)
        return winner.response

    def serve_forever(self) -> None:
        """Runs `MAX_WORKERS` accept threads and `MAX_WORKERS` handler
        threads, all `daemon=True`. Deliberately hand-rolled rather than
        `concurrent.futures.ThreadPoolExecutor`: that pool's worker threads
        are NOT daemon threads (confirmed empirically -- there's no
        constructor option to make them so), and closing the `Listener`
        does not unblock a thread already parked inside `accept()`
        (confirmed empirically too). Together those mean a stuck accept
        call would keep a non-daemon thread alive forever, which keeps the
        whole process alive forever, which is exactly the hang a shutdown
        path must never have. Daemon threads sidestep that: once
        `shutdown_event` is set, this method returns immediately and the
        process can exit even with an accept thread still parked -- the OS
        reclaims it along with everything else when the process actually
        terminates.
        """
        authkey = load_or_create_authkey(self._identity.authkey_path)
        listener = Listener(self._identity.address, authkey=authkey, backlog=self.MAX_WORKERS * 2)
        shutdown_event = threading.Event()
        connections: queue.Queue[Any] = queue.Queue()

        def accept_loop() -> None:
            while not shutdown_event.is_set():
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
                    if shutdown_event.is_set():
                        return  # the listener was closed as part of shutdown -- expected
                    logger.exception("error accepting a connection")
                    continue
                connections.put(conn)

        def worker_loop() -> None:
            while True:
                conn = connections.get()
                if conn is _STOP:
                    return
                self._handle_connection(conn, shutdown_event)

        threads = [
            threading.Thread(target=accept_loop, daemon=True, name=f"belay-accept-{i}")
            for i in range(self.MAX_WORKERS)
        ] + [
            threading.Thread(target=worker_loop, daemon=True, name=f"belay-worker-{i}")
            for i in range(self.MAX_WORKERS)
        ]
        for t in threads:
            t.start()

        logger.info("belay supervisor listening on %s", self._identity.address)
        shutdown_event.wait()
        logger.info("belay supervisor shutting down on request")
        for _ in range(self.MAX_WORKERS):
            connections.put(_STOP)  # let idle workers exit their loop promptly
        listener.close()

    # `listener.accept()` returns `Connection | PipeConnection` (the latter
    # Windows-only, for named pipes) -- typeshed doesn't expose a common
    # base type for both that's convenient to annotate with, hence `Any`
    # rather than fighting the stdlib's own cross-platform typing gap.
    def _handle_connection(self, conn: Any, shutdown_event: threading.Event) -> None:
        """Runs in a worker thread. Sets `shutdown_event` if this connection
        asked the supervisor to stop (after replying, so the client isn't
        left waiting on a connection that was simply dropped). Any I/O
        error on `conn` -- the peer disconnecting, a broken pipe
        mid-response, anything -- is swallowed here: a misbehaving or
        vanished client must never take the supervisor down, or even take
        down this one worker thread in a way that leaks it."""
        try:
            if self._handle_request(conn) == "shutdown":
                shutdown_event.set()
        except (OSError, EOFError):
            logger.warning("connection error while handling a request", exc_info=True)
        finally:
            conn.close()

    def _handle_request(self, conn: Any) -> str | None:
        if not conn.poll(self.RECV_TIMEOUT_S):
            logger.info("closing a connection that sent nothing within %.0fs", self.RECV_TIMEOUT_S)
            return None
        try:
            raw = recv_json(conn)
        except EOFError:
            return None  # peer connected and disconnected without sending anything -- benign
        except WireError as exc:
            logger.warning("rejected a malformed/oversized message: %s", exc)
            send_json(conn, SupervisorResponse(ok=False, error="malformed request").to_wire())
            return None

        try:
            request = SupervisorRequest.from_wire(raw)
        except ValueError as exc:
            response = SupervisorResponse(ok=False, error=f"malformed request: {exc}")
            send_json(conn, response.to_wire())
            return None

        if request.kind == "ping":
            send_json(conn, SupervisorResponse(ok=True, payload={"pong": True}).to_wire())
            return None

        if request.kind == "shutdown":
            send_json(conn, SupervisorResponse(ok=True, payload={"stopping": True}).to_wire())
            return "shutdown"

        if request.kind == "hook_event" and isinstance(request.event, dict):
            host = str(request.event.get("_host", ""))
            payload = {k: v for k, v in request.event.items() if k != "_host"}
            try:
                result = self.handle_hook_event(host, payload)
                send_json(conn, SupervisorResponse(ok=True, payload=result).to_wire())
            except Exception as exc:  # never let a bad event kill the supervisor
                logger.exception("error handling hook event")
                send_json(conn, SupervisorResponse(ok=False, error=str(exc)).to_wire())
            return None

        send_json(
            conn,
            SupervisorResponse(ok=False, error=f"unknown request kind {request.kind!r}").to_wire(),
        )
        return None
