"""Replay: reconstruct session state from the ledger alone (spec §9.4).

@spec("9.4")
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from belay.ledger.model import Event


class SessionState(BaseModel):
    """Reconstructed session state (spec §9.4): state, verdicts, compensations.

    No access to the original tool servers is used or needed — this is a
    pure fold over the event list.
    """

    session_id: str | None = None
    set_hash: str | None = None
    status: str = "open"
    # E14 (plan-v2): bound once on `session_started`, surfaced here for the
    # whole session (see `belay/ledger/model.py`'s `Event` docstring).
    initiated_by: str | None = None
    on_behalf_of: str | None = None
    steps: dict[int, str] = Field(default_factory=dict)  # step_seq -> last event type
    verdicts: list[dict[str, Any]] = Field(default_factory=list)
    compensations: dict[int, dict[str, Any]] = Field(default_factory=dict)


def replay(events: list[Event]) -> SessionState:
    """Fold `events` into a `SessionState` (spec §9.4).

    Deterministic and pure: no I/O, no tool access, no clocks — the same
    event list always yields the same state.
    """
    state = SessionState()
    for ev in events:
        if state.session_id is None:
            state.session_id = ev.session_id
        if ev.type == "session_started":
            state.status = "open"
            if ev.initiated_by is not None:
                state.initiated_by = ev.initiated_by
            if ev.on_behalf_of is not None:
                state.on_behalf_of = ev.on_behalf_of
        elif ev.type == "contract_set_pinned":
            set_hash = ev.payload.get("set_hash")
            if isinstance(set_hash, str):
                state.set_hash = set_hash
        elif ev.type == "policy_evaluated":
            state.verdicts.append({"step_seq": ev.step_seq, **ev.payload})
        elif ev.type == "compensation_registered" and ev.step_seq is not None:
            state.compensations[ev.step_seq] = dict(ev.payload)
        elif ev.type == "session_closed":
            state.status = "closed"

        if ev.step_seq is not None:
            state.steps[ev.step_seq] = ev.type

    return state
