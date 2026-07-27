"""`belay rewind --intent/--keep`: selective rewind by declared subgoal (adoption/DX,
not spec-numbered -- builds on the real spec §10 `RewindService`, doesn't replace it).

An agent tags each tool call with a `_belay_intent` label (see
`belay/proxy/server.py`), recorded on that step's `plan_created` ledger
event (`extra="allow"`, spec §14 -- doesn't touch the normative `Plan`
schema). This module only answers one question honestly: *is undoing
exactly the steps tagged `intent`, and nothing else, actually safe with the
strict-reverse-order compensation `RewindService` already implements* --
it never attempts a semantic merge of interleaved subgoals.

The one case this supports: every step tagged `intent` is a contiguous
trailing run -- nothing after the last `keep`-tagged step belongs to any
other subgoal. In that case, "undo `intent`, keep `keep`" is exactly
`RewindService.rewind(..., to_step=<last keep step_seq>)`, already built
and tested (spec §10). Anything else -- an untagged step, a third subgoal,
or `intent` steps interleaved with `keep` steps -- is refused outright
(`IntentRewindError("rewind_intent_not_suffix")`) rather than guessed at: no
semantic merge is attempted, matching the existing rewind honesty taxonomy
(`docs/adr` Verified Rewind) of `exact`/`compensated`/`partial`/`impossible`
over fabricating a result.
"""

from __future__ import annotations

from belay.ledger.model import Event


class IntentRewindError(Exception):
    """Adoption/DX error for `belay rewind --intent` -- not part of spec §11's closed
    `BelayError`/`ERROR_CODES` registry (that's the governed MCP tool-call error
    surface; this is a CLI-only, non-spec convenience on top of it)."""

    def __init__(self, code: str, detail: dict[str, object]) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "detail": self.detail}


def _intent_by_step(events: list[Event]) -> dict[int, str | None]:
    by_step: dict[int, str | None] = {}
    for event in events:
        if event.type == "plan_created" and event.step_seq is not None:
            by_step[event.step_seq] = event.payload.get("intent_id")
    return by_step


def resolve_intent_to_step(events: list[Event], intent: str, keep: str | None) -> int:
    """Return the `to_step` cutoff that undoes exactly `intent`'s steps, keeping `keep`'s.

    Raises `IntentRewindError("intent_not_found")` if no step is tagged
    `intent`, or `IntentRewindError("rewind_intent_not_suffix")` if the
    tagged steps aren't a safe contiguous trailing run.
    """
    by_step = _intent_by_step(events)
    if not by_step:
        raise IntentRewindError(
            "intent_not_found", {"intent": intent, "reason": "no tagged steps at all"}
        )

    target_steps = {s for s, i in by_step.items() if i == intent}
    if not target_steps:
        raise IntentRewindError("intent_not_found", {"intent": intent})

    keep_steps = {s for s, i in by_step.items() if keep and i == keep}
    cutoff = max(keep_steps) if keep_steps else 0

    if keep_steps and max(keep_steps) > min(target_steps):
        raise IntentRewindError(
            "rewind_intent_not_suffix",
            {
                "intent": intent,
                "keep": keep,
                "reason": "a 'keep' step comes after a step tagged for removal -- "
                "interleaved, not a safe contiguous rewind",
            },
        )

    steps_after_cutoff = {s for s in by_step if s > cutoff}
    if steps_after_cutoff != target_steps:
        extra = steps_after_cutoff - target_steps
        raise IntentRewindError(
            "rewind_intent_not_suffix",
            {
                "intent": intent,
                "keep": keep,
                "reason": "step(s) after the last kept step belong to neither "
                "'intent' nor 'keep' -- refusing rather than guessing what to do with them",
                "unaccounted_steps": sorted(extra),
            },
        )

    return cutoff
