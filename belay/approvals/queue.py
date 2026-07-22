"""Approval queue: states, transitions, expiration (spec §7).

State machine (spec §7.1, verbatim): `pending -> approved | rejected | expired`.
Transitions are one-way — there is no path back to `pending` and no path
between `approved`/`rejected`/`expired`. Expiration is a *default expiry of
30 minutes* is checked lazily (spec §7.1: "an expired item MUST NOT be
executable"), and at the exact instant an item both qualifies as expired and
is approved/rejected, expiration wins (§12 TOCTOU discipline: the safer
outcome wins ties, same spirit as unknown-effects-as-worst-case in §6.3).

Persisted in SQLite (the `approvals` table, plan.md §1) rather than kept
in-memory, because the CLI (`belay approvals list/approve/reject`) runs as a
process separate from `belay run` — the queue must outlive the proxy
process for the demo flow (plan.md §10) to work at all.

No-self-approval (spec §12) is enforced architecturally, not just by test:
this module exposes no MCP tool, is never imported by `belay/proxy/server.py`
except to be *invoked by the operator's CLI process*, and `ApprovalStage`
(in `belay/proxy/lifecycle.py`) only ever *reads* queue state on the agent's
behalf -- it has no `approve`/`reject` call sites at all. The agent-facing
proxy has no code path that can call `ApprovalQueue.approve`/`.reject`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as DBSession

from belay.clock import Clock, SystemClock
from belay.db.models import ApprovalRow, Base
from belay.errors import BelayError

ApprovalState = Literal["pending", "approved", "rejected", "expired"]

#: Default expiry (spec §7.1): 30 minutes from `requested_at`.
DEFAULT_EXPIRY = timedelta(minutes=30)

#: The legal state graph (spec §7.1). Transitions are unidirectional: once an
#: item leaves `pending` there is no way back into it or across to a sibling
#: terminal state.
_LEGAL_TRANSITIONS: dict[ApprovalState, frozenset[ApprovalState]] = {
    "pending": frozenset({"approved", "rejected", "expired"}),
    "approved": frozenset(),
    "rejected": frozenset(),
    "expired": frozenset(),
}


@dataclass(frozen=True)
class ApprovalItem:
    """One approval item (spec §7.1). Bound to the `plan_id` it was requested
    for -- re-planning the same logical call produces a new `plan_id`, and an
    item for the old one is never looked up again (§12 approver binding)."""

    approval_id: str
    session_id: str
    plan_id: str
    plan: dict[str, Any]
    requested_at: datetime
    expires_at: datetime
    state: ApprovalState
    step_seq: int | None = None
    approved_by: str | None = None
    rejected_by: str | None = None
    reason: str | None = None


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _row_to_item(row: ApprovalRow) -> ApprovalItem:
    return ApprovalItem(
        approval_id=row.approval_id,
        session_id=row.session_id,
        plan_id=row.plan_id,
        plan=row.plan,
        requested_at=_parse(row.requested_at),
        expires_at=_parse(row.expires_at),
        state=row.state,  # type: ignore[arg-type]
        step_seq=row.step_seq,
        approved_by=row.approved_by,
        rejected_by=row.rejected_by,
        reason=row.reason,
    )


class ApprovalQueue:
    """The approval queue (spec §7). Reachable only from operator-facing code
    (the CLI's `belay approvals` subcommands) -- never registered as an MCP
    tool exposed to the protected agent (spec §12)."""

    def __init__(
        self,
        db_url: str = "sqlite:///:memory:",
        *,
        engine: Engine | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._engine = engine if engine is not None else create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)
        self._clock = clock or SystemClock()

    def request(
        self,
        session_id: str,
        plan_id: str,
        plan: dict[str, Any],
        *,
        step_seq: int | None = None,
        expiry: timedelta = DEFAULT_EXPIRY,
    ) -> ApprovalItem:
        """Park one action as a new `pending` approval item (spec §7.1)."""
        now = self._clock.now()
        approval_id = f"ap_{uuid.uuid4().hex[:10]}"
        row = ApprovalRow(
            approval_id=approval_id,
            session_id=session_id,
            plan_id=plan_id,
            step_seq=step_seq,
            plan=plan,
            requested_at=now.isoformat(),
            expires_at=(now + expiry).isoformat(),
            state="pending",
        )
        with DBSession(self._engine) as db:
            db.add(row)
            db.commit()
            item = _row_to_item(row)
        return self._settle_expiry(item)

    def get(self, approval_id: str) -> ApprovalItem | None:
        with DBSession(self._engine) as db:
            row = db.get(ApprovalRow, approval_id)
            if row is None:
                return None
            item = _row_to_item(row)
        return self._settle_expiry(item)

    def for_plan(self, plan_id: str) -> ApprovalItem | None:
        """The (at most one) item currently bound to this `plan_id` (spec §12)."""
        with DBSession(self._engine) as db:
            row = db.scalars(
                select(ApprovalRow)
                .where(ApprovalRow.plan_id == plan_id)
                .order_by(ApprovalRow.requested_at.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            item = _row_to_item(row)
        return self._settle_expiry(item)

    def list(self) -> list[ApprovalItem]:
        with DBSession(self._engine) as db:
            rows = db.scalars(select(ApprovalRow).order_by(ApprovalRow.requested_at)).all()
            items = [_row_to_item(row) for row in rows]
        return [self._settle_expiry(item) for item in items]

    def approve(
        self, approval_id: str, approved_by: str, reason: str | None = None
    ) -> ApprovalItem:
        """Transition `pending -> approved` (spec §7.1, §12: `approved_by` MUST be recorded)."""
        return self._resolve(approval_id, "approved", by=approved_by, reason=reason)

    def reject(self, approval_id: str, rejected_by: str, reason: str | None = None) -> ApprovalItem:
        """Transition `pending -> rejected` (spec §7.1)."""
        return self._resolve(approval_id, "rejected", by=rejected_by, reason=reason)

    def _resolve(
        self, approval_id: str, to: ApprovalState, *, by: str, reason: str | None
    ) -> ApprovalItem:
        with DBSession(self._engine) as db:
            row = db.get(ApprovalRow, approval_id)
            if row is None:
                raise BelayError(
                    "approval_expired", {"approval_id": approval_id, "reason": "not found"}
                )

            item = self._settle_expiry(_row_to_item(row), persist_with=db, row=row)

            # Expiration wins the tie (spec §7.1/§12 TOCTOU): re-check "now"
            # against `expires_at` before honoring any resolution, even if the
            # item was still `pending` a moment ago.
            if item.state == "expired" or self._clock.now() >= item.expires_at:
                if row.state == "pending":
                    row.state = "expired"
                    db.commit()
                raise BelayError("approval_expired", {"approval_id": approval_id})

            if to not in _LEGAL_TRANSITIONS[item.state]:
                raise ValueError(
                    f"illegal approval transition for {approval_id}: {item.state} -> {to}"
                )

            row.state = to
            if to == "approved":
                row.approved_by = by
            else:
                row.rejected_by = by
            row.reason = reason
            db.commit()
            return _row_to_item(row)

    def _settle_expiry(
        self,
        item: ApprovalItem,
        *,
        persist_with: DBSession | None = None,
        row: ApprovalRow | None = None,
    ) -> ApprovalItem:
        """Lazily flip `pending` -> `expired` once `now >= expires_at` (spec §7.1)."""
        if item.state != "pending" or self._clock.now() < item.expires_at:
            return item
        if persist_with is not None and row is not None:
            row.state = "expired"
            persist_with.commit()
            return _row_to_item(row)
        with DBSession(self._engine) as db:
            fresh = db.get(ApprovalRow, item.approval_id)
            if fresh is not None and fresh.state == "pending":
                fresh.state = "expired"
                db.commit()
                return _row_to_item(fresh)
        return item
