"""A `conditional` contract whose conditions are unmet at execution time
registers as irreversible even though it nominally declares `undo` (spec §4.2,
re-checked at execution time per the TOCTOU discipline of spec §12)."""

from __future__ import annotations

import pytest
from belay.contracts.model import Capture, Contract, Effect, Undo
from belay.executor.saga import SagaExecutor
from belay.ledger.store import LedgerStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _conditional_contract() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="fs.delete_file",
        reversibility="conditional",
        conditions=["$state.before.existed == true"],
        capture=Capture(tool="fs.stat", args={"path": "$args.path"}, **{"as": "before"}),
        undo=Undo(
            tool="fs.write_file",
            args={"path": "$args.path", "content": "$state.before.content"},
        ),
        effects=[Effect(type="delete", resource="fs.file", count="1")],
    )


async def _executor_file_did_not_exist(tool: str, args: dict) -> dict:
    if tool == "fs.stat":
        return {"existed": False}
    return {"ok": True}


async def _executor_file_existed(tool: str, args: dict) -> dict:
    if tool == "fs.stat":
        return {"existed": True, "content": "hello"}
    return {"ok": True}


async def test_conditional_step_with_unmet_conditions_is_irreversible() -> None:
    ledger = LedgerStore()
    saga = SagaExecutor(ledger=ledger)
    outcome = await saga.run_step(
        "s1",
        1,
        "fs.delete_file",
        {"path": "a.txt"},
        _conditional_contract(),
        _executor_file_did_not_exist,
    )
    assert outcome.compensation == {"reversible": False, "reason": "conditional_unmet"}
    events = {e.type: e for e in ledger.read("s1")}
    assert events["compensation_registered"].payload == {
        "reversible": False,
        "reason": "conditional_unmet",
    }
    assert "step_committed" in events, "step still commits -- it just can't be undone"


async def test_conditional_step_with_met_conditions_is_reversible() -> None:
    ledger = LedgerStore()
    saga = SagaExecutor(ledger=ledger)
    outcome = await saga.run_step(
        "s1",
        1,
        "fs.delete_file",
        {"path": "a.txt"},
        _conditional_contract(),
        _executor_file_existed,
    )
    assert outcome.compensation["reversible"] is True
    assert outcome.compensation["tool"] == "fs.write_file"
    assert outcome.compensation["args"] == {"path": "a.txt", "content": "hello"}
