"""Injectable clock abstraction.

Plan expiration (spec §5.4) and quiet hours (spec §6.1) both depend on
"now". Neither `Planner` nor `PolicyEngine` reads wall-clock time directly —
both take a `Clock`, so tests can control "now" deterministically instead of
sleeping or racing the real clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """The real wall clock (UTC). Default in production."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class FixedClock:
    """A clock that always returns the same instant, moved explicitly by tests."""

    _now: datetime

    def now(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> None:
        self._now = at
