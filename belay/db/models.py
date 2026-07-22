"""SQLAlchemy ORM models for Belay's SQLite store (spec §9.1, §7).

`events` landed in E2 — it's all `LedgerStore` needs. `approvals` lands in
E5 alongside `belay/approvals/queue.py` (spec §7): it must persist across
process invocations, since the CLI (`belay approvals list/approve/reject`)
runs as a separate process from `belay run`. `sessions`, `contract_sets`
remain for whichever later entrega actually reads/writes them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class EventRow(Base):
    """One row per ledger event (spec §9.1 envelope)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    session_id: Mapped[str] = mapped_column(String(255), index=True)
    step_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type: Mapped[str] = mapped_column(String(64))
    at: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    set_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class ApprovalRow(Base):
    """One row per approval item (spec §7.1). Persisted so the CLI (a separate
    process from `belay run`) can list/approve/reject items."""

    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(255), index=True)
    plan_id: Mapped[str] = mapped_column(String(255), index=True)
    step_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON)
    requested_at: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class IdempotencyRow(Base):
    """One row per idempotency key seen by the saga executor (spec §8.1).

    `status` is `"calling"` from the moment the upstream call is issued
    until its result is durably recorded (`"done"`) — the window a crash
    between `calling` and `result_recorded` can land in (spec §8.1
    paragraph 2), which is exactly what `belay/executor/recovery.py`
    reconciles on restart.
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(255), index=True)
    step_seq: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
