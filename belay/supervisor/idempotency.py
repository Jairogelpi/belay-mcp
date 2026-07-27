"""Durable idempotency store for hook events (spec ARCH-006): duplicate
event IDs MUST be idempotent -- across a supervisor restart, not merely for
one process's in-memory lifetime (an earlier version cached this in a plain
dict; a P0 review correctly pointed out that a restart between two retries
of the same event could re-decide it against changed approval state and
flip `deny` to `allow`).

Keyed by installation_id+host+host_session_id+event_id+phase. A request
with the SAME key but a DIFFERENT content digest is a collision -- the same
identifier reused for a logically different event -- and is never answered
from either cached version; the caller denies it outright.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from belay.db.models import Base, HookEventRow
from belay.supervisor.protocol import HookEvent


def event_key(
    installation_id: str, host: str, host_session_id: str, event_id: str, phase: str
) -> str:
    return f"{installation_id}|{host}|{host_session_id}|{event_id}|{phase}"


def content_digest(event: HookEvent) -> str:
    """Hashes only the fields that make two occurrences of "the same event"
    actually the same request -- deliberately excludes `monotonic_ns`/
    `wall_clock` (those always differ between the original call and a
    retry moments later, even for a genuinely identical logical event) and
    `trust_tier`/`adapter_version`/`schema_version`/`os_user` (properties of
    the *environment* evaluating the event, not the event's own content)."""
    material = {
        "tool_name": event.tool_name,
        "normalized_identity": event.normalized_identity,
        "surface": event.surface,
        "args": event.args,
        "cwd": event.cwd,
        "repo_identity": event.repo_identity,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachedEvent:
    request_digest: str
    response: dict[str, Any]


class IdempotencyStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        Base.metadata.create_all(self._engine)

    def get(self, key: str) -> CachedEvent | None:
        with DBSession(self._engine) as db:
            row = db.get(HookEventRow, key)
            if row is None:
                return None
            return CachedEvent(request_digest=row.request_digest, response=row.response)

    def record_if_absent(
        self, key: str, request_digest: str, response: dict[str, Any]
    ) -> CachedEvent:
        """Insert-only: if another (concurrent) call already recorded this
        key first, THIS call's own response is discarded and the winner's
        is returned instead -- true single-decision-per-event even when two
        requests for the identical event race each other, not just when
        they arrive one at a time."""
        with DBSession(self._engine) as db:
            row = HookEventRow(
                event_key=key,
                request_digest=request_digest,
                response=response,
                decided_at=datetime.now(UTC).isoformat(),
            )
            db.add(row)
            try:
                db.commit()
                return CachedEvent(request_digest=request_digest, response=response)
            except IntegrityError:
                db.rollback()

        existing = self.get(key)
        assert existing is not None  # the IntegrityError means a row is now there
        return existing

    def all_keys(self) -> list[str]:
        """For tests/diagnostics only."""
        with DBSession(self._engine) as db:
            return list(db.scalars(select(HookEventRow.event_key)).all())
