"""E6 exit criterion (plan.md): a 5-step saga against `examples/crm-mock`
with a failure injected at step 4 and `auto_compensate: true` leaves the CRM
in its exact initial state.

Runs the real `examples/crm-mock` server as a stdio MCP subprocess -- a real
upstream boundary, not a mock -- and drives it through `SagaExecutor.run_saga`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from belay.contracts.model import Capture, Contract, Effect, Undo
from belay.executor.saga import SagaExecutor, SagaStep
from belay.ledger.store import LedgerStore
from belay.proxy.upstream import connect_stdio

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _update_contract(tool: str) -> Contract:
    return Contract(
        belay_contract="0.1",
        tool=tool,
        reversibility="reversible",
        capture=Capture(tool="crm.get", args={"id": "$args.id"}, **{"as": "before"}),
        undo=Undo(tool=tool, args={"id": "$args.id", "fields": "$state.before.fields"}),
        effects=[Effect(type="update", resource="crm.record", count="1")],
    )


@pytest.mark.slow
async def test_five_step_saga_fails_at_step_4_auto_compensates_to_initial_state() -> None:
    async with connect_stdio(
        sys.executable, [str(REPO_ROOT / "examples" / "crm-mock" / "server.py")]
    ) as upstream:

        async def executor(tool: str, args: dict) -> dict:
            result = await upstream.call_tool(tool, args)
            if result.isError:
                raise RuntimeError(str(result.content))
            content = result.structuredContent or {}
            return dict(content.get("result", content))

        # Seed two records the saga will mutate.
        await executor("crm.create", {"id": "a", "fields": {"name": "Alice", "stage": "lead"}})
        await executor("crm.create", {"id": "b", "fields": {"name": "Bob", "stage": "lead"}})
        initial = await executor("crm.export_records", {})

        ledger = LedgerStore()
        saga = SagaExecutor(ledger=ledger)

        undo = _update_contract("crm.update")
        steps = [
            SagaStep(
                "crm.update", {"id": "a", "fields": {"name": "Alice", "stage": "qualified"}}, undo
            ),
            SagaStep(
                "crm.update", {"id": "b", "fields": {"name": "Bob", "stage": "qualified"}}, undo
            ),
            SagaStep("crm.update", {"id": "a", "fields": {"name": "Alice", "stage": "won"}}, undo),
            SagaStep("crm.update", {"id": "does-not-exist", "fields": {}}, undo),
            SagaStep("crm.update", {"id": "b", "fields": {"name": "Bob", "stage": "won"}}, undo),
        ]

        async def flaky_executor(tool: str, args: dict) -> dict:
            if tool == "crm.update" and args.get("id") == "does-not-exist":
                raise RuntimeError("simulated step-4 failure")
            return await executor(tool, args)

        report = await saga.run_saga("s_crm_demo", steps, flaky_executor, auto_compensate=True)

        assert report.failed is not None
        assert len(report.committed) == 3
        assert report.compensated == [3, 2, 1]  # strict reverse order

        final = await executor("crm.export_records", {})
        assert final == initial, "the CRM must be back in its exact initial state"
