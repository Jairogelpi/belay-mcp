"""Recovery of a step journaled but not resolved when the process is killed
between `calling` and `result_recorded` (spec §8.1 paragraph 2)."""

from __future__ import annotations

import pytest
from belay.executor.idempotency import IdempotencyStore
from belay.executor.recovery import recover_session
from belay.ledger.store import LedgerStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _simulate_crash_after_calling(
    ledger: LedgerStore, session_id: str, step_seq: int, tool: str, args: dict, key: str | None
) -> None:
    """Append exactly the events a real crash between stage 3 and 4 leaves behind."""
    ledger.append(session_id, "step_journaled", {"tool": tool, "args": args}, step_seq=step_seq)
    ledger.append(
        session_id,
        "tool_called",
        {"tool": tool, "args": args, "idempotency_key": key},
        step_seq=step_seq,
    )
    # No result_recorded -- the process "died" right here.


async def test_recovery_reconciles_via_idempotency_key() -> None:
    """@spec("8.1") — recovery MUST reconcile via idempotency key or mark step indeterminate."""
    ledger = LedgerStore()
    idempotency = IdempotencyStore()
    idempotency.begin("k1", "s1", 1)
    _simulate_crash_after_calling(ledger, "s1", 1, "crm.update", {"id": "1"}, "k1")

    async def reconcile(tool: str, args: dict, key: str) -> dict:
        assert key == "k1"
        return {"tool": tool, "reconciled": True}

    outcomes = await recover_session(ledger, idempotency, "s1", reconcile=reconcile)

    assert outcomes[0].status == "reconciled"
    events = ledger.read("s1")
    assert [e.type for e in events][-1] == "result_recorded"
    assert events[-1].payload["recovered"] is True
    assert idempotency.get("k1").status == "done"  # type: ignore[union-attr]


async def test_recovery_without_idempotency_key_is_indeterminate() -> None:
    ledger = LedgerStore()
    idempotency = IdempotencyStore()
    _simulate_crash_after_calling(ledger, "s1", 1, "crm.update", {"id": "1"}, None)

    async def reconcile(tool: str, args: dict, key: str) -> dict:
        raise AssertionError("must not be called without an idempotency key")

    outcomes = await recover_session(ledger, idempotency, "s1", reconcile=reconcile)

    assert outcomes[0].status == "indeterminate"
    events = ledger.read("s1")
    assert [e.type for e in events][-1] == "step_indeterminate"
