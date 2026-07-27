"""belay/approvals/triage.py: deterministic risk-sort of the pending queue."""

from __future__ import annotations

from belay.approvals.queue import ApprovalQueue
from belay.approvals.triage import triage, triage_queue


def _item(queue: ApprovalQueue, plan_id: str, plan: dict):
    return queue.request("s_test", plan_id, plan)


def test_low_risk_plan_gets_low_score() -> None:
    queue = ApprovalQueue()
    item = _item(
        queue,
        "p_1",
        {"tool": "fs.write_file", "reversibility": "reversible", "confidence": "high"},
    )
    result = triage(item)
    assert result.risk == "low"


def test_irreversible_low_confidence_gets_high_score() -> None:
    queue = ApprovalQueue()
    item = _item(
        queue,
        "p_2",
        {
            "tool": "crm.bulk_delete",
            "reversibility": "irreversible",
            "confidence": "low",
            "unknown": [{"reason": "no estimate"}],
            "policy_reasons": ["caps[0]", "defaults.unknown_effects"],
        },
    )
    result = triage(item)
    assert result.risk == "high"
    assert "irreversible" in result.reasons
    assert "1 unknown effect(s)" in result.reasons


def test_triage_queue_sorts_highest_risk_first() -> None:
    queue = ApprovalQueue()
    low = _item(queue, "p_low", {"tool": "a", "reversibility": "reversible", "confidence": "high"})
    high = _item(
        queue,
        "p_high",
        {"tool": "b", "reversibility": "irreversible", "confidence": "low", "unknown": [{}]},
    )
    ranked = triage_queue(queue.list())
    assert ranked[0][0].approval_id == high.approval_id
    assert ranked[-1][0].approval_id == low.approval_id
