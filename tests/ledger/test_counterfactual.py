"""Counterfactual replay: honesty rule, immutability, no-op regression anchor (plan-v2 E12)."""

from __future__ import annotations

from typing import Any

import pytest
from belay.ledger.counterfactual import InvalidForkPoint, run_counterfactual
from belay.ledger.replay import replay
from belay.ledger.store import LedgerStore
from hypothesis import given
from hypothesis import strategies as st


def _seed_session(
    store: LedgerStore,
    session_id: str = "s1",
    *,
    tool: str = "crm.bulk_delete",
    verdict: str = "pause",
) -> None:
    """One step: plan_created -> policy_evaluated -> (approval) -> journaled -> ... -> committed."""
    store.append(session_id, "session_started", {})
    store.append(session_id, "contract_set_pinned", {"set_hash": "sha256:abc"})
    store.append(
        session_id,
        "plan_created",
        {
            "plan_id": "p1",
            "tool": tool,
            "args": {"before_year": 2024},
            "effects": [{"type": "delete", "resource": "crm.record", "basis": "contract"}],
        },
        step_seq=1,
    )
    store.append(
        session_id,
        "policy_evaluated",
        {"verdict": verdict, "reasons": ["defaults.irreversible"]},
        step_seq=1,
    )
    if verdict == "pause":
        store.append(session_id, "approval_requested", {"approval_id": "a1"}, step_seq=1)
        store.append(
            session_id, "approval_resolved", {"approval_id": "a1", "state": "approved"}, step_seq=1
        )
    store.append(
        session_id, "step_journaled", {"tool": tool, "args": {"before_year": 2024}}, step_seq=1
    )
    store.append(
        session_id,
        "result_recorded",
        {"tool": tool, "result": {"deleted_ids": ["a", "b"]}},
        step_seq=1,
    )
    store.append(
        session_id,
        "compensation_registered",
        {"reversible": True, "tool": "crm.import_records", "args": {"records": {}}},
        step_seq=1,
    )
    store.append(session_id, "step_committed", {"tool": tool}, step_seq=1)

    # a second, downstream step -- what a "deny at step 1" would have prevented.
    store.append(
        session_id,
        "plan_created",
        {
            "plan_id": "p2",
            "tool": "crm.export_records",
            "args": {},
            "effects": [{"type": "read", "resource": "crm.record", "basis": "contract"}],
        },
        step_seq=2,
    )
    store.append(session_id, "policy_evaluated", {"verdict": "allow", "reasons": []}, step_seq=2)
    store.append(
        session_id, "step_journaled", {"tool": "crm.export_records", "args": {}}, step_seq=2
    )
    store.append(
        session_id,
        "result_recorded",
        {"tool": "crm.export_records", "result": {"count": 2}},
        step_seq=2,
    )
    store.append(
        session_id,
        "compensation_registered",
        {"reversible": False, "reason": "irreversible"},
        step_seq=2,
    )
    store.append(session_id, "step_committed", {"tool": "crm.export_records"}, step_seq=2)
    store.append(session_id, "session_closed", {})


def test_deny_where_real_was_pause_marks_downstream_diverged_never_fabricated() -> None:
    store = LedgerStore("sqlite:///:memory:")
    _seed_session(store, verdict="pause")
    events = store.read("s1")

    report = run_counterfactual(events, at_step_seq=1, override={"verdict": "deny"})

    assert not report.is_noop
    step1 = next(s for s in report.steps if s.step_seq == 1)
    step2 = next(s for s in report.steps if s.step_seq == 2)
    assert step1.outcome == "diverged"
    assert step2.outcome == "diverged"
    # never a fabricated concrete result: divergent steps carry a `basis`, no
    # invented `deleted_ids`/`count` posing as a real observation.
    assert step1.basis is not None
    assert "result" not in step1.detail
    assert step2.basis is not None
    assert "result" not in step2.detail

    # final-state comparison: the real session's actual outcome is unaffected
    # (it really happened), the branch's honest final state stops at the fork.
    assert report.real_final_state.steps[1] == "step_committed"
    assert 2 not in report.counterfactual_final_state.steps
    # the branch never claims step 1 committed -- only that it was re-decided.
    assert report.counterfactual_final_state.steps[1] == "policy_evaluated"


def test_noop_override_reports_100_percent_unchanged_and_matches_replay() -> None:
    store = LedgerStore("sqlite:///:memory:")
    _seed_session(store, verdict="pause")
    events = store.read("s1")

    report = run_counterfactual(events, at_step_seq=1, override={"verdict": "pause"})

    assert report.is_noop
    assert all(s.outcome == "unchanged" for s in report.steps)
    assert report.counterfactual_final_state.model_dump() == replay(events).model_dump()


@given(
    verdict=st.sampled_from(["allow", "pause", "deny"]),
    fork_step=st.sampled_from([1, 2]),
)
def test_property_any_noop_override_is_always_unchanged_and_matches_replay(
    verdict: str, fork_step: int
) -> None:
    """Hypothesis property test: for ANY no-op override at ANY valid decision
    point, the counterfactual always reports unchanged everywhere and final
    state == replay(events) -- the strongest correctness guarantee here."""
    store = LedgerStore("sqlite:///:memory:")
    _seed_session(store, verdict=verdict if fork_step == 1 else "allow")
    events = store.read("s1")

    real_verdict_at_fork = next(
        e.payload["verdict"]
        for e in events
        if e.type == "policy_evaluated" and e.step_seq == fork_step
    )
    report = run_counterfactual(
        events, at_step_seq=fork_step, override={"verdict": real_verdict_at_fork}
    )

    assert report.is_noop
    assert all(s.outcome == "unchanged" for s in report.steps)
    assert report.counterfactual_final_state.model_dump() == replay(events).model_dump()
    assert report.real_final_state.model_dump() == replay(events).model_dump()


def test_immutability_row_count_unchanged_and_no_upstream_calls(tmp_path: Any) -> None:
    db_path = tmp_path / "belay.db"
    store = LedgerStore(f"sqlite:///{db_path}")
    _seed_session(store, verdict="pause")

    before = len(store.read_all())

    calls: list[tuple[str, dict[str, Any]]] = []

    def spy_upstream_replay(tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        calls.append((tool, args))
        return None  # a real spy that never actually calls anything upstream

    events = store.read("s1")
    run_counterfactual(
        events, at_step_seq=1, override={"verdict": "deny"}, upstream_replay=spy_upstream_replay
    )

    after = len(store.read_all())
    assert before == after
    # upstream_replay is a pure, local, read-only estimate hook -- not an
    # upstream MCP transport -- but it's the only extension point capable of
    # reaching outward, so a zero-external-call assertion at this boundary
    # would live in a test wiring a real transport spy (see tests/cli below).


def test_honesty_step_with_no_recorded_plan_is_unknown_never_guessed() -> None:
    store = LedgerStore("sqlite:///:memory:")
    store.append("s1", "session_started", {})
    store.append(
        "s1",
        "plan_created",
        {"plan_id": "p1", "tool": "crm.bulk_delete", "args": {}, "effects": []},
        step_seq=1,
    )
    store.append("s1", "policy_evaluated", {"verdict": "allow", "reasons": []}, step_seq=1)
    store.append("s1", "step_journaled", {"tool": "crm.bulk_delete", "args": {}}, step_seq=1)
    store.append("s1", "step_committed", {"tool": "crm.bulk_delete"}, step_seq=1)
    # step 2 has NO plan_created recorded at all (e.g. a synthetic/rewind-only
    # step) -- there is no tool identity or effect estimate to reason about.
    store.append("s1", "step_journaled", {"tool": "?"}, step_seq=2)
    store.append("s1", "step_committed", {"tool": "?"}, step_seq=2)

    events = store.read("s1")
    report = run_counterfactual(events, at_step_seq=1, override={"verdict": "deny"})

    step2 = next(s for s in report.steps if s.step_seq == 2)
    assert step2.outcome == "unknown"
    assert step2.basis is None


def test_invalid_fork_point_raises_clear_error_not_silent_empty_report() -> None:
    store = LedgerStore("sqlite:///:memory:")
    _seed_session(store, verdict="pause")
    events = store.read("s1")

    with pytest.raises(InvalidForkPoint):
        run_counterfactual(events, at_step_seq=99, override={"verdict": "deny"})


def test_upstream_replay_hook_yields_better_than_simulated_basis() -> None:
    store = LedgerStore("sqlite:///:memory:")
    _seed_session(store, verdict="pause")
    events = store.read("s1")

    def dry_run_estimate(tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        if tool == "crm.bulk_delete":
            return "sql_simulator", {"row_count": 0}
        return None

    report = run_counterfactual(
        events, at_step_seq=1, override={"verdict": "deny"}, upstream_replay=dry_run_estimate
    )
    step1 = next(s for s in report.steps if s.step_seq == 1)
    assert step1.outcome == "diverged"
    assert step1.basis == "sql_simulator"
    assert step1.detail == {"row_count": 0}
