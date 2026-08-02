"""The local supervisor (ARCH-001/002/006/007, see
docs/adr/0020-extended-requirement-catalog.md): a persistent,
authenticated process holding the `ApprovalQueue`'s connection warm (so a
hook decision doesn't pay Python interpreter cold-start on every single tool
call) and providing duplicate-event idempotency that survives a restart
(`belay/supervisor/idempotency.py` -- SQLite-backed, not an in-memory dict:
a P0 review correctly found that losing an in-memory cache on restart could
re-decide a retried event against changed approval state).

Listens on a `multiprocessing.connection` address (a Windows named pipe or a
POSIX Unix domain socket -- never an unauthenticated TCP port,
ARCH-002) with an installation-scoped `authkey` (ARCH-003/004,
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

import contextlib
import json
import logging
import os
import queue
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

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
from belay.contracts.loader import load_contract_set
from belay.contracts.model import ContractSet
from belay.errors import BelayError
from belay.hooks import claude_code_adapter, gate
from belay.hooks.decision import ExtraAllowlist, load_extra_allowlist
from belay.hooks.file_snapshot import SnapshotStore
from belay.hooks.gate import GateDecision
from belay.hooks.quota import QuotaConfig
from belay.ledger.model import STEP_COMMITTED, STEP_FAILED, STEP_INDETERMINATE
from belay.ledger.store import LedgerStore
from belay.policy.quota import parse_window
from belay.rewind.service import is_fenced
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


@dataclass(frozen=True)
class ConfigUnavailable:
    """R1.6: returned by `_load_contract_set`/`_load_quota_config`/
    `_load_extra_allowlist` when a pointer file exists (this install
    opted into that config via `belay hooks install`) but its target is
    now missing, unreadable, or invalid -- distinct from `None`/`()`,
    which mean "never configured" and stay permissive by design.

    Before R1.6, both cases collapsed to the same permissive fallback:
    a corrupted or deleted contracts/quota/allowlist file silently
    reverted the gate to pre-opt-in behavior with no signal to the
    operator. Once a security opt-in has been activated, that must be an
    invariant this process enforces, not a best-effort nicety that
    quietly degrades -- `_decide_pre` denies every event outright
    (`configured_policy_unavailable`) while any of the three is in this
    state, rather than trusting an agent's call against a policy nobody
    can currently confirm is actually loaded."""

    reason: str


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
        # `identity.lock_path` and (on POSIX) `identity.address` share the
        # same "run" directory (belay/supervisor/addressing.py) -- normally
        # already created as a side effect of the spawning client's own
        # `_acquire_spawn_lock` call before this process ever starts
        # (belay/supervisor/lifecycle.py), but this constructor must not
        # silently depend on always being reached that way: a direct
        # `belay supervisor serve` invocation (the CLI's own documented
        # "debugging or explicit service-manager integration" use case) or
        # a test constructing `Supervisor` directly never goes through that
        # path at all. On POSIX, `Listener(identity.address, ...)` binding
        # a Unix domain socket into a directory that doesn't exist yet
        # fails with FileNotFoundError -- invisible on Windows, where named
        # pipes aren't backed by a real filesystem directory the same way,
        # which is exactly why this went uncaught until real POSIX CI ran
        # (plan-v2 E19.7).
        identity.lock_path.parent.mkdir(parents=True, exist_ok=True)
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
        #: R1.8 prerequisite (ADR 0026): a per-`ledger_session_id`
        #: monotonic step counter, giving hook-gated calls a real int
        #: `step_seq` -- `belay/rewind/service.py::build_plan` and
        #: `belay/policy/baseline.py`'s anomaly counting both silently
        #: skip any event with `step_seq is None`, so without this,
        #: nothing written here would ever be visible to that machinery.
        #: Same in-memory-only, lost-on-restart posture as `_pre_times`
        #: above, for the same reason: an honest, accepted gap (a handful
        #: of step_seqs restarting from 1 after a restart), never a
        #: correctness problem the way losing idempotency/approval state
        #: would be.
        self._hook_step_seq: dict[str, int] = {}
        self._hook_step_seq_by_event_id: dict[str, int] = {}
        self._contract_set = self._load_contract_set()
        self._quota = self._load_quota_config()
        self._extra_allowlist = self._load_extra_allowlist()

    def _load_contract_set(self) -> ContractSet | ConfigUnavailable | None:
        """`None` (the default) is unchanged legacy behavior -- no
        `belay hooks install --contracts <file>` ever wrote
        `identity.contracts_pointer_path` for this install, so there was
        never anything to fail closed over. Once that pointer file exists,
        though, a missing/unreadable/invalid target is no longer treated
        as "unconfigured" -- it returns `ConfigUnavailable` (R1.6) so
        `_decide_pre` denies everything instead of silently reverting to
        the pre-opt-in unconditional-allow behavior."""
        pointer = self._identity.contracts_pointer_path
        if not pointer.is_file():
            return None
        try:
            contracts_path = pointer.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return ConfigUnavailable(f"could not read contracts pointer file: {exc}")
        if not contracts_path:
            return ConfigUnavailable("contracts pointer file is empty")
        try:
            return load_contract_set([contracts_path])
        except (OSError, BelayError) as exc:
            return ConfigUnavailable(
                f"configured contracts file {contracts_path!r} is unreadable/invalid: {exc}"
            )

    def _load_quota_config(self) -> QuotaConfig | ConfigUnavailable | None:
        """`None` (the default) means no quota check at all -- only set when
        `belay hooks install --quota-max` wrote `identity.quota_config_path`
        for this install. Same R1.6 posture as `_load_contract_set`: once
        that pointer file exists, a corrupt/invalid target returns
        `ConfigUnavailable` rather than silently falling back to "no quota
        check"."""
        pointer = self._identity.quota_config_path
        if not pointer.is_file():
            return None
        try:
            raw = json.loads(pointer.read_text(encoding="utf-8"))
            max_actions = raw["max_actions"]
            window = parse_window(raw["window"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return ConfigUnavailable(f"quota config file is unreadable/invalid: {exc}")
        return QuotaConfig(ledger=self._ledger, max_actions=max_actions, window=window)

    def _load_extra_allowlist(self) -> ExtraAllowlist | ConfigUnavailable:
        """`()` (the default) means no operator-configured entries --
        exactly `belay/hooks/decision.py::classify_bash`'s own default.
        Same R1.6 posture as `_load_contract_set`/`_load_quota_config`:
        once `identity.extra_allowlist_pointer_path` exists, a corrupt/
        invalid target returns `ConfigUnavailable` rather than silently
        falling back to no extra entries."""
        pointer = self._identity.extra_allowlist_pointer_path
        if not pointer.is_file():
            return ()
        try:
            allowlist_path = pointer.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return ConfigUnavailable(f"could not read allowlist pointer file: {exc}")
        if not allowlist_path:
            return ConfigUnavailable("allowlist pointer file is empty")
        try:
            return load_extra_allowlist(allowlist_path)
        except (OSError, ValueError) as exc:
            return ConfigUnavailable(
                f"configured allowlist file {allowlist_path!r} is unreadable/invalid: {exc}"
            )

    def _decide_pre(self, event: HookEvent) -> GateDecision:
        # R1.6: once a security opt-in has been activated for this install
        # (a pointer file exists), its target becoming unreadable/invalid
        # must deny everything, not silently revert to unconfigured
        # (permissive) behavior -- checked first, before fencing or any
        # per-surface gate, since a broken configured policy makes every
        # decision below untrustworthy regardless of surface.
        contract_set = self._contract_set
        quota = self._quota
        extra_allowlist = self._extra_allowlist
        broken: list[str] = []
        if isinstance(contract_set, ConfigUnavailable):
            broken.append(f"contracts ({contract_set.reason})")
            contract_set = None
        if isinstance(quota, ConfigUnavailable):
            broken.append(f"quota ({quota.reason})")
            quota = None
        if isinstance(extra_allowlist, ConfigUnavailable):
            broken.append(f"extra Bash allowlist ({extra_allowlist.reason})")
            extra_allowlist = ()
        if broken:
            return GateDecision(
                "deny",
                "belay: configured_policy_unavailable -- this install opted into "
                f"stricter config that is now unreadable/invalid: {'; '.join(broken)} -- "
                "fix or remove the affected pointer file(s) and restart the supervisor; "
                "denying rather than silently reverting to unconfigured behavior",
            )
        # R1 third slice (ADR 0021): a session fenced via `belay hooks
        # fence` is closed to every surface, checked once here rather than
        # duplicated in each evaluate_*() -- fencing is a ledger fact,
        # exactly like `belay/rewind/service.py::is_fenced()` already is
        # for the MCP proxy path, so it holds even across a supervisor
        # restart, not just in-process state.
        if is_fenced(self._ledger, gate.session_key(event.host, event.host_session_id)):
            return GateDecision(
                "deny",
                "belay: this session is fenced (closed to new actions by `belay hooks "
                "fence`) -- no new tool calls are accepted for it",
            )
        if event.surface == "shell" and event.tool_name == "Bash":
            return gate.evaluate(
                event, self._queue, quota=quota, extra_allowlist=extra_allowlist
            )
        if event.surface == "file":
            return gate.evaluate_file_edit(
                event,
                self._queue,
                self._snapshots,
                contract_set=contract_set,
                quota=quota,
            )
        if event.surface == "mcp":
            return gate.evaluate_mcp_call(
                event, self._queue, contract_set=contract_set, quota=quota
            )
        # its own guard denies unrecognized surfaces
        return gate.evaluate(event, self._queue, quota=quota, extra_allowlist=extra_allowlist)

    def _decide(self, event: HookEvent, render: RenderFn) -> dict[str, Any]:
        if event.phase == "pre":
            decision = self._decide_pre(event)
            session_id = gate.ledger_session_id(event)
            self._ledger.append(
                session_id,
                "hook_pre_tool_use",
                gate.pre_event_evidence(event, decision),
            )
            if event.event_id:
                with self._pre_times_lock:
                    self._pre_times[event.event_id] = event.monotonic_ns
                    step_seq = self._hook_step_seq.get(session_id, 0) + 1
                    self._hook_step_seq[session_id] = step_seq
                    self._hook_step_seq_by_event_id[event.event_id] = step_seq
                # R1.8 prerequisite (ADR 0026): a real `plan_created`-shaped
                # event for this hook-gated call, so MCP-side machinery
                # that reads `plan_created` (anomaly counting, `causal`
                # display) has something real to see on this surface for
                # the first time -- evidence only, assembled after
                # `decision` already exists, never consulted by it.
                contract_set_for_evidence = (
                    self._contract_set if isinstance(self._contract_set, ContractSet) else None
                )
                self._ledger.append(
                    session_id,
                    "plan_created",
                    gate.plan_created_evidence(event, contract_set_for_evidence),
                    step_seq=step_seq,
                )
                # `belay/rewind/service.py::build_plan` reads a step's
                # tool name from `step_journaled` specifically (not
                # `plan_created`) -- without this, every hooks-sourced
                # `RewindStepPlan.tool` would show "?" instead of the
                # real tool name. Minimal payload: just enough for that
                # one lookup, not a claim this is a real saga journal
                # entry (`belay/executor/saga.py`'s own `step_journaled`
                # carries far more -- contract, reversibility -- none of
                # which exists on this surface).
                self._ledger.append(
                    session_id,
                    "step_journaled",
                    {"tool": event.tool_name},
                    step_seq=step_seq,
                )
            return render(decision)

        if event.phase == "post":
            duration_ms = None
            session_id = gate.ledger_session_id(event)
            outcome_step_seq: int | None = None
            if event.event_id:
                with self._pre_times_lock:
                    pre_ns = self._pre_times.pop(event.event_id, None)
                    outcome_step_seq = self._hook_step_seq_by_event_id.pop(event.event_id, None)
                if pre_ns is not None:
                    duration_ms = (event.monotonic_ns - pre_ns) / 1_000_000
            if event.surface == "file" and event.event_id:
                path_str = gate.file_path_from_args(event.args)
                if path_str is not None:
                    self._snapshots.record_after(event.event_id, Path(path_str))
            self._ledger.append(
                session_id,
                "hook_post_tool_use",
                gate.post_event_evidence(event, duration_ms=duration_ms),
            )
            if outcome_step_seq is not None:
                # R1.8 prerequisite (ADR 0026): the outcome half of the
                # same step-lifecycle presence -- `STEP_INDETERMINATE`
                # (not `STEP_FAILED`) for an unrecognized/missing
                # `result_status`, honestly reflecting "we don't know
                # what happened" rather than guessing it failed.
                if event.result_status == "success":
                    outcome_type = STEP_COMMITTED
                elif event.result_status == "failure":
                    outcome_type = STEP_FAILED
                else:
                    outcome_type = STEP_INDETERMINATE
                if outcome_type is STEP_COMMITTED:
                    # `belay/ledger/verify.py::verify_coherence` requires
                    # `result_recorded`/`compensation_registered` for
                    # every `step_committed` step (spec §9.2) -- without
                    # these, a hooks session with any committed step would
                    # now fail coherence verification where it used to
                    # trivially pass (no step_committed existed to check
                    # at all). Both are honest, not fabricated:
                    # `result_recorded` reuses the same result fields
                    # already in `hook_post_tool_use`; `compensation_registered`
                    # truthfully declares `reversible: False` -- there is
                    # no MCP-shaped compensation mechanism on this surface
                    # (the real undo path for native file edits is
                    # `belay hooks rewind`'s separate `SnapshotStore`, per
                    # ADR 0026's rewind-duplication finding), so this
                    # matches exactly what `RewindService.build_plan`
                    # already defaults to when the event is absent -- it
                    # makes the classification explicit and coherence-valid,
                    # it does not change what that classification is.
                    self._ledger.append(
                        session_id,
                        "result_recorded",
                        {
                            "tool": event.tool_name,
                            "result_status": event.result_status,
                            "exit_code": event.exit_code,
                            "output_digest": event.output_digest,
                        },
                        step_seq=outcome_step_seq,
                    )
                    self._ledger.append(
                        session_id,
                        "compensation_registered",
                        {
                            "reversible": False,
                            "reason": "no_mcp_shaped_compensation_on_the_hooks_surface",
                        },
                        step_seq=outcome_step_seq,
                    )
                self._ledger.append(
                    session_id,
                    outcome_type,
                    {
                        "event_id": event.event_id,
                        "tool_name": event.tool_name,
                        "result_status": event.result_status,
                    },
                    step_seq=outcome_step_seq,
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
            # denies this itself (ambiguous identity -- its own rule, not
            # spec §7.1, which is about the approval queue, not HookEvent),
            # every time, so there's nothing useful to cache anyway.
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
        if sys.platform != "win32":
            # A hard-killed (not gracefully shut down) prior supervisor for
            # this same identity leaves its Unix domain socket FILE behind
            # on disk -- unlike Windows named pipes, which the OS reclaims
            # automatically when the owning process dies. bind() on that
            # stale path fails with "address already in use" even though
            # nothing is actually listening there, blocking every future
            # respawn for this install until the file is manually removed.
            # Safe to unlink unconditionally here: by the time this runs,
            # the caller (ensure_running -> _acquire_spawn_lock) has
            # already won the exclusive right to spawn for this identity
            # and confirmed via is_listening() that nothing is genuinely
            # listening there -- a real, verified bug (plan-v2 E19.7,
            # confirmed via real Linux/macOS CI:
            # test_a_fresh_supervisor_can_be_spawned_after_a_hard_kill),
            # not a hypothetical one.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._identity.address)
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
