"""Counterfactual replay: "what would have happened if a human had decided
differently at an approval/policy point" (plan-v2 E12).

Computed entirely offline from a real session's ledger events -- this module
never calls a real upstream MCP server and never appends to or mutates the
real session's ledger. `CounterfactualBranch` is a plain, frozen dataclass
holding only a read-only tuple of `Event`s: it has no field capable of
holding a `LedgerStore` (which is the only object with an `.append()`
method, spec §9.1), so a branch is architecturally incapable of writing to
the real ledger, not merely disciplined not to (docs/adr/0012).

Honesty rule (the crux of this entrega, analogous to E7's `fully_rewound`,
spec-equivalent of §10.3 -- see `docs/adr/0007-e7-rewind.md`):

- A step identical to the real session (before the fork point, or after it
  when the override is a no-op) is `unchanged`: it reuses the *actual*
  recorded result from the ledger, because nothing changed.
- A step whose behavior diverges *because of* the override, for which a safe
  read-only estimate is available (the real ledger's own `plan_created`
  event already carries a `native_dry_run`/`sql_simulator`/`dry_run` basis
  effect estimate for that tool call, or the caller's `upstream_replay` hook
  supplies one, or a plain contract-declared effect estimate exists with no
  call required) is `diverged`, tagged with that `Basis` (or the branch's own
  `"simulated"` marker when no estimate stronger than a bare guess is safely
  available).
- A step that diverges with **no** safe way to know what would have
  happened (no plan was ever recorded for it, so this module has no tool
  identity or effect estimate to reason about) is `unknown` -- never
  fabricated as a concrete result.

Reuses `belay.ledger.replay.replay()` for the real session's baseline final
state (does not reimplement the fold) and the existing `Basis` literal from
`belay/planner/model.py` (does not invent a parallel enum).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from typing import Literal as TLiteral

from belay.ledger.model import Event
from belay.ledger.replay import SessionState, replay
from belay.planner.model import Basis

StepOutcome = TLiteral["unchanged", "diverged", "unknown"]

#: `Basis` (spec §5.3, extended by E11) plus one branch-only marker for "no
#: real dry-run/estimate was safely available, this is a bare guess from the
#: contract's declared effects at best". Extends, does not replace, `Basis`.
CounterfactualBasis = Basis | TLiteral["simulated"]

#: Optional hook: given (tool, args) for a step whose behavior diverges from
#: the real session, return a better-than-"simulated" `(basis, detail)`
#: estimate using a real read-only dry-run adapter (native_dry_run /
#: sql_simulator, spec §5.3/E11) -- or `None` to fall back to the safe
#: default. Must never perform anything but a read-only/rolled-back
#: operation; `run_counterfactual` has no way to enforce that on the
#: caller's behalf, same trust boundary as `PlanningSession.native_dry_run`/
#: `.sql_runner` in the forward path.
UpstreamReplay = Callable[[str, dict[str, Any]], "tuple[Basis, dict[str, Any]] | None"]


class InvalidForkPoint(ValueError):
    """`at_step_seq` does not correspond to a real `policy_evaluated` event."""


@dataclass(frozen=True)
class CounterfactualStep:
    """One step's honest classification in the branch (the honesty rule)."""

    step_seq: int
    tool: str
    outcome: StepOutcome
    basis: CounterfactualBasis | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_seq": self.step_seq,
            "tool": self.tool,
            "outcome": self.outcome,
            "basis": self.basis,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CounterfactualBranch:
    """A read-only, in-memory fork of a real session's ledger.

    Deliberately holds nothing but a plain tuple of already-read `Event`s --
    no `LedgerStore`, no upstream connection, no session-mutating handle of
    any kind. This is the architectural half of the immutability guarantee
    (docs/adr/0012): there is no field on this object through which a bug
    could reach the real session's ledger or a real upstream server, so the
    "never mutates the real session" property holds independent of any test.
    """

    session_id: str
    at_step_seq: int
    override: dict[str, Any]
    events: tuple[Event, ...]


@dataclass(frozen=True)
class CounterfactualReport:
    """`run_counterfactual`'s result: per-step classification + final states."""

    session_id: str
    at_step_seq: int
    override: dict[str, Any]
    steps: list[CounterfactualStep]
    real_final_state: SessionState
    counterfactual_final_state: SessionState

    @property
    def unchanged(self) -> list[CounterfactualStep]:
        return [s for s in self.steps if s.outcome == "unchanged"]

    @property
    def diverged(self) -> list[CounterfactualStep]:
        return [s for s in self.steps if s.outcome == "diverged"]

    @property
    def unknown(self) -> list[CounterfactualStep]:
        return [s for s in self.steps if s.outcome == "unknown"]

    @property
    def is_noop(self) -> bool:
        """True iff every step is `unchanged` -- the no-op-override regression anchor."""
        return all(s.outcome == "unchanged" for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "at_step_seq": self.at_step_seq,
            "override": self.override,
            "steps": [s.to_dict() for s in self.steps],
            "real_final_state": self.real_final_state.model_dump(mode="json"),
            "counterfactual_final_state": self.counterfactual_final_state.model_dump(mode="json"),
            "is_noop": self.is_noop,
        }


def _plan_event(events: tuple[Event, ...], step_seq: int) -> Event | None:
    for ev in events:
        if ev.step_seq == step_seq and ev.type == "plan_created":
            return ev
    return None


def _policy_event(events: tuple[Event, ...], step_seq: int) -> Event | None:
    for ev in events:
        if ev.step_seq == step_seq and ev.type == "policy_evaluated":
            return ev
    return None


def _recorded_result(events: tuple[Event, ...], step_seq: int) -> dict[str, Any] | None:
    for ev in events:
        if ev.step_seq == step_seq and ev.type == "result_recorded":
            return dict(ev.payload)
    return None


def _safe_basis(plan_ev: Event) -> tuple[CounterfactualBasis, dict[str, Any]] | None:
    """A basis already safely available in the real ledger's own recorded plan.

    `native_dry_run`/`sql_simulator`/`dry_run` are real, read-only-derived
    estimates already computed once (E4/E11) -- reusing the recorded value,
    never a fresh call. A plain `contract`-basis effect estimate requires no
    call at all (it's a static declaration), so it's also safe to reuse as a
    bare guess, tagged `"simulated"` rather than the (stronger) `contract`
    basis literal, since it's now describing a hypothetical call that never
    actually happened.
    """
    effects = plan_ev.payload.get("effects") or []
    if not effects:
        return None
    strong = {"native_dry_run", "sql_simulator", "dry_run"}
    for effect in effects:
        basis = effect.get("basis")
        if basis in strong:
            return basis, {"effects": effects}
    return "simulated", {"effects": effects}


def run_counterfactual(
    events: list[Event],
    at_step_seq: int,
    override: dict[str, Any],
    *,
    upstream_replay: UpstreamReplay | None = None,
) -> CounterfactualReport:
    """Fork `events` at `at_step_seq`, substituting `override` for the real verdict.

    `events` is a plain, already-read list (spec §9.1, via `LedgerStore.read`)
    -- this function never touches a `LedgerStore` itself and performs no I/O.
    """
    if not events:
        raise InvalidForkPoint("session has no events")

    branch = CounterfactualBranch(
        session_id=events[0].session_id,
        at_step_seq=at_step_seq,
        override=dict(override),
        events=tuple(events),
    )

    fork_event = _policy_event(branch.events, at_step_seq)
    if fork_event is None:
        raise InvalidForkPoint(
            f"step_seq {at_step_seq} does not correspond to a policy_evaluated event "
            f"in session {branch.session_id}"
        )

    real_verdict = fork_event.payload.get("verdict")
    real_args = None
    fork_plan = _plan_event(branch.events, at_step_seq)
    if fork_plan is not None:
        real_args = fork_plan.payload.get("args")

    override_verdict = branch.override.get("verdict", real_verdict)
    override_args = branch.override.get("args", real_args)
    is_noop = override_verdict == real_verdict and override_args == real_args

    step_seqs = sorted({ev.step_seq for ev in branch.events if ev.step_seq is not None})
    steps: list[CounterfactualStep] = []
    for step_seq in step_seqs:
        plan_ev = _plan_event(branch.events, step_seq)
        tool = str(plan_ev.payload.get("tool")) if plan_ev is not None else "?"

        if step_seq < at_step_seq or is_noop:
            recorded = _recorded_result(branch.events, step_seq)
            steps.append(
                CounterfactualStep(
                    step_seq, tool, "unchanged", detail={"result": recorded} if recorded else {}
                )
            )
            continue

        # step_seq >= at_step_seq and the override genuinely diverges from
        # reality: never reuse the real recorded result past this point.
        if plan_ev is None:
            # No plan was ever recorded for this step: no tool identity, no
            # effect estimate -- there is nothing safe to reason about.
            steps.append(CounterfactualStep(step_seq, tool, "unknown"))
            continue

        args = dict(plan_ev.payload.get("args") or {})
        replay_result = upstream_replay(tool, args) if upstream_replay is not None else None
        if replay_result is not None:
            basis, detail = replay_result
            steps.append(CounterfactualStep(step_seq, tool, "diverged", basis=basis, detail=detail))
            continue

        safe = _safe_basis(plan_ev)
        if safe is None:
            steps.append(CounterfactualStep(step_seq, tool, "unknown"))
            continue
        safe_basis, safe_detail = safe
        steps.append(
            CounterfactualStep(step_seq, tool, "diverged", basis=safe_basis, detail=safe_detail)
        )

    real_final_state = replay(list(branch.events))

    if is_noop:
        counterfactual_final_state = real_final_state
    else:
        prefix = [ev for ev in branch.events if ev.step_seq is None or ev.step_seq < at_step_seq]
        synthetic = Event(
            event_id="counterfactual-fork",
            session_id=branch.session_id,
            step_seq=at_step_seq,
            type="policy_evaluated",
            at=datetime.now(UTC).isoformat(),
            payload={"verdict": override_verdict, "reasons": ["counterfactual_override"]},
            prev_hash="0" * 64,
            hash="0" * 64,
        )
        counterfactual_final_state = replay([*prefix, synthetic])

    return CounterfactualReport(
        session_id=branch.session_id,
        at_step_seq=at_step_seq,
        override=branch.override,
        steps=steps,
        real_final_state=real_final_state,
        counterfactual_final_state=counterfactual_final_state,
    )
