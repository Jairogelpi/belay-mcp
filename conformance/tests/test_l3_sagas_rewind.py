"""L3 -- sagas & rewind (spec §8, §10), full ledger verification (spec §9.2).

@conformance(level=3)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from belay.contracts.loader import load_contract_set
from belay.executor.saga import SagaStep
from belay.ledger.verify import verify_chain, verify_coherence

from conformance.target import ConformanceTarget
from conformance.tests.fakes import make_crm_executor

pytestmark = [pytest.mark.anyio, pytest.mark.l3]

REPO_ROOT = Path(__file__).resolve().parents[2]
CRM_CONTRACT = REPO_ROOT / "examples" / "contracts" / "crm.yaml"


def _steps() -> list[SagaStep]:
    contracts = load_contract_set([CRM_CONTRACT])
    update = contracts.resolve("crm.update")
    assert update is not None
    return [
        SagaStep("crm.update", {"id": "a", "fields": {"stage": "qualified"}}, update),
        SagaStep("crm.update", {"id": "a", "fields": {"stage": "won"}}, update),
    ]


async def test_saga_commits_every_step_in_order(target: ConformanceTarget) -> None:
    executor, snapshot = make_crm_executor()
    session_id = target.new_session([CRM_CONTRACT], executor)
    await executor("crm.create", {"id": "a", "fields": {"stage": "lead"}})

    report = await target.run_saga(session_id, _steps())

    assert report.failed is None
    assert len(report.committed) == 2
    assert snapshot()["a"]["stage"] == "won"


async def test_rewind_restores_the_saga_to_its_initial_state(
    target: ConformanceTarget,
) -> None:
    """Materialized-compensation (spec §8.1.5): rewind replays what was captured at
    commit time, never re-evaluating the undo expression against later state."""
    executor, snapshot = make_crm_executor()
    session_id = target.new_session([CRM_CONTRACT], executor)
    await executor("crm.create", {"id": "a", "fields": {"stage": "lead"}})
    initial = snapshot()

    report = await target.run_saga(session_id, _steps())
    assert report.failed is None
    assert snapshot() != initial

    rewind_report = await target.rewind(session_id, by="conformance-operator")

    assert rewind_report.fully_rewound
    assert snapshot() == initial


async def test_rewind_dry_run_never_touches_state(target: ConformanceTarget) -> None:
    executor, snapshot = make_crm_executor()
    session_id = target.new_session([CRM_CONTRACT], executor)
    await executor("crm.create", {"id": "a", "fields": {"stage": "lead"}})
    await target.run_saga(session_id, _steps())
    before = snapshot()

    rewind_report = await target.rewind(session_id, dry_run=True)

    assert rewind_report.dry_run
    assert snapshot() == before


async def test_ledger_chain_and_coherence_verify_after_saga_and_rewind(
    target: ConformanceTarget,
) -> None:
    """spec §9.2: chain verification holds end-to-end, not just for the happy path."""
    executor, _snapshot = make_crm_executor()
    session_id = target.new_session([CRM_CONTRACT], executor)
    await executor("crm.create", {"id": "a", "fields": {"stage": "lead"}})
    await target.run_saga(session_id, _steps())
    await target.rewind(session_id, by="conformance-operator")

    events = target.ledger(session_id)
    chain_report = verify_chain(events)
    coherence_report = verify_coherence(events)
    assert chain_report.ok, chain_report.errors
    assert coherence_report.ok, coherence_report.errors
