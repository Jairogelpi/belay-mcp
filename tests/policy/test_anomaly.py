"""Tests for the `anomaly` policy dimension (plan-v2 E10): statistical baselines, no manual
thresholds. Uses a real `LedgerStore` (per-session history), never global in-memory state.
"""

from __future__ import annotations

from belay.ledger.store import LedgerStore
from belay.planner.model import EffectEstimate, Plan
from belay.policy.engine import PolicyEngine
from belay.policy.model import AnomalyDefaults, Cap, CapMatch, Defaults, PolicyDoc, default_policy


def _plan(
    *,
    plan_id: str,
    session_id: str = "s1",
    tool: str = "crm.bulk_delete",
    count: str,
    reversibility: str = "reversible",
) -> Plan:
    return Plan(
        plan_id=plan_id,
        session_id=session_id,
        tool=tool,
        args={},
        effects=[EffectEstimate(type="delete", resource="crm.record", count=count, estimate=True)],
        reversibility=reversibility,  # type: ignore[arg-type]
        confidence="medium",
        created_at="2026-07-22T12:00:00+00:00",
        expires_at="2026-07-22T12:10:00+00:00",
    )


def _seed_normal_history(ledger: LedgerStore, engine: PolicyEngine, policy: PolicyDoc, n: int = 10):
    """Run `n` "normal" (~10-row) calls through the engine, appending `plan_created` like
    `Lifecycle.govern_and_execute()` does, so a real baseline builds up."""
    for i in range(n):
        p = _plan(plan_id=f"p{i}", count=str(10 + (i % 3)))
        ledger.append(p.session_id, "plan_created", p.model_dump(mode="json"))
        engine.evaluate(p, policy)


def test_cold_start_never_flags_anomaly_below_min_samples() -> None:
    """Fewer than `min_samples` prior calls: anomaly never fires, regardless of magnitude."""
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger)
    policy = default_policy()
    _seed_normal_history(ledger, engine, policy, n=5)  # below default min_samples=10

    huge = _plan(plan_id="p_huge", count="5000")
    ledger.append(huge.session_id, "plan_created", huge.model_dump(mode="json"))
    result = engine.evaluate(huge, policy)
    assert result.verdict == "allow"


def test_trigger_after_baseline_established_pauses_with_explainable_reason() -> None:
    """After ~10 calls averaging ~10 rows, a 500-row call pauses with baseline/observed/deviation
    spelled out in `reasons`."""
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger)
    policy = default_policy()
    _seed_normal_history(ledger, engine, policy, n=10)

    outlier = _plan(plan_id="p_outlier", count="500")
    ledger.append(outlier.session_id, "plan_created", outlier.model_dump(mode="json"))
    result = engine.evaluate(outlier, policy)

    assert result.verdict == "pause"
    assert len(result.reasons) == 1
    reason = result.reasons[0]
    assert "500" in reason
    assert "baseline" in reason
    assert "x the" in reason  # human-readable "Nx the trailing baseline"


def test_zero_policy_config_still_catches_the_outlier() -> None:
    """The literal win condition: no `Cap`, no policy config beyond defaults, still pauses a
    50x-normal bulk action purely from its own ledger history."""
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger)
    policy = default_policy()  # zero caps, zero tool rules -- nothing manually configured
    assert policy.caps == []
    assert policy.tools == []

    for i in range(12):
        normal = _plan(plan_id=f"n{i}", count="10")
        ledger.append(normal.session_id, "plan_created", normal.model_dump(mode="json"))
        baseline_result = engine.evaluate(normal, policy)
        assert baseline_result.verdict == "allow"  # normal traffic sails through

    outlier = _plan(plan_id="p_outlier", count="500")  # 50x
    ledger.append(outlier.session_id, "plan_created", outlier.model_dump(mode="json"))
    result = engine.evaluate(outlier, policy)

    assert result.verdict == "pause"
    assert result.requires_approval is True


def test_anomaly_composes_with_cap_without_double_firing_same_effect() -> None:
    """A `Cap` and the anomaly baseline don't both fire on the same effect -- the cap's own
    verdict is enough; the anomaly reason is suppressed for that effect."""
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger)
    policy = PolicyDoc(
        caps=[
            Cap(
                match=CapMatch(effect="delete", resource="crm.record"),
                max_count=50,
                over="pause",
            )
        ]
    )
    _seed_normal_history(ledger, engine, policy, n=10)

    outlier = _plan(plan_id="p_outlier", count="500")
    ledger.append(outlier.session_id, "plan_created", outlier.model_dump(mode="json"))
    result = engine.evaluate(outlier, policy)

    assert result.verdict == "pause"
    assert len(result.reasons) == 1  # only the cap fired, not the cap AND the anomaly
    assert result.reasons[0] == "caps[0]"


def test_anomaly_fires_independently_when_no_cap_covers_the_effect() -> None:
    """A cap on a *different* effect doesn't suppress an independent anomaly finding."""
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger)
    policy = PolicyDoc(
        caps=[
            Cap(
                match=CapMatch(effect="send", resource="email.message"),
                max_count=1,
                over="pause",
            )
        ]
    )
    _seed_normal_history(ledger, engine, policy, n=10)

    outlier = _plan(plan_id="p_outlier", count="500")
    ledger.append(outlier.session_id, "plan_created", outlier.model_dump(mode="json"))
    result = engine.evaluate(outlier, policy)

    assert result.verdict == "pause"
    assert any(r.startswith("anomaly:") for r in result.reasons)


def test_composes_with_irreversible_and_quiet_hours_max_severity_wins() -> None:
    """Anomaly stacks with the existing dimensions under the same max-severity rule."""
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger)
    policy = PolicyDoc(defaults=Defaults(irreversible="deny"))
    _seed_normal_history(ledger, engine, policy, n=10)

    outlier = _plan(plan_id="p_outlier", count="500", reversibility="irreversible")
    ledger.append(outlier.session_id, "plan_created", outlier.model_dump(mode="json"))
    result = engine.evaluate(outlier, policy)

    assert result.verdict == "deny"  # deny (irreversible default) beats pause (anomaly)
    assert any(r.startswith("anomaly:") for r in result.reasons)
    assert "defaults.irreversible" in result.reasons


def test_anomaly_disabled_per_tool_via_exclude_glob() -> None:
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger)
    policy = PolicyDoc(defaults=Defaults(anomaly=AnomalyDefaults(exclude=["crm.bulk_delete"])))
    _seed_normal_history(ledger, engine, policy, n=10)

    outlier = _plan(plan_id="p_outlier", count="500")
    ledger.append(outlier.session_id, "plan_created", outlier.model_dump(mode="json"))
    result = engine.evaluate(outlier, policy)
    assert result.verdict == "allow"


def test_baseline_is_per_session_no_cross_contamination() -> None:
    """Two independent sessions never share a baseline: session 2's first outlier-looking call
    is still cold-start there even though session 1 has plenty of (unrelated) history."""
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger)
    policy = default_policy()
    _seed_normal_history(ledger, engine, policy, n=10)  # all in session "s1"

    fresh_session_call = _plan(plan_id="q1", session_id="s2", count="500")
    ledger.append(
        fresh_session_call.session_id, "plan_created", fresh_session_call.model_dump(mode="json")
    )
    result = engine.evaluate(fresh_session_call, policy)
    assert result.verdict == "allow"  # session s2 has zero history of its own: cold start
