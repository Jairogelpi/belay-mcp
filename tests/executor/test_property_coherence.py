"""Property: any sequence of steps with random success/failure outcomes always
produces a ledger that passes `verify_coherence` (spec §9.2, plan.md E6)."""

from __future__ import annotations

import anyio
from belay.contracts.model import Contract, Effect, Undo
from belay.executor.saga import SagaExecutor, SagaStep
from belay.ledger.store import LedgerStore
from belay.ledger.verify import verify_coherence
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


def _reversible_contract(tool: str) -> Contract:
    return Contract(
        belay_contract="0.1",
        tool=tool,
        reversibility="reversible",
        undo=Undo(tool=tool, args={"undo": True}),
        effects=[Effect(type="update", resource="thing", count="1")],
    )


def _irreversible_contract(tool: str) -> Contract:
    return Contract(
        belay_contract="0.1",
        tool=tool,
        reversibility="irreversible",
        effects=[Effect(type="delete", resource="thing", count="1")],
    )


def _make_executor(outcomes: list[bool]):
    """Fails forward calls per `outcomes`; compensation calls always succeed.

    Compensation calls are recognizable by their frozen `{"undo": True}` args
    (spec §8.1.5's materialized undo payload) -- a real compensation failure
    path is exercised separately by `tests/executor/test_stage_order.py`, so
    this generator only needs to vary the forward-step outcomes.
    """
    calls = {"i": 0}

    async def executor(tool: str, args: dict) -> dict:
        if args.get("undo") is True:
            return {"ok": True, "undone": True}
        i = calls["i"]
        calls["i"] += 1
        if i < len(outcomes) and not outcomes[i]:
            raise RuntimeError("injected upstream failure")
        return {"ok": True}

    return executor


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    outcomes=st.lists(st.booleans(), min_size=1, max_size=8),
    reversible_flags=st.lists(st.booleans(), min_size=1, max_size=8),
)
def test_random_saga_always_yields_coherent_ledger(
    outcomes: list[bool], reversible_flags: list[bool]
) -> None:
    async def run() -> None:
        ledger = LedgerStore()
        saga = SagaExecutor(ledger=ledger)
        n = min(len(outcomes), len(reversible_flags))
        steps = [
            SagaStep(
                tool=f"tool.{i}",
                args={"i": i},
                contract=(
                    _reversible_contract(f"tool.{i}")
                    if reversible_flags[i]
                    else _irreversible_contract(f"tool.{i}")
                ),
            )
            for i in range(n)
        ]
        executor = _make_executor(outcomes[:n])
        await saga.run_saga("s1", steps, executor, auto_compensate=True)

        events = ledger.read("s1")
        report = verify_coherence(events)
        assert report.ok, report.errors

    anyio.run(run)
