"""Idempotency-key deduplication: repeating a call returns the recorded
result, and the upstream is called exactly once (spec §4.5, §8.1)."""

from __future__ import annotations

import pytest
from belay.contracts.model import Contract, Effect
from belay.executor.idempotency import IdempotencyStore
from belay.executor.saga import SagaExecutor
from belay.ledger.store import LedgerStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _contract() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="crm.update",
        reversibility="irreversible",
        idempotency_key="$args.request_id",
        effects=[Effect(type="update", resource="crm.record", count="1")],
    )


class _SpyUpstream:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append(args)
        return {"tool": tool, "echo": args}


async def test_repeating_the_same_idempotency_key_calls_upstream_once() -> None:
    ledger = LedgerStore()
    idempotency = IdempotencyStore()
    saga = SagaExecutor(ledger=ledger, idempotency=idempotency)
    upstream = _SpyUpstream()

    first = await saga.run_step(
        "s1", 1, "crm.update", {"request_id": "r1", "x": 1}, _contract(), upstream
    )
    second = await saga.run_step(
        "s1", 2, "crm.update", {"request_id": "r1", "x": 1}, _contract(), upstream
    )

    assert len(upstream.calls) == 1, "upstream must be called exactly once"
    assert first.result == second.result
