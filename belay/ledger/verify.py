"""Chain and coherence verification (spec §9.2).

@spec("9.2")
"""

from __future__ import annotations

from belay.ledger.model import GENESIS_HASH, Event, VerifyReport
from belay.ledger.store import compute_hash

# For a committed step, these event types MUST all be present (spec §9.2:
# "every committed step has its journal, capture (if contracted), result,
# and registered compensation"). `state_captured` is conditional on the
# contract declaring a `capture` block, so it is not required here — the
# ledger alone can't tell whether one was declared.
_REQUIRED_FOR_COMMITTED = ("step_journaled", "result_recorded", "compensation_registered")


def verify_chain(events: list[Event]) -> VerifyReport:
    """Recompute the hash chain (spec §9.2).

    Chains are partitioned by `session_id`. Reports the index (within
    `events`, as given) of the first event whose `prev_hash` or `hash`
    doesn't check out.
    """
    last_hash_by_session: dict[str, str] = {}
    for i, ev in enumerate(events):
        expected_prev = last_hash_by_session.get(ev.session_id, GENESIS_HASH)
        if ev.prev_hash != expected_prev:
            return VerifyReport(
                ok=False,
                errors=[f"event {i} ({ev.event_id}): prev_hash does not chain from prior event"],
                failed_index=i,
                failed_event_id=ev.event_id,
            )
        recomputed = compute_hash(ev)
        if recomputed != ev.hash:
            return VerifyReport(
                ok=False,
                errors=[f"event {i} ({ev.event_id}): hash does not match its recomputed value"],
                failed_index=i,
                failed_event_id=ev.event_id,
            )
        last_hash_by_session[ev.session_id] = ev.hash
    return VerifyReport(ok=True)


def verify_coherence(events: list[Event]) -> VerifyReport:
    """Check per-step evidence coherence (spec §9.2).

    Every `step_committed` step must also have a `step_journaled`,
    `result_recorded`, and `compensation_registered` event for the same
    step. Every `compensation_executed` event must reference a step that
    was committed.
    """
    by_session: dict[str, dict[int, set[str]]] = {}
    for ev in events:
        if ev.step_seq is None:
            continue
        types = by_session.setdefault(ev.session_id, {}).setdefault(ev.step_seq, set())
        types.add(ev.type)

    errors: list[str] = []
    for session_id, steps in by_session.items():
        for step_seq, types in steps.items():
            if "step_committed" in types:
                for required in _REQUIRED_FOR_COMMITTED:
                    if required not in types:
                        errors.append(
                            f"session {session_id} step {step_seq}: committed without {required}"
                        )
            if "compensation_executed" in types and "step_committed" not in types:
                errors.append(
                    f"session {session_id} step {step_seq}: "
                    "compensation_executed references a step that was never committed"
                )
    return VerifyReport(ok=not errors, errors=errors)
