"""Forward-compatible unknown fields are preserved on re-read (spec §14)."""

from __future__ import annotations

from belay.ledger.model import Event
from belay.ledger.store import LedgerStore


def test_unknown_payload_fields_survive_a_write_read_roundtrip() -> None:
    """@spec("14.1") — unknown fields MUST be preserved in the ledger (evidence is tolerant)."""
    store = LedgerStore("sqlite:///:memory:")
    store.append("s1", "step_journaled", {"known": 1, "from_the_future": {"nested": True}})

    got = store.read("s1")[0]
    assert got.payload["from_the_future"] == {"nested": True}


def test_event_model_tolerates_unknown_envelope_fields() -> None:
    """§14: 'Unknown fields MUST be preserved (ledger) ... evidence is tolerant.'"""
    event = Event(
        event_id="e1",
        session_id="s1",
        type="step_journaled",
        at="2026-07-22T00:00:00Z",
        prev_hash="0" * 64,
        hash="a" * 64,
        belay_ledger_version="0.2",  # a hypothetical future envelope field
    )
    assert event.model_dump()["belay_ledger_version"] == "0.2"
