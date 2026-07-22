"""LedgerStore.append/read and hash-chain basics (spec §9.1, §9.2)."""

from __future__ import annotations

import pytest
from belay.ledger.model import GENESIS_HASH
from belay.ledger.store import LedgerStore


def make_store() -> LedgerStore:
    return LedgerStore("sqlite:///:memory:")


def test_append_computes_prev_hash_chain() -> None:
    store = make_store()
    e1 = store.append("s1", "session_started", {"a": 1})
    e2 = store.append("s1", "step_journaled", {"b": 2}, step_seq=1)

    assert e1.prev_hash == GENESIS_HASH
    assert e2.prev_hash == e1.hash
    assert e1.hash != e2.hash


def test_read_returns_events_in_append_order() -> None:
    store = make_store()
    store.append("s1", "session_started", {})
    store.append("s1", "step_journaled", {}, step_seq=1)
    store.append("s1", "step_committed", {}, step_seq=1)

    events = store.read("s1")
    assert [e.type for e in events] == ["session_started", "step_journaled", "step_committed"]


def test_read_partitions_by_session() -> None:
    store = make_store()
    store.append("s1", "session_started", {})
    store.append("s2", "session_started", {})

    assert len(store.read("s1")) == 1
    assert len(store.read("s2")) == 1
    # Each session's chain starts fresh from genesis.
    assert store.read("s1")[0].prev_hash == GENESIS_HASH
    assert store.read("s2")[0].prev_hash == GENESIS_HASH


def test_read_all_spans_every_session() -> None:
    store = make_store()
    store.append("s1", "session_started", {})
    store.append("s2", "session_started", {})

    assert len(store.read_all()) == 2


def test_store_exposes_no_update_or_delete_api() -> None:
    """An already-written event is immutable — no mutation surface exists (spec §9.2)."""
    store = make_store()
    forbidden_names = ("update", "edit", "modify", "delete", "remove", "patch", "set")
    public_methods = {name for name in dir(store) if not name.startswith("_")}
    for forbidden in forbidden_names:
        assert forbidden not in public_methods


@pytest.mark.parametrize("field", ["payload", "type", "step_seq"])
def test_append_persists_fields_roundtrip(field: str) -> None:
    store = make_store()
    store.append("s1", "step_journaled", {"tool": "fs.write"}, step_seq=3)
    got = store.read("s1")[0]
    assert got.step_seq == 3
    assert got.type == "step_journaled"
    assert got.payload == {"tool": "fs.write"}
