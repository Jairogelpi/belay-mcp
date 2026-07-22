"""Idempotency-key deduplication (spec §4.5, §8.1).

A step whose contract declares `idempotency_key` gets a row here the moment
`calling` starts (`status="calling"`) and the row is completed with the
upstream result once `result_recorded` happens (`status="done"`). This is
exactly the window spec §8.1 describes as crash-prone ("a crash between 3
and 4"), and it's what `belay/executor/recovery.py` reconciles against on
restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as DBSession

from belay.db.models import Base, IdempotencyRow


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    session_id: str
    step_seq: int
    status: str  # "calling" | "done"
    result: dict[str, Any] | None


def _row_to_record(row: IdempotencyRow) -> IdempotencyRecord:
    return IdempotencyRecord(
        key=row.key,
        session_id=row.session_id,
        step_seq=row.step_seq,
        status=row.status,
        result=row.result,
    )


class IdempotencyStore:
    """Tracks in-flight and completed calls by their declared `idempotency_key`."""

    def __init__(self, db_url: str = "sqlite:///:memory:", *, engine: Engine | None = None) -> None:
        self._engine = engine if engine is not None else create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)

    def get(self, key: str) -> IdempotencyRecord | None:
        with DBSession(self._engine) as db:
            row = db.get(IdempotencyRow, key)
            return _row_to_record(row) if row is not None else None

    def begin(self, key: str, session_id: str, step_seq: int) -> IdempotencyRecord:
        """Record that a call for `key` is starting.

        If `key` already exists (retried call, or a repeated idempotency
        key), returns the existing record unchanged instead of clobbering
        it -- the caller uses `.status`/`.result` to decide whether to call
        the upstream at all.
        """
        with DBSession(self._engine) as db:
            existing = db.get(IdempotencyRow, key)
            if existing is not None:
                return _row_to_record(existing)
            row = IdempotencyRow(
                key=key, session_id=session_id, step_seq=step_seq, status="calling", result=None
            )
            db.add(row)
            db.commit()
            return _row_to_record(row)

    def complete(self, key: str, result: dict[str, Any]) -> None:
        with DBSession(self._engine) as db:
            row = db.get(IdempotencyRow, key)
            if row is None:  # pragma: no cover - defensive, begin() always precedes complete()
                return
            row.status = "done"
            row.result = result
            db.commit()
