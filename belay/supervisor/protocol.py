"""Common hook event schema (spec §7.1) and the supervisor IPC envelope.

Every host adapter normalizes its own hook payload into `HookEvent` before
it ever reaches the decision logic in `belay/hooks/`, so `belay/hooks/gate.py`
never has to know which host it's talking to -- one normalized shape, one
decision path, regardless of how many adapters exist later.

Only plain JSON-shaped dicts cross the wire, over `belay/supervisor/wire.py`
(`send_json`/`recv_json`) -- never `Connection.send()`/`.recv()`, which
pickle their argument (see `wire.py`'s docstring for why that matters
against an authenticated-but-not-necessarily-trustworthy peer).
`SupervisorRequest.from_wire`/`SupervisorResponse.from_wire` validate every
field's type explicitly before constructing anything; a malformed dict
raises `ValueError`, never silently coerces or partially constructs an
object from data that doesn't match this shape.
"""

from __future__ import annotations

import getpass
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

SCHEMA_VERSION = 1

Phase = Literal["pre", "post"]
Surface = Literal["shell", "file", "mcp", "other"]
#: Spec §5.2 defines T0/T1/T2. `"UNKNOWN"` is spec §1's separate state
#: ("Belay cannot establish the state safely. It MUST never render this as
#: green or protected") -- included here because an adapter that hasn't
#: actually been verified against a real host binary and pinned version
#: range (spec §7.2's conformance suite; TRUTH-004: "PROTECTED only after
#: its pinned-version end-to-end bypass suite passes") has no honest T0/T1/T2
#: answer to give. See `claude_code_adapter.py`'s `_trust_tier()`.
TrustTier = Literal["T0", "T1", "T2", "UNKNOWN"]


@dataclass(frozen=True)
class HookEvent:
    """Spec §7.1's normalized event -- every field it requires, present
    regardless of which host produced the raw payload."""

    schema_version: int
    installation_id: str
    trust_tier: TrustTier
    host: str
    host_version: str | None
    adapter_version: str
    host_session_id: str
    event_id: str
    phase: Phase
    surface: Surface
    tool_name: str
    normalized_identity: str
    args: dict[str, Any]
    cwd: str | None
    repo_identity: str | None
    os_user: str
    monotonic_ns: int
    wall_clock: str
    # Post-phase only (spec §7.1: "result status, exit code, duration,
    # output digest, and truncation flag for post events").
    result_status: str | None = None
    exit_code: int | None = None
    duration_ms: float | None = None
    output_digest: str | None = None
    truncated: bool | None = None

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


def local_os_user() -> str:
    """The OS user identity, obtained from the OS itself -- never from
    anything in a hook payload, which an adapter's own host process (and,
    transitively, whatever's steering it) controls (spec §7.1: "obtained
    outside agent-supplied text")."""
    try:
        return os.getlogin()
    except OSError:
        return getpass.getuser()


def now_fields() -> tuple[int, str]:
    """(monotonic_ns, wall_clock ISO-8601) -- spec §7.1 wants both: monotonic
    for duration/ordering, wall-clock for human-facing evidence, never one
    computed from the other."""
    return time.monotonic_ns(), datetime.now(UTC).isoformat()


_REQUEST_KINDS = frozenset({"hook_event", "ping", "shutdown"})


@dataclass(frozen=True)
class SupervisorRequest:
    kind: Literal["hook_event", "ping", "shutdown"]
    event: dict[str, Any] | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"kind": self.kind, "event": self.event}

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> SupervisorRequest:
        """Raises `ValueError` on anything that doesn't match this shape --
        never partially constructs a request from data this module doesn't
        recognize."""
        if set(data.keys()) - {"kind", "event"}:
            raise ValueError(f"unexpected fields: {sorted(set(data.keys()) - {'kind', 'event'})}")
        kind = data.get("kind")
        if kind not in _REQUEST_KINDS:
            raise ValueError(f"'kind' must be one of {sorted(_REQUEST_KINDS)}, got {kind!r}")
        event = data.get("event")
        if event is not None and not isinstance(event, dict):
            raise ValueError(f"'event' must be an object or null, got {type(event).__name__}")
        return cls(kind=kind, event=event)


@dataclass(frozen=True)
class SupervisorResponse:
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"ok": self.ok, "payload": self.payload, "error": self.error}

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> SupervisorResponse:
        """Raises `ValueError` on anything that doesn't match this shape."""
        if not isinstance(data.get("ok"), bool):
            raise ValueError(f"'ok' must be a boolean, got {data.get('ok')!r}")
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError(f"'payload' must be an object, got {type(payload).__name__}")
        error = data.get("error")
        if error is not None and not isinstance(error, str):
            raise ValueError(f"'error' must be a string or null, got {type(error).__name__}")
        return cls(ok=data["ok"], payload=payload, error=error)
