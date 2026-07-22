"""replay() determinism and purity (spec §9.4)."""

from __future__ import annotations

from belay.ledger.model import EVENT_TYPES
from belay.ledger.replay import replay
from belay.ledger.store import LedgerStore
from hypothesis import given
from hypothesis import strategies as st


def test_replay_reconstructs_basic_session_state() -> None:
    store = LedgerStore("sqlite:///:memory:")
    store.append("s1", "session_started", {})
    store.append("s1", "contract_set_pinned", {"set_hash": "sha256:abc"})
    store.append("s1", "step_journaled", {}, step_seq=1)
    store.append(
        "s1", "compensation_registered", {"tool": "fs.delete", "args": {"id": 1}}, step_seq=1
    )
    store.append("s1", "step_committed", {}, step_seq=1)
    store.append("s1", "session_closed", {})

    state = replay(store.read("s1"))

    assert state.session_id == "s1"
    assert state.set_hash == "sha256:abc"
    assert state.status == "closed"
    assert state.compensations[1]["tool"] == "fs.delete"
    assert state.steps[1] == "step_committed"


@given(
    types=st.lists(st.sampled_from(EVENT_TYPES), min_size=0, max_size=15),
    step_seqs=st.lists(st.integers(min_value=1, max_value=5) | st.none(), min_size=0, max_size=15),
)
def test_replay_is_deterministic_and_pure(types: list[str], step_seqs: list[int | None]) -> None:
    """@conformance §9.4 — replay(events) run twice yields identical states."""
    store = LedgerStore("sqlite:///:memory:")
    n = min(len(types), len(step_seqs))
    for i in range(n):
        store.append("s1", types[i], {"i": i}, step_seq=step_seqs[i])

    events = store.read("s1")

    state_a = replay(events)
    state_b = replay(events)

    assert state_a.model_dump() == state_b.model_dump()
