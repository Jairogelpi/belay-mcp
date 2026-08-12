"""Ownership-aware SQLAlchemy engine lifecycle tests (plan E21)."""

from __future__ import annotations

import gc
import weakref
from collections.abc import Callable
from typing import Any

import pytest
from belay.approvals.queue import ApprovalQueue
from belay.db.lifecycle import EngineLease
from belay.executor.idempotency import IdempotencyStore
from belay.ledger.store import LedgerStore
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

StoreFactory = Callable[..., Any]
STORE_FACTORIES: tuple[StoreFactory, ...] = (ApprovalQueue, IdempotencyStore, LedgerStore)


def _track_disposals(engine: Engine) -> list[Engine]:
    disposed: list[Engine] = []
    event.listen(engine, "engine_disposed", disposed.append)
    return disposed


def test_owned_engine_lease_disposes_its_engine_exactly_once() -> None:
    lease = EngineLease.create("sqlite:///:memory:")
    disposed = _track_disposals(lease.engine)

    lease.close()
    lease.close()

    assert disposed == [lease.engine]


def test_borrowed_engine_lease_does_not_dispose_its_engine() -> None:
    engine = create_engine("sqlite:///:memory:")
    try:
        disposed = _track_disposals(engine)
        lease = EngineLease.borrow(engine)

        lease.close()
        lease.close()

        assert disposed == []
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()


def test_owned_engine_lease_context_manager_disposes_its_engine() -> None:
    lease = EngineLease.create("sqlite:///:memory:")
    disposed = _track_disposals(lease.engine)

    with lease as entered:
        assert entered is lease

    assert disposed == [lease.engine]


def test_unclosed_owned_engine_lease_is_disposed_by_finalizer() -> None:
    lease = EngineLease.create("sqlite:///:memory:")
    engine = lease.engine
    disposed = _track_disposals(engine)
    lease_ref = weakref.ref(lease)

    del lease
    gc.collect()

    assert lease_ref() is None
    assert disposed == [engine]


@pytest.mark.parametrize("store_factory", STORE_FACTORIES)
def test_store_disposes_internally_created_engine(store_factory: StoreFactory) -> None:
    store = store_factory()
    disposed = _track_disposals(store._engine)

    store.close()
    store.close()

    assert disposed == [store._engine]


@pytest.mark.parametrize("store_factory", STORE_FACTORIES)
def test_store_does_not_dispose_supplied_engine(store_factory: StoreFactory) -> None:
    engine = create_engine("sqlite:///:memory:")
    try:
        disposed = _track_disposals(engine)
        store = store_factory(engine=engine)

        store.close()

        assert disposed == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("store_factory", STORE_FACTORIES)
def test_store_context_manager_returns_store_and_disposes_owned_engine(
    store_factory: StoreFactory,
) -> None:
    store = store_factory()
    disposed = _track_disposals(store._engine)

    with store as entered:
        assert entered is store

    assert disposed == [store._engine]


@pytest.mark.parametrize("store_factory", STORE_FACTORIES)
def test_unclosed_owned_store_is_disposed_by_finalizer(store_factory: StoreFactory) -> None:
    store = store_factory()
    engine = store._engine
    disposed = _track_disposals(engine)
    store_ref = weakref.ref(store)

    del store
    gc.collect()

    assert store_ref() is None
    assert disposed == [engine]
