"""SQLAlchemy ORM models for Belay's SQLite store (spec §9.1).

Only `events` lands in E2 — it's all `LedgerStore` needs. `sessions`,
`approvals`, `contract_sets` land with the entregas that actually read/write
them (E5+); adding them now would be schema for later, which can add
itself when it's needed.
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
