"""Tests for `belay.policy.baseline`: Welford streaming stats + per-session BaselineStore."""

from __future__ import annotations

import statistics

import pytest
from belay.ledger.store import LedgerStore
from belay.policy.baseline import BaselineStore, Welford
from hypothesis import given
from hypothesis import strategies as st


@given(
    st.lists(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False), min_size=1, max_size=200)
)
def test_welford_matches_naive_mean_and_stddev(values: list[float]) -> None:
    """Welford's streaming update must agree with naive `statistics.mean`/`pstdev`."""
    w = Welford()
    for v in values:
        w.update(v)
    assert w.n == len(values)
    assert w.mean == pytest.approx(statistics.mean(values))
    assert w.stddev == pytest.approx(statistics.pstdev(values), abs=1e-6)


def test_welford_empty_series_has_zero_mean_and_stddev() -> None:
    w = Welford()
    assert w.n == 0
    assert w.mean == 0.0
    assert w.stddev == 0.0


def _seed_plan_created(
    ledger: LedgerStore, session_id: str, tool: str, effect_type: str, count: str, plan_id: str
) -> None:
    ledger.append(
        session_id,
        "plan_created",
        {
            "plan_id": plan_id,
            "tool": tool,
            "effects": [{"type": effect_type, "resource": f"{tool}.record", "count": count}],
        },
    )


def test_baseline_store_reads_only_matching_tool_and_effect_type() -> None:
    ledger = LedgerStore()
    _seed_plan_created(ledger, "s1", "crm.delete", "delete", "10", "p1")
    _seed_plan_created(ledger, "s1", "crm.delete", "delete", "12", "p2")
    _seed_plan_created(ledger, "s1", "crm.create", "create", "1", "p3")  # different tool
    _seed_plan_created(ledger, "s1", "crm.delete", "read", "5", "p4")  # different effect type

    stats = BaselineStore(ledger).stats("s1", "crm.delete", "delete")
    assert stats.n == 2
    assert stats.mean == pytest.approx(11.0)


def test_baseline_store_excludes_the_plan_being_evaluated() -> None:
    ledger = LedgerStore()
    _seed_plan_created(ledger, "s1", "crm.delete", "delete", "10", "p1")
    _seed_plan_created(ledger, "s1", "crm.delete", "delete", "500", "current")

    stats = BaselineStore(ledger).stats("s1", "crm.delete", "delete", exclude_plan_id="current")
    assert stats.n == 1
    assert stats.mean == pytest.approx(10.0)


def test_baseline_store_never_crosses_sessions() -> None:
    ledger = LedgerStore()
    _seed_plan_created(ledger, "s1", "crm.delete", "delete", "10", "p1")
    _seed_plan_created(ledger, "s2", "crm.delete", "delete", "999", "p2")

    stats = BaselineStore(ledger).stats("s1", "crm.delete", "delete")
    assert stats.n == 1
    assert stats.mean == pytest.approx(10.0)
