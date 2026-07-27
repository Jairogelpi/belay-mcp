"""Deterministic pre-execution check of a call against an `IntentContract`.

Runs before `resolve`/plan/policy (in `Lifecycle.govern_and_execute`) --
a violation here means the call never reaches the upstream at all, not
even as a dry-run estimate. No LLM, no heuristic guess: `fnmatch` against
`path`, an exact tool-name set membership check, and an integer count.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

from belay.intent.model import IntentContract


@dataclass(frozen=True)
class IntentViolation:
    reason: str
    detail: dict[str, Any]


def check_intent_contract(
    contract: IntentContract,
    tool: str,
    args: dict[str, Any],
    files_touched_so_far: frozenset[str],
) -> IntentViolation | None:
    """Return the violation blocking this call under `contract`, or `None` if it's clear."""
    if tool in contract.forbidden_tools:
        return IntentViolation(
            "forbidden_tool", {"tool": tool, "forbidden_tools": contract.forbidden_tools}
        )

    path = args.get("path")
    if isinstance(path, str):
        for pattern in contract.forbidden_scope:
            if fnmatch(path, pattern):
                return IntentViolation("forbidden_scope", {"path": path, "pattern": pattern})
        if contract.allowed_scope and not any(
            fnmatch(path, pattern) for pattern in contract.allowed_scope
        ):
            return IntentViolation(
                "out_of_scope", {"path": path, "allowed_scope": contract.allowed_scope}
            )

        cap = contract.budgets.files_changed
        if cap is not None:
            projected = files_touched_so_far | {path}
            if len(projected) > cap:
                return IntentViolation(
                    "budget_exceeded",
                    {
                        "budget": "files_changed",
                        "cap": cap,
                        "files_touched": sorted(projected),
                    },
                )

    return None
