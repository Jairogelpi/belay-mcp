"""Tests for `belay/policy/explain.py` (plan-v2 E16): blast-radius self-explanation.

`explain()` is a PURE FORMATTING function -- these tests assert every number
in its output is byte-for-byte traceable back to the real `PolicyResult`
produced by `PolicyEngine.evaluate()` (never invented, never re-derived).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from belay.clock import FixedClock
from belay.contracts.model import Contract, Effect, SqlHint, Undo
from belay.ledger.store import LedgerStore
from belay.planner.model import EffectEstimate, Plan
from belay.policy.engine import PolicyEngine
from belay.policy.explain import Explanation, explain
from belay.policy.model import (
    Cap,
    CapMatch,
    Defaults,
    PolicyDoc,
    QuotaDefaults,
    default_policy,
)
from hypothesis import given, settings
from hypothesis import strategies as st

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)


def _plan(
    *,
    plan_id: str = "p1",
    session_id: str = "s1",
    tool: str = "crm.bulk_delete",
    reversibility: str = "reversible",
    effects: list[EffectEstimate] | None = None,
) -> Plan:
    return Plan(
        plan_id=plan_id,
        session_id=session_id,
        tool=tool,
        args={},
        effects=effects
        or [EffectEstimate(type="delete", resource="crm.record", count="1", estimate=True)],
        reversibility=reversibility,  # type: ignore[arg-type]
        confidence="medium",
        created_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(minutes=10)).isoformat(),
    )


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


# -- 1. anomaly-triggered ----------------------------------------------------


def test_anomaly_triggered_headline_traces_to_real_reasons() -> None:
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger)
    policy = default_policy()
    for i in range(10):
        p = _plan(
            plan_id=f"n{i}",
            effects=[EffectEstimate(type="delete", resource="crm.record", count="10")],
        )
        ledger.append(p.session_id, "plan_created", p.model_dump(mode="json"))
        engine.evaluate(p, policy)

    outlier = _plan(
        plan_id="p_outlier",
        effects=[EffectEstimate(type="delete", resource="crm.record", count="500")],
    )
    ledger.append(outlier.session_id, "plan_created", outlier.model_dump(mode="json"))
    result = engine.evaluate(outlier, policy)
    assert result.verdict == "pause"

    explanation = explain(result, outlier)
    assert explanation.verdict == "pause"
    assert len(explanation.dimensions) == 1
    dim = explanation.dimensions[0]
    assert dim.name == "anomaly"
    assert dim.rule == result.reasons[0]
    # every number in the headline is byte-for-byte one already in `reasons`
    assert _numbers(explanation.headline) <= _numbers(" ".join(result.reasons))
    assert "500" in explanation.headline
    assert "baseline" in explanation.headline


# -- 2. quota-triggered -------------------------------------------------------


def test_quota_triggered_headline_traces_to_real_reasons() -> None:
    from tests.policy.test_quota import _seed_approved_executed_irreversible

    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger, clock=FixedClock(NOW))
    policy = PolicyDoc(
        defaults=Defaults(
            quota=QuotaDefaults(enabled=True, max_irreversible_actions=3, window="1d")
        )
    )
    for i in range(3):
        _seed_approved_executed_irreversible(
            ledger, session_id="s1", identity="alice", step_seq=i, at=NOW
        )
    new_plan = _plan(
        plan_id="p_new",
        tool="mail.send",
        reversibility="irreversible",
        effects=[EffectEstimate(type="send", resource="email.message", count="1")],
    )
    ledger.append("s1", "plan_created", new_plan.model_dump(mode="json"), step_seq=99)
    result = engine.evaluate(new_plan, policy)
    assert result.verdict == "pause"

    explanation = explain(result, new_plan)
    dim = next(d for d in explanation.dimensions if d.name == "quota")
    assert dim.rule == next(r for r in result.reasons if r.startswith("quota:"))
    assert "alice" in explanation.headline
    assert "3" in explanation.headline
    assert "1d" in explanation.headline
    assert _numbers(explanation.headline) <= _numbers(" ".join(result.reasons))


# -- 3. cap-triggered and irreversible-default-triggered ---------------------


def test_cap_triggered_headline_traces_to_rule_id_and_plan_only() -> None:
    engine = PolicyEngine()
    policy = PolicyDoc(
        caps=[
            Cap(
                match=CapMatch(effect="delete", resource="crm.record"),
                max_count=1,
                over="pause",
            )
        ]
    )
    plan = _plan(
        effects=[EffectEstimate(type="delete", resource="crm.record", count="5")],
    )
    result = engine.evaluate(plan, policy)
    assert result.verdict == "pause"
    assert result.reasons == ["caps[0]"]

    explanation = explain(result, plan)
    dim = explanation.dimensions[0]
    assert dim.name == "caps"
    assert dim.rule == "caps[0]"
    assert "caps[0]" in explanation.headline
    assert plan.tool in explanation.headline
    # the only digit present ("0" in "caps[0]") is the rule id itself, already in reasons.
    assert _numbers(explanation.headline) == _numbers("caps[0]")


def test_irreversible_default_triggered_headline_traces_to_rule_id() -> None:
    engine = PolicyEngine()
    plan = _plan(reversibility="irreversible")
    result = engine.evaluate(plan, default_policy())
    assert result.verdict == "pause"
    assert result.reasons == ["defaults.irreversible"]

    explanation = explain(result, plan)
    dim = explanation.dimensions[0]
    assert dim.name == "irreversible_default"
    assert dim.rule == "defaults.irreversible"
    assert "defaults.irreversible" in explanation.headline
    assert plan.tool in explanation.headline


# -- 4. allow verdict -> empty/minimal dimensions ----------------------------


def test_allow_verdict_has_empty_dimensions_and_no_fabricated_concern() -> None:
    engine = PolicyEngine()
    plan = _plan()
    result = engine.evaluate(plan, default_policy())
    assert result.verdict == "allow"

    explanation = explain(result, plan)
    assert explanation.verdict == "allow"
    assert explanation.dimensions == []
    assert explanation.suggested_action is None
    assert "allow" in explanation.headline


# -- 5. suggested_action: present only when deterministic --------------------


def _conditional_contract_with_args_condition() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="crm.delete",
        reversibility="conditional",
        conditions=["$args.confirm == true"],
        undo=Undo(tool="crm.create", args={"id": "$args.id"}),
        effects=[Effect(type="delete", resource="crm.record", count="1")],
    )


def _reversible_contract_with_sql_args_param() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="crm.bulk_delete",
        reversibility="reversible",
        undo=Undo(tool="crm.import_records", args={}),
        effects=[Effect(type="delete", resource="crm.record")],
        sql=SqlHint(
            statement="DELETE FROM records WHERE year < :cutoff",
            params={"cutoff": "$args.before_year"},
        ),
    )


def _no_narrowing_contract() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="crm.export_records",
        reversibility="irreversible",
        effects=[Effect(type="read", resource="crm.record")],
    )


def test_suggested_action_present_for_conditions_bearing_narrowing_argument() -> None:
    engine = PolicyEngine()
    plan = _plan(tool="crm.delete", reversibility="conditional")
    policy = PolicyDoc(caps=[Cap(match=CapMatch(effect="delete"), max_count=0, over="pause")])
    result = engine.evaluate(plan, policy)
    assert result.verdict == "pause"

    explanation = explain(result, plan, contract=_conditional_contract_with_args_condition())
    assert explanation.suggested_action is not None
    assert "args.confirm" in explanation.suggested_action
    assert "crm.delete" in explanation.suggested_action


def test_suggested_action_present_for_sql_params_narrowing_argument() -> None:
    engine = PolicyEngine()
    plan = _plan(tool="crm.bulk_delete")
    policy = PolicyDoc(caps=[Cap(match=CapMatch(effect="delete"), max_count=0, over="pause")])
    result = engine.evaluate(plan, policy)
    assert result.verdict == "pause"

    explanation = explain(result, plan, contract=_reversible_contract_with_sql_args_param())
    assert explanation.suggested_action is not None
    assert "args.before_year" in explanation.suggested_action


def test_suggested_action_absent_when_no_deterministic_rule_applies() -> None:
    engine = PolicyEngine()
    plan = _plan(tool="crm.export_records", reversibility="irreversible")
    result = engine.evaluate(plan, default_policy())
    assert result.verdict == "pause"

    # no contract at all -- never guessed
    assert explain(result, plan).suggested_action is None
    # a contract with nothing narrowing to suggest -- still never guessed
    assert explain(result, plan, contract=_no_narrowing_contract()).suggested_action is None


# -- 7. disclosure-policy consistency across all firing dimensions ----------


def test_disclosure_policy_is_applied_consistently_across_dimensions() -> None:
    """Full transparency, uniformly: whatever numbers a dimension's `reasons` entry
    carries are echoed verbatim in its `detail`, never selectively redacted."""
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger, clock=FixedClock(NOW))
    policy = PolicyDoc(
        defaults=Defaults(irreversible="deny"),
        caps=[Cap(match=CapMatch(effect="send"), max_count=0, over="deny")],
    )
    for i in range(10):
        p = _plan(
            plan_id=f"n{i}",
            effects=[EffectEstimate(type="delete", resource="crm.record", count="10")],
        )
        ledger.append(p.session_id, "plan_created", p.model_dump(mode="json"))
        engine.evaluate(p, policy)

    plan = _plan(
        plan_id="p_multi",
        reversibility="irreversible",
        effects=[
            EffectEstimate(type="delete", resource="crm.record", count="500"),
            EffectEstimate(type="send", resource="email.message", count="1"),
        ],
    )
    ledger.append(plan.session_id, "plan_created", plan.model_dump(mode="json"))
    result = engine.evaluate(plan, policy)
    assert len(result.reasons) >= 2  # multiple dimensions fire together

    explanation = explain(result, plan)
    for dim in explanation.dimensions:
        assert dim.rule in dim.detail  # the full, real reason text is never redacted
    assert set(explanation.dimensions[i].rule for i in range(len(explanation.dimensions))) == set(
        result.reasons
    )


# -- 8. Hypothesis property test: traceability across the real engine -------


@given(
    n_normal=st.integers(min_value=0, max_value=15),
    outlier_count=st.integers(min_value=1, max_value=2000),
    reversibility=st.sampled_from(["reversible", "irreversible", "conditional"]),
    max_count_cap=st.one_of(st.none(), st.integers(min_value=0, max_value=100)),
)
@settings(max_examples=60, deadline=None)
def test_property_explain_never_raises_and_never_invents_a_number(
    n_normal: int, outlier_count: int, reversibility: str, max_count_cap: int | None
) -> None:
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger)
    caps = (
        [Cap(match=CapMatch(effect="delete"), max_count=max_count_cap, over="pause")]
        if max_count_cap is not None
        else []
    )
    policy = PolicyDoc(caps=caps)

    for i in range(n_normal):
        p = _plan(
            plan_id=f"n{i}",
            effects=[EffectEstimate(type="delete", resource="crm.record", count="10")],
        )
        ledger.append(p.session_id, "plan_created", p.model_dump(mode="json"))
        engine.evaluate(p, policy)

    plan = _plan(
        plan_id="p_probe",
        reversibility=reversibility,
        effects=[EffectEstimate(type="delete", resource="crm.record", count=str(outlier_count))],
    )
    ledger.append(plan.session_id, "plan_created", plan.model_dump(mode="json"))
    result = engine.evaluate(plan, policy)

    explanation = explain(result, plan)  # must never raise

    reasons_numbers = _numbers(" ".join(result.reasons))
    assert _numbers(explanation.headline) <= reasons_numbers
    for dim in explanation.dimensions:
        assert _numbers(dim.detail) <= reasons_numbers | _numbers(dim.rule)
    assert isinstance(explanation, Explanation)
