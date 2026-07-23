"""Tests for PolicyEngine.evaluate() (spec §6.2, §6.3, §6.4)."""

from __future__ import annotations

from datetime import UTC, datetime

from belay.clock import FixedClock
from belay.planner.model import EffectEstimate, Plan
from belay.policy.engine import PolicyEngine
from belay.policy.model import (
    Cap,
    CapMatch,
    Defaults,
    MaxAmount,
    PolicyDoc,
    QuietHours,
    ToolRule,
    default_policy,
)
from hypothesis import given
from hypothesis import strategies as st


def _plan(
    *,
    tool: str = "crm.bulk_delete",
    reversibility: str = "reversible",
    effects: list[EffectEstimate] | None = None,
    unknown: list[dict] | None = None,
) -> Plan:
    return Plan(
        plan_id="p_1",
        session_id="s1",
        tool=tool,
        args={},
        effects=effects or [],
        reversibility=reversibility,  # type: ignore[arg-type]
        confidence="medium",
        unknown=unknown or [],
        created_at="2026-07-22T12:00:00+00:00",
        expires_at="2026-07-22T12:10:00+00:00",
    )


def test_allow_when_nothing_fires() -> None:
    engine = PolicyEngine()
    result = engine.evaluate(_plan(), default_policy())
    assert result.verdict == "allow"
    assert result.reasons == []


def test_irreversible_defaults_to_pause() -> None:
    engine = PolicyEngine()
    result = engine.evaluate(_plan(reversibility="irreversible"), default_policy())
    assert result.verdict == "pause"
    assert result.reasons == ["defaults.irreversible"]
    assert result.requires_approval is True


def test_cap_exceeded_produces_its_own_verdict_and_rule_id() -> None:
    """@spec("6.2") — every verdict MUST be recorded with the rule ids that fired."""
    policy = PolicyDoc(
        caps=[Cap(match=CapMatch(effect="update", resource="db.*"), max_count=100, over="pause")]
    )
    effects = [
        EffectEstimate(
            type="update", resource="db.rows", count="150", estimate=True, basis="dry_run"
        )
    ]
    result = PolicyEngine().evaluate(_plan(effects=effects), policy)
    assert result.verdict == "pause"
    assert result.reasons == ["caps[0]"]


def test_cap_spend_over_denies() -> None:
    policy = PolicyDoc(
        caps=[
            Cap(
                match=CapMatch(effect="spend"),
                max_amount=MaxAmount(value=50, currency="EUR"),
                over="deny",
            )
        ]
    )
    effects = [
        EffectEstimate(type="spend", resource="payments", amount={"value": 75, "currency": "EUR"})
    ]
    result = PolicyEngine().evaluate(_plan(effects=effects), policy)
    assert result.verdict == "deny"
    assert result.reasons == ["caps[0]"]


def test_most_restrictive_verdict_wins_across_dimensions() -> None:
    # A cap that pauses and a tool rule that denies: deny wins.
    policy = PolicyDoc(
        caps=[Cap(match=CapMatch(effect="delete"), max_count=1, over="pause")],
        tools=[ToolRule(match="fs.delete_*", verdict="deny")],
    )
    effects = [EffectEstimate(type="delete", resource="fs.file", count="5", estimate=True)]
    result = PolicyEngine().evaluate(_plan(tool="fs.delete_all", effects=effects), policy)
    assert result.verdict == "deny"
    assert set(result.reasons) == {"caps[0]", "tools[0]"}


def test_first_match_wins_within_tools_dimension() -> None:
    policy = PolicyDoc(
        tools=[
            ToolRule(match="fs.delete_*", verdict="pause"),
            ToolRule(match="fs.delete_all", verdict="deny"),
        ]
    )
    result = PolicyEngine().evaluate(_plan(tool="fs.delete_all"), policy)
    assert result.verdict == "pause"
    assert result.reasons == ["tools[0]"]


def test_tool_rule_relaxes_irreversible_default_and_is_recorded() -> None:
    policy = PolicyDoc(
        defaults=Defaults(irreversible="pause"),
        tools=[ToolRule(match="crm.send_receipt", verdict="allow")],
    )
    result = PolicyEngine().evaluate(
        _plan(tool="crm.send_receipt", reversibility="irreversible"), policy
    )
    assert result.verdict == "allow"
    assert result.relaxations == ["tools[0]"]


def test_tool_rule_that_does_not_relax_is_not_recorded_as_a_relaxation() -> None:
    policy = PolicyDoc(
        defaults=Defaults(irreversible="pause"),
        tools=[ToolRule(match="mail.send", verdict="deny")],
    )
    result = PolicyEngine().evaluate(_plan(tool="mail.send", reversibility="irreversible"), policy)
    assert result.verdict == "deny"
    assert result.relaxations == []


def test_quiet_hours_pauses_matching_effect_in_window() -> None:
    clock = FixedClock(datetime(2026, 7, 22, 2, 0, tzinfo=UTC))  # 02:00, inside 00:00-07:00
    policy = PolicyDoc(
        quiet_hours=[
            QuietHours(between=("00:00", "07:00"), scope=CapMatch(effect="send"), verdict="pause")
        ]
    )
    effects = [EffectEstimate(type="send", resource="email", recipients="3")]
    result = PolicyEngine(clock=clock).evaluate(_plan(effects=effects), policy)
    assert result.verdict == "pause"
    assert result.reasons == ["quiet_hours[0]"]


def test_quiet_hours_does_not_fire_outside_window() -> None:
    clock = FixedClock(datetime(2026, 7, 22, 12, 0, tzinfo=UTC))
    policy = PolicyDoc(
        quiet_hours=[
            QuietHours(between=("00:00", "07:00"), scope=CapMatch(effect="send"), verdict="pause")
        ]
    )
    effects = [EffectEstimate(type="send", resource="email", recipients="3")]
    result = PolicyEngine(clock=clock).evaluate(_plan(effects=effects), policy)
    assert result.verdict == "allow"


def test_unknown_top_level_field_in_policy_doc_is_rejected() -> None:
    """@spec("14.2") — policies are authority (like contracts): unknown fields MUST be rejected."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PolicyDoc.model_validate({"belay_policy": "0.1", "not_a_real_field": True})


def test_unknown_effects_apply_default_verdict() -> None:
    policy = PolicyDoc(defaults=Defaults(unknown_effects="pause"))
    result = PolicyEngine().evaluate(
        _plan(unknown=[{"type": "delete", "resource": "crm.record"}]), policy
    )
    assert result.verdict == "pause"
    assert result.reasons == ["defaults.unknown_effects"]


@given(unknown_count=st.integers(min_value=1, max_value=5))
def test_property_unknown_effects_never_resolve_to_allow_when_default_is_pause(
    unknown_count: int,
) -> None:
    """@spec("6.3") — caps MUST be evaluated against the upper bound; unknowns are worst-case."""
    policy = PolicyDoc(defaults=Defaults(unknown_effects="pause"))
    unknown = [{"type": "delete", "resource": f"r{i}"} for i in range(unknown_count)]
    result = PolicyEngine().evaluate(_plan(unknown=unknown), policy)
    assert result.verdict != "allow"
