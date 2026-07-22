"""Capture runs before the call; its own contract must be read-only (spec §8.1)."""

from __future__ import annotations

import pytest
from belay.contracts.model import Capture, Contract, ContractSet, Effect, Undo
from belay.errors import BelayError
from belay.executor.saga import SagaExecutor
from belay.ledger.store import LedgerStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _reversible_with_capture() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="crm.update",
        reversibility="reversible",
        capture=Capture(tool="crm.get", args={"id": "$args.id"}, **{"as": "before"}),
        undo=Undo(tool="crm.update", args={"id": "$args.id", "fields": "$state.before.fields"}),
        effects=[Effect(type="update", resource="crm.record", count="1")],
    )


async def _executor(tool: str, args: dict) -> dict:
    calls.append(tool)
    if tool == "crm.get":
        return {"id": args["id"], "fields": {"name": "orig"}}
    return {"id": args["id"], "ok": True}


calls: list[str] = []


async def test_capture_runs_before_the_tool_call_and_snapshot_is_in_the_ledger() -> None:
    calls.clear()
    ledger = LedgerStore()
    saga = SagaExecutor(ledger=ledger)
    await saga.run_step("s1", 1, "crm.update", {"id": "1"}, _reversible_with_capture(), _executor)

    assert calls == ["crm.get", "crm.update"], "capture must execute before the real call"
    events = ledger.read("s1")
    captured = [e for e in events if e.type == "state_captured"]
    assert len(captured) == 1
    assert captured[0].payload["snapshot"] == {"id": "1", "fields": {"name": "orig"}}


async def test_non_read_only_capture_contract_is_rejected() -> None:
    ledger = LedgerStore()
    write_capture_contract = Contract(
        belay_contract="0.1",
        tool="crm.get",
        reversibility="irreversible",
        effects=[Effect(type="update", resource="crm.record", count="1")],  # not read-only!
    )
    contract_set = ContractSet(contracts={"crm.get": write_capture_contract}, set_hash="sha256:x")
    saga = SagaExecutor(ledger=ledger, contract_set=contract_set)

    with pytest.raises(BelayError) as excinfo:
        await saga.run_step(
            "s1", 1, "crm.update", {"id": "1"}, _reversible_with_capture(), _executor
        )
    assert excinfo.value.code == "contract_invalid"
