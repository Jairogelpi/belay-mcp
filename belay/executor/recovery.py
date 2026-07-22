"""Startup recovery of journaled-but-unresolved steps (spec §8.1 paragraph 2).

"A crash between 3 and 4 leaves a journaled-but-unresolved step; on recovery
Belay MUST reconcile via the idempotency key (re-issue and deduplicate) or,
if impossible, mark the step `indeterminate` -- a first-class state that
rewind reports honestly."

`step_indeterminate` is not a crash and not a silent skip: it is appended to
the ledger like any other event, and an operator resolves it out-of-band
(inspecting the upstream directly, or, once E7 lands, choosing to treat it as
irreversible for rewind purposes). This module only detects the situation and
records it -- it never guesses.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from belay.executor.idempotency import IdempotencyStore
from belay.ledger.store import LedgerStore

#: Re-issue `(tool, args, idempotency_key)` against the upstream and return its
#: result. A real upstream is expected to dedupe by `idempotency_key` and hand
#: back the original result rather than repeating the effect.
Reconciler = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class RecoveryOutcome:
    step_seq: int
    status: str  # "reconciled" | "indeterminate"
    result: dict[str, Any] | None = None


def _unresolved_steps(events: list[Any]) -> dict[int, dict[str, Any]]:
    """Steps with a `tool_called` but no `result_recorded` (spec §8.1's crash window)."""
    types_by_step: dict[int, set[str]] = {}
    tool_called: dict[int, dict[str, Any]] = {}
    for ev in events:
        if ev.step_seq is None:
            continue
        types_by_step.setdefault(ev.step_seq, set()).add(ev.type)
        if ev.type == "tool_called":
            tool_called[ev.step_seq] = ev.payload
    return {
        step_seq: tool_called[step_seq]
        for step_seq, types in types_by_step.items()
        if "tool_called" in types and "result_recorded" not in types
    }


async def recover_session(
    ledger: LedgerStore,
    idempotency: IdempotencyStore,
    session_id: str,
    *,
    reconcile: Reconciler,
) -> list[RecoveryOutcome]:
    """Reconcile every unresolved step of `session_id` found in the ledger.

    For each step with `tool_called` but no `result_recorded`: if the step
    declared an `idempotency_key`, re-issue the call through `reconcile` and
    append the recovered `result_recorded`; otherwise append `step_indeterminate`.
    """
    events = ledger.read(session_id)
    outcomes: list[RecoveryOutcome] = []
    for step_seq, payload in sorted(_unresolved_steps(events).items()):
        tool = str(payload.get("tool"))
        args = payload.get("args") or {}
        key = payload.get("idempotency_key")
        if key:
            result = await reconcile(tool, args, key)
            idempotency.complete(key, result)
            ledger.append(
                session_id,
                "result_recorded",
                {"tool": tool, "result": result, "recovered": True},
                step_seq=step_seq,
            )
            outcomes.append(RecoveryOutcome(step_seq, "reconciled", result))
        else:
            ledger.append(
                session_id,
                "step_indeterminate",
                {
                    "tool": tool,
                    "reason": "no idempotency_key declared; cannot safely reconcile with upstream",
                },
                step_seq=step_seq,
            )
            outcomes.append(RecoveryOutcome(step_seq, "indeterminate"))
    return outcomes
