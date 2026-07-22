"""Tests for Planner.plan() dry-run bases and plan expiration (spec §5.3, §5.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from belay.clock import FixedClock
from belay.contracts.model import Contract, Effect, Undo
from belay.errors import BelayError
from belay.planner.model import PlanningSession
from belay.planner.planner import Planner, check_plan_binding

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _write_contract() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="crm.update_record",
        reversibility="reversible",
        undo=Undo(tool="crm.update_record", args={}),
        effects=[Effect(type="update", resource="crm.record", count="1")],
    )


def _bulk_delete_contract() -> Contract:
    # No declared count: the contract cannot promise a number for a bulk op.
    return Contract(
        belay_contract="0.1",
        tool="crm.bulk_delete",
        reversibility="irreversible",
        effects=[Effect(type="delete", resource="crm.record")],
    )


async def test_contract_basis_plan_marks_declared_counts_as_estimates() -> None:
    planner = Planner(clock=FixedClock(datetime(2026, 7, 22, 12, 0, tzinfo=UTC)))
    session = PlanningSession(session_id="s1", contract=_write_contract())

    plan = await planner.plan("crm.update_record", {"id": 1}, session)

    assert [e.model_dump(mode="json") for e in plan.effects] == [
        {
            "type": "update",
            "resource": "crm.record",
            "count": "1",
            "estimate": True,
            "basis": "contract",
            "amount": None,
            "recipients": None,
        }
    ]
    assert plan.unknown == []
    assert plan.confidence == "medium"
    assert plan.reversibility == "reversible"


async def test_effect_with_no_declared_count_is_unknown_not_a_guess() -> None:
    planner = Planner()
    session = PlanningSession(session_id="s1", contract=_bulk_delete_contract())

    plan = await planner.plan("crm.bulk_delete", {}, session)

    assert plan.effects == []
    assert plan.unknown == [{"type": "delete", "resource": "crm.record"}]
    assert plan.confidence == "low"


async def test_no_contract_uses_implicit_effects_from_resolve() -> None:
    planner = Planner()
    session = PlanningSession(
        session_id="s1",
        contract=None,
        implicit_effects=[{"type": "read", "resource": "fs.list_files"}],
    )

    plan = await planner.plan("fs.list_files", {}, session)

    effects = [e.model_dump(mode="json") for e in plan.effects]
    assert effects[0]["type"] == "read"
    assert effects[0]["basis"] == "contract"
    assert effects[0]["estimate"] is False
    assert plan.reversibility == "reversible"


async def test_native_dry_run_takes_precedence_over_contract_basis() -> None:
    async def native(tool: str, args: dict) -> dict:
        return {"effects": [{"type": "delete", "resource": "crm.record", "count": "42"}]}

    planner = Planner()
    session = PlanningSession(
        session_id="s1", contract=_bulk_delete_contract(), native_dry_run=native
    )

    plan = await planner.plan("crm.bulk_delete", {}, session)

    effects = [e.model_dump(mode="json") for e in plan.effects]
    assert effects[0]["basis"] == "native_dry_run"
    assert effects[0]["count"] == "42"
    assert plan.unknown == []
    assert plan.confidence == "high"


async def test_native_dry_run_returning_none_falls_back_to_contract() -> None:
    async def native(tool: str, args: dict) -> dict | None:
        return None

    planner = Planner()
    session = PlanningSession(session_id="s1", contract=_write_contract(), native_dry_run=native)

    plan = await planner.plan("crm.update_record", {"id": 1}, session)

    effects = [e.model_dump(mode="json") for e in plan.effects]
    assert effects[0]["basis"] == "contract"


async def test_plan_expiration_rejects_execution_after_ttl() -> None:
    clock = FixedClock(datetime(2026, 7, 22, 12, 0, tzinfo=UTC))
    planner = Planner(clock=clock, plan_ttl_seconds=600)
    session = PlanningSession(session_id="s1", contract=_write_contract())
    plan = await planner.plan("crm.update_record", {"id": 1}, session)

    clock.set(clock.now() + timedelta(minutes=11))
    with pytest.raises(BelayError) as excinfo:
        check_plan_binding(plan, "crm.update_record", {"id": 1}, clock=clock)
    assert excinfo.value.code == "plan_expired"


async def test_plan_execution_within_ttl_is_accepted() -> None:
    clock = FixedClock(datetime(2026, 7, 22, 12, 0, tzinfo=UTC))
    planner = Planner(clock=clock, plan_ttl_seconds=600)
    session = PlanningSession(session_id="s1", contract=_write_contract())
    plan = await planner.plan("crm.update_record", {"id": 1}, session)

    clock.set(clock.now() + timedelta(minutes=5))
    check_plan_binding(plan, "crm.update_record", {"id": 1}, clock=clock)  # no raise


async def test_plan_mismatch_on_non_identical_args() -> None:
    clock = FixedClock(datetime(2026, 7, 22, 12, 0, tzinfo=UTC))
    planner = Planner(clock=clock)
    session = PlanningSession(session_id="s1", contract=_write_contract())
    plan = await planner.plan("crm.update_record", {"id": 1}, session)

    with pytest.raises(BelayError) as excinfo:
        check_plan_binding(plan, "crm.update_record", {"id": 2}, clock=clock)
    assert excinfo.value.code == "plan_mismatch"


async def test_plan_mismatch_on_different_tool() -> None:
    clock = FixedClock(datetime(2026, 7, 22, 12, 0, tzinfo=UTC))
    planner = Planner(clock=clock)
    session = PlanningSession(session_id="s1", contract=_write_contract())
    plan = await planner.plan("crm.update_record", {"id": 1}, session)

    with pytest.raises(BelayError) as excinfo:
        check_plan_binding(plan, "crm.other_tool", {"id": 1}, clock=clock)
    assert excinfo.value.code == "plan_mismatch"
