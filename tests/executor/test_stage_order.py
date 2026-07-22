"""The step lifecycle's exact normative order (spec §8.1) — the most important
test in the repo (plan.md E6): inject a failure right after each of the six
stages and verify both the resulting ledger events and that nothing past the
failure point ever ran.
"""

from __future__ import annotations

import pytest
from belay.contracts.model import Capture, Contract, Effect, Undo
from belay.executor.saga import STAGES, SagaExecutor
from belay.ledger.store import LedgerStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _contract() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="crm.update",
        reversibility="reversible",
        capture=Capture(tool="crm.get", args={"id": "$args.id"}, **{"as": "before"}),
        undo=Undo(tool="crm.update", args={"id": "$args.id", "fields": "$state.before.fields"}),
        idempotency_key="$args.id",
        effects=[Effect(type="update", resource="crm.record", count="1")],
    )


async def _executor(tool: str, args: dict) -> dict:
    if tool == "crm.get":
        return {"id": args["id"], "fields": {"name": "orig"}}
    return {"id": args["id"], "ok": True}


class _StopAt:
    """Raises right after the named stage completes; a no-op otherwise."""

    def __init__(self, stage: str) -> None:
        self.stage = stage

    def __call__(self, stage: str) -> None:
        if stage == self.stage:
            raise RuntimeError(f"injected failure after {stage}")


EXPECTED_EVENTS_BY_STAGE = {
    "journaled": ["step_journaled", "step_failed"],
    "capturing": ["step_journaled", "state_captured", "step_failed"],
    "calling": ["step_journaled", "state_captured", "tool_called", "step_failed"],
    "result_recorded": [
        "step_journaled",
        "state_captured",
        "tool_called",
        "result_recorded",
        "step_failed",
    ],
    "compensation_registered": [
        "step_journaled",
        "state_captured",
        "tool_called",
        "result_recorded",
        "compensation_registered",
        "step_failed",
    ],
    "committed": [
        "step_journaled",
        "state_captured",
        "tool_called",
        "result_recorded",
        "compensation_registered",
        "step_committed",
    ],
}


def test_six_stages_are_exactly_spec_8_1() -> None:
    assert STAGES == (
        "journaled",
        "capturing",
        "calling",
        "result_recorded",
        "compensation_registered",
        "committed",
    )


@pytest.mark.parametrize("stage", STAGES)
async def test_failure_injected_after_each_stage(stage: str) -> None:
    ledger = LedgerStore()
    saga = SagaExecutor(ledger=ledger)

    if stage == "committed":
        # "committed" has nothing after it to fail -- this is the happy
        # path, proving the full six-stage sequence completes cleanly end
        # to end with no injection at all.
        result = await saga.run_step("s1", 1, "crm.update", {"id": "42"}, _contract(), _executor)
        assert result.step_seq == 1
    else:
        hook = _StopAt(stage)
        with pytest.raises(RuntimeError):
            await saga.run_step(
                "s1", 1, "crm.update", {"id": "42"}, _contract(), _executor, on_stage=hook
            )

    events = ledger.read("s1")
    assert [e.type for e in events] == EXPECTED_EVENTS_BY_STAGE[stage]
