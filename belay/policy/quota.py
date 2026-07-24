"""Per-identity rolling quota of approved-and-executed irreversible actions (plan-v2 E15).

Same philosophy as E10's `belay.policy.baseline.BaselineStore`: read prior
ledger events, keep no second parallel in-memory store of truth. Unlike
E10 (per-session), quota is scoped per `initiated_by` **identity** (E14) --
one identity's sessions can span many `session_id`s, so `QuotaTracker`
reads `LedgerStore.read_all()` and groups by session to find which ones
belong to the identity in question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from belay.ledger.model import Event
from belay.ledger.store import LedgerStore

_WINDOW_RE = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_window(text: str) -> timedelta:
    """Parse `"1d"` / `"7d"` / `"12h"` / `"30m"` / `"45s"` into a `timedelta`."""
    match = _WINDOW_RE.match(text.strip())
    if match is None:
        raise ValueError(f"invalid quota window {text!r}, expected e.g. '1d', '7d', '12h'")
    value, unit = match.groups()
    return timedelta(seconds=int(value) * _UNIT_SECONDS[unit])


@dataclass
class QuotaTracker:
    """Counts one identity's approved-and-executed irreversible actions within a rolling window."""

    ledger: LedgerStore

    def count(self, identity: str, *, now: datetime, window: timedelta) -> int:
        """Count `identity`'s irreversible actions that were both approved (or
        auto-allowed) *and* actually executed, with the deciding
        `policy_evaluated` event timestamped within `window` of `now`.

        Boundary rule: an event exactly `window` old still counts (`now - at
        <= window`); anything older does not.
        """
        cutoff = now - window
        events_by_session: dict[str, list[Event]] = {}
        for event in self.ledger.read_all():
            events_by_session.setdefault(event.session_id, []).append(event)

        total = 0
        for session_events in events_by_session.values():
            session_identity = next(
                (e.initiated_by for e in session_events if e.type == "session_started"), None
            )
            if session_identity != identity:
                continue
            total += _count_session(session_events, cutoff=cutoff, now=now)
        return total


def _count_session(events: list[Event], *, cutoff: datetime, now: datetime) -> int:
    reversibility: dict[int, str] = {}
    verdict: dict[int, str] = {}
    at: dict[int, datetime] = {}
    plan_id_of: dict[int, str] = {}
    committed: set[int] = set()
    # Keyed by `plan_id`, not `step_seq`: a `pause`d call's retry re-plans
    # under a *new* `step_seq` once approved (spec §7 -- `ApprovalStage` is
    # itself bound to `plan_id` for the same reason, see its docstring), so
    # matching the approval back to the step that actually executed only
    # works through the `plan_id` both share.
    approved_plan_ids: set[str] = set()

    for event in events:
        step = event.step_seq
        if event.type == "approval_resolved":
            if event.payload.get("state") == "approved":
                plan_id = event.payload.get("plan_id")
                if isinstance(plan_id, str):
                    approved_plan_ids.add(plan_id)
            continue
        if step is None:
            continue
        if event.type == "plan_created":
            value = event.payload.get("reversibility")
            if isinstance(value, str):
                reversibility[step] = value
            plan_id = event.payload.get("plan_id")
            if isinstance(plan_id, str):
                plan_id_of[step] = plan_id
        elif event.type == "policy_evaluated":
            v = event.payload.get("verdict")
            if isinstance(v, str):
                verdict[step] = v
            at[step] = datetime.fromisoformat(event.at)
        elif event.type == "step_committed":
            committed.add(step)

    count = 0
    for step, reversible in reversibility.items():
        if reversible != "irreversible":
            continue
        step_verdict = verdict.get(step)
        if step_verdict == "allow":
            executed = step in committed
        elif step_verdict == "pause":
            plan_id = plan_id_of.get(step)
            executed = (
                plan_id is not None and plan_id in approved_plan_ids and step in committed
            )
        else:  # "deny" or unknown -- never counts
            executed = False
        if not executed:
            continue
        when = at.get(step)
        if when is None:
            continue
        if cutoff <= when <= now:
            count += 1
    return count
