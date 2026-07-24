"""Redaction (spec §9.3) must cover compensation events too, not just the
forward call's own `step_journaled`/`tool_called`/`result_recorded` -- an
undo call materializes its `args` from the same `$args`/`$state` scope, so a
secret used to undo something (e.g. a password needed to revoke a grant) is
just as sensitive echoed back there."""

from __future__ import annotations

from typing import Any

import pytest
from belay.contracts.model import Contract, Effect, Undo
from belay.executor.saga import SagaExecutor
from belay.ledger.store import LedgerStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _contract() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="auth.grant",
        reversibility="reversible",
        undo=Undo(tool="auth.revoke", args={"password": "$args.password"}),
        effects=[Effect(type="create", resource="auth.grant", count="1")],
        redact=["$args.password"],
    )


async def _executor(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


async def test_compensation_registered_and_executed_redact_declared_secret() -> None:
    ledger = LedgerStore()
    saga = SagaExecutor(ledger=ledger)
    contract = _contract()

    step = await saga.run_step(
        "s1", 1, "auth.grant", {"password": "hunter2", "user": "jairo"}, contract, _executor
    )
    await saga.compensate("s1", step, _executor, contract)

    events = ledger.read("s1")
    blob = str([e.payload for e in events])
    assert "hunter2" not in blob

    registered = next(e for e in events if e.type == "compensation_registered")
    assert registered.payload["args"]["password"]["redacted"] is True

    executed = next(e for e in events if e.type == "compensation_executed")
    assert executed.payload["args"]["password"]["redacted"] is True


async def test_auto_unwind_compensation_also_redacts() -> None:
    """`run_saga(auto_compensate=True)`'s unwind path goes through the same
    `SagaExecutor.compensate` -- must thread the contract through there too."""
    from belay.executor.saga import SagaStep

    ledger = LedgerStore()
    saga = SagaExecutor(ledger=ledger)
    contract = _contract()

    async def failing_second_call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "auth.grant" and args.get("user") == "boom":
            raise RuntimeError("boom")
        return {"ok": True}

    steps = [
        SagaStep(
            tool="auth.grant", args={"password": "hunter2", "user": "a"}, contract=contract
        ),
        SagaStep(
            tool="auth.grant", args={"password": "hunter2", "user": "boom"}, contract=contract
        ),
    ]
    await saga.run_saga("s2", steps, failing_second_call, auto_compensate=True)

    events = ledger.read("s2")
    blob = str([e.payload for e in events])
    assert "hunter2" not in blob
