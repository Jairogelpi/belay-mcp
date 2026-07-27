"""Common hook event schema (spec §7.1) and the supervisor IPC envelope.

Every host adapter normalizes its own hook payload into `HookEvent` before
it ever reaches the decision logic in `belay/hooks/`, so `belay/hooks/gate.py`
never has to know which host it's talking to -- one normalized shape, one
decision path, regardless of how many adapters exist later.

Only plain JSON-serializable dicts cross the wire (never a pickled dataclass
or arbitrary object): `to_wire()`/`from_wire()` round-trip through a plain
dict deliberately, even though `multiprocessing.connection` would happily
pickle a dataclass directly -- the channel is authenticated before any
payload is exchanged, but keeping the wire format to plain JSON-shaped data
is a second, independent boundary that costs nothing here.
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
TrustTier = Literal["T0", "T1", "T2"]


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

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> HookEvent:
        return cls(**data)


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


@dataclass(frozen=True)
class SupervisorRequest:
    kind: Literal["hook_event", "ping", "shutdown"]
    event: dict[str, Any] | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"kind": self.kind, "event": self.event}


@dataclass(frozen=True)
class SupervisorResponse:
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"ok": self.ok, "payload": self.payload, "error": self.error}

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> SupervisorResponse:
        return cls(ok=data["ok"], payload=data.get("payload") or {}, error=data.get("error"))
