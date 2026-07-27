"""Deterministic triage scoring for the approval queue (adoption/DX, not spec-numbered).

Belay's safety path never lets an LLM (or anything else) decide/verdict a
plan (spec §6, §7) -- that stays exactly as strict as before. Triage doesn't
touch that: it's a pure, deterministic *sort/label* over items already in
the human's queue, from data the plan/policy stages already computed
(`reversibility`, `confidence`, `policy_reasons`, `unknown`). It never
approves or rejects anything -- `belay approvals approve/reject` is the only
way an item's state changes (§12: no-self-approval, CLI-only).

The point is queue fatigue, not automation: when there are fifty pending
items, "the ten irreversible/low-confidence ones need your eyes first" is a
real time-save that doesn't weaken the approval gate itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Literal as TLiteral

from belay.approvals.queue import ApprovalItem

Risk = TLiteral["low", "medium", "high"]

_REVERSIBILITY_WEIGHT: dict[str, int] = {"reversible": 0, "conditional": 1, "irreversible": 2}
_CONFIDENCE_WEIGHT: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class Triage:
    approval_id: str
    risk: Risk
    score: int
    reasons: list[str]


def _score(plan: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    reversibility = plan.get("reversibility", "irreversible")
    w = _REVERSIBILITY_WEIGHT.get(reversibility, 2)
    score += w * 2
    if w:
        reasons.append(f"{reversibility}")

    confidence = plan.get("confidence", "low")
    w = _CONFIDENCE_WEIGHT.get(confidence, 2)
    score += w
    if w:
        reasons.append(f"{confidence}-confidence estimate")

    unknown = plan.get("unknown") or []
    if unknown:
        score += 2 * len(unknown)
        reasons.append(f"{len(unknown)} unknown effect(s)")

    fired = plan.get("policy_reasons") or []
    if len(fired) > 1:
        score += len(fired) - 1
        reasons.append(f"{len(fired)} policy dimensions fired")

    return score, reasons


def triage(item: ApprovalItem) -> Triage:
    """Score one pending item. Higher score = needs a human's attention sooner."""
    score, reasons = _score(item.plan)
    if score >= 5:
        risk: Risk = "high"
    elif score >= 2:
        risk = "medium"
    else:
        risk = "low"
    if not reasons:
        reasons = ["reversible, high-confidence, single dimension"]
    return Triage(item.approval_id, risk, score, reasons)


def triage_queue(items: list[ApprovalItem]) -> list[tuple[ApprovalItem, Triage]]:
    """Every item paired with its `Triage`, sorted highest-risk first (ties: oldest first)."""
    scored = [(item, triage(item)) for item in items]
    return sorted(scored, key=lambda pair: -pair[1].score)
