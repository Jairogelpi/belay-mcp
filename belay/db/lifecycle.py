"""Ownership-aware lifecycle for SQLAlchemy engines."""

from __future__ import annotations

import weakref
from types import TracebackType

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


class EngineLease:
    """Own or borrow an engine and close only engines created by the lease."""

    def __init__(self, engine: Engine, *, owned: bool) -> None:
        self._engine = engine
        self._finalizer = weakref.finalize(self, engine.dispose) if owned else None

    @classmethod
    def create(cls, db_url: str) -> EngineLease:
        return cls(create_engine(db_url, future=True), owned=True)

    @classmethod
    def borrow(cls, engine: Engine) -> EngineLease:
        return cls(engine, owned=False)

    @property
    def engine(self) -> Engine:
        return self._engine

    def close(self) -> None:
        if self._finalizer is not None:
            self._finalizer()

    def __enter__(self) -> EngineLease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
