"""Chain and coherence verification (spec §9.2)."""

from __future__ import annotations

from belay.ledger.store import LedgerStore
from belay.ledger.verify import verify_chain, verify_coherence


def test_verify_chain_ok_for_untampered_events() -> None:
    store = LedgerStore("sqlite:///:memory:")
    for i in range(5):
        store.append("s1", "step_journaled", {"i": i}, step_seq=i)

    report = verify_chain(store.read("s1"))
    assert report.ok
    assert report.failed_index is None


def test_verify_chain_flags_corrupted_event_at_exact_index() -> None:
    """@conformance §9.2 — corrupting event k's payload MUST be reported at k."""
    store = LedgerStore("sqlite:///:memory:")
    n = 8
    for i in range(n):
        store.append("s1", "step_journaled", {"i": i}, step_seq=i)

    events = store.read("s1")
    corrupted_index = 3
    events[corrupted_index].payload["i"] = "tampered"

    report = verify_chain(events)

    assert not report.ok
    assert report.failed_index == corrupted_index
    assert report.failed_event_id == events[corrupted_index].event_id


def test_verify_chain_flags_corrupted_hash_field_directly() -> None:
    """@conformance §9.2 — flipping the stored `hash` itself is also detected."""
    store = LedgerStore("sqlite:///:memory:")
    for i in range(4):
        store.append("s1", "step_journaled", {"i": i}, step_seq=i)

    events = store.read("s1")
    events[2].hash = "0" * 64

    report = verify_chain(events)
    assert not report.ok
    assert report.failed_index == 2


def test_verify_chain_is_partitioned_per_session() -> None:
    store = LedgerStore("sqlite:///:memory:")
    store.append("s1", "session_started", {})
    store.append("s2", "session_started", {})

    report = verify_chain(store.read_all())
    assert report.ok


def test_verify_coherence_ok_for_complete_step() -> None:
    store = LedgerStore("sqlite:///:memory:")
    store.append("s1", "step_journaled", {}, step_seq=1)
    store.append("s1", "result_recorded", {}, step_seq=1)
    store.append("s1", "compensation_registered", {}, step_seq=1)
    store.append("s1", "step_committed", {}, step_seq=1)

    report = verify_coherence(store.read("s1"))
    assert report.ok


def test_verify_coherence_flags_committed_without_journal() -> None:
    store = LedgerStore("sqlite:///:memory:")
    store.append("s1", "result_recorded", {}, step_seq=1)
    store.append("s1", "compensation_registered", {}, step_seq=1)
    store.append("s1", "step_committed", {}, step_seq=1)

    report = verify_coherence(store.read("s1"))
    assert not report.ok
    assert any("step_journaled" in e for e in report.errors)


def test_verify_coherence_flags_orphan_compensation_executed() -> None:
    store = LedgerStore("sqlite:///:memory:")
    store.append("s1", "compensation_executed", {}, step_seq=1)

    report = verify_coherence(store.read("s1"))
    assert not report.ok
    assert any("compensation_executed" in e for e in report.errors)
