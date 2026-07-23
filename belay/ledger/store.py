"""LedgerStore: append, read, hash chain (spec §9.1, §9.2).

@spec("9.1")
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as DBSession

from belay.canonical import canonical_bytes, sha256_hex
from belay.db.models import Base, EventRow
from belay.ledger.model import GENESIS_HASH, Event


def _row_to_event(row: EventRow) -> Event:
    return Event(
        event_id=row.event_id,
        session_id=row.session_id,
        step_seq=row.step_seq,
        type=row.type,
        at=row.at,
        payload=row.payload,
        set_hash=row.set_hash,
        prev_hash=row.prev_hash,
        hash=row.hash,
        initiated_by=row.initiated_by,
        on_behalf_of=row.on_behalf_of,
    )


def compute_hash(event: Event) -> str:
    """`hash = SHA-256(canonical(event without hash) || prev_hash)` (spec §9.2)."""
    data = canonical_bytes(event.unsigned_dict()) + event.prev_hash.encode("utf-8")
    return sha256_hex(data)


class LedgerStore:
    """Append-only, hash-chained event ledger backed by SQLite (spec §9.1).

    There is deliberately no update/delete method: `append` is the only
    write path exposed. Past events are immutable (spec §9.2/§9.3).
    """

    def __init__(self, db_url: str = "sqlite:///:memory:", *, engine: Engine | None = None) -> None:
        self._engine = engine if engine is not None else create_engine(db_url, future=True)
        Base.metadata.create_all(self._engine)

    @property
    def engine(self) -> Engine:
        """The underlying SQLAlchemy engine, so other stores (e.g. `ApprovalQueue`)
        can share the same SQLite file without re-parsing a `db_url`."""
        return self._engine

    def append(
        self,
        session_id: str,
        type: str,
        payload: dict[str, Any],
        step_seq: int | None = None,
        set_hash: str | None = None,
        initiated_by: str | None = None,
        on_behalf_of: str | None = None,
    ) -> Event:
        """Append one event, computing `prev_hash`/`hash` (spec §9.1, §9.2).

        `initiated_by`/`on_behalf_of` (E14, plan-v2) are normally only passed
        by `Lifecycle.start_session()` for the `session_started` event -- see
        `belay/ledger/model.py`'s `Event` docstring for why this isn't
        repeated on every event.
        """
        with DBSession(self._engine) as db:
            last = db.scalars(
                select(EventRow)
                .where(EventRow.session_id == session_id)
                .order_by(EventRow.id.desc())
                .limit(1)
            ).first()
            prev_hash = last.hash if last is not None else GENESIS_HASH

            event = Event(
                event_id=str(uuid.uuid4()),
                session_id=session_id,
                step_seq=step_seq,
                type=type,
                at=datetime.now(UTC).isoformat(),
                payload=payload,
                set_hash=set_hash,
                prev_hash=prev_hash,
                hash="",
                initiated_by=initiated_by,
                on_behalf_of=on_behalf_of,
            )
            event.hash = compute_hash(event)

            row = EventRow(
                event_id=event.event_id,
                session_id=event.session_id,
                step_seq=event.step_seq,
                type=event.type,
                at=event.at,
                payload=event.payload,
                set_hash=event.set_hash,
                prev_hash=event.prev_hash,
                hash=event.hash,
                initiated_by=event.initiated_by,
                on_behalf_of=event.on_behalf_of,
            )
            db.add(row)
            db.commit()
            return event

    def read(self, session_id: str) -> list[Event]:
        """Read every event of one session, in append order."""
        with DBSession(self._engine) as db:
            rows = db.scalars(
                select(EventRow).where(EventRow.session_id == session_id).order_by(EventRow.id)
            ).all()
            return [_row_to_event(row) for row in rows]

    def read_all(self) -> list[Event]:
        """Read every event in the store, across all sessions, in append order."""
        with DBSession(self._engine) as db:
            rows = db.scalars(select(EventRow).order_by(EventRow.id)).all()
            return [_row_to_event(row) for row in rows]
