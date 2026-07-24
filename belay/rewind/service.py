"""Rewind plan, execution, and honest reporting (spec §10).

Normative shape implemented here:

- §10.1 request: `rewind(session_id, to_step?, dry_run?, by)`. Scope is every
  *committed* step with `step_seq > to_step` (default: all), strict reverse
  order. `dry_run: true` returns the rewind plan -- an honest enumeration of
  what would happen -- without executing or appending anything.
- Fencing: a live session is fenced (closed to new steps) before a real
  (non-dry-run) rewind begins. `belay.proxy.lifecycle.Lifecycle` checks
  `is_fenced()` at the top of every `govern_and_execute` call and raises
  `session_fenced` if the session has a `session_fenced` event -- fencing is
  a ledger fact, not in-process state, so it holds even across processes
  (the CLI's `belay rewind` runs separately from `belay run`).
- §10.2 execution: compensations run in strict reverse `step_seq` order.
  Each is its own mini-step: journaled via `compensation_executed`/
  `compensation_failed`, exactly like `SagaExecutor.compensate` (E6) --
  this module now owns that logic and `SagaExecutor.compensate` delegates
  to it, so there is exactly one compensation code path.
- Compensations pass through the policy engine (spec §12): a `pause`
  verdict parks the compensation in the approval queue just like a forward
  action; a `deny` verdict halts it. `halt_on_failure` is the default;
  `skip_and_continue` is an explicit opt-in, itself recorded in the ledger.
- §10.3 honesty: `RewindReport.fully_rewound` is true only if every in-scope
  step is `reversible` *and* was compensated with passing verification.
  Irreversible/conditional-unmet/indeterminate steps in scope always make it
  false -- there is no code path that reports "fully rewound" while any of
  those remain.
- Verified Rewind (plan-v2): sending an inverse call is not the same claim as
  proving the system is back the way it was -- `RewindReport.verified_result`
  distinguishes the two honestly instead of collapsing them into one boolean:

    * `restored`    -- re-querying the real system after compensation matches
                       the state *captured before the original action ran*
                       (contract's `capture` snapshot), byte for byte.
    * `compensated` -- the compensation ran and its declared `verification`
                       passed, but the post-state isn't identical to the
                       pre-action snapshot (or no `capture` was declared to
                       compare against) -- the business effect is neutralized,
                       not necessarily bit-identical.
    * `partial`     -- some in-scope steps reached `restored`/`compensated`,
                       others didn't (irreversible, verification failed,
                       compensation failed, denied, or never reached because
                       the rewind halted).
    * `impossible`  -- no in-scope step reached `restored`/`compensated` --
                       e.g. every in-scope step was contract-irreversible.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from belay.approvals.queue import ApprovalQueue
from belay.canonical import canonical_bytes, sha256_hex
from belay.contracts.expressions import evaluate, parse
from belay.contracts.model import Contract, ContractSet
from belay.ledger.model import Event
from belay.ledger.store import LedgerStore
from belay.planner.model import EffectEstimate, Plan
from belay.policy.engine import PolicyEngine
from belay.policy.model import PolicyDoc, default_policy

Executor = Callable[[str, dict[str, Any]], Awaitable[Any]]

StepStatus = Literal["reversible", "irreversible", "conditional_unmet", "indeterminate"]
OutcomeStatus = Literal[
    "compensated", "verification_failed", "compensation_failed", "skipped", "paused", "denied"
]
#: Verified Rewind's honest 4-value taxonomy (see module docstring below):
#: whether the undo call ran is bookkeeping (`OutcomeStatus`); this is the
#: claim actually being made about real-world state.
RewindResult = Literal["restored", "compensated", "partial", "impossible"]

_PLAN_TTL_SECONDS = 600  # mirrors belay.planner.planner's default (spec §5.4)


def _as_dict(value: Any) -> dict[str, Any]:
    """Mirrors `belay.executor.saga._as_dict`: unwrap a real `CallToolResult`'s
    `structuredContent` before falling back to a bare model/dict coercion."""
    structured = getattr(value, "structuredContent", None)
    if isinstance(structured, dict):
        nested = structured.get("result", structured)
        return dict(nested) if isinstance(nested, dict) else {"value": nested}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    return {"value": value}


def _materialize(value: Any, scope: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return evaluate(parse(value), scope)
    if isinstance(value, dict):
        return {k: _materialize(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [_materialize(v, scope) for v in value]
    return value


def _check_verification(expect: Any, result: Any) -> bool:
    """Interpret a contract's `verification.expect` against the tool's result.

    # ponytail: only `"not_found"` and dict-subset-equality are understood;
    # v0.1 has no example needing anything richer. Extend when a contract
    # declares a verification shape this doesn't cover.
    """
    if expect == "not_found":
        if isinstance(result, dict):
            if "existed" in result:
                return result["existed"] is False
            return not result
        return not result
    if isinstance(expect, dict):
        return isinstance(result, dict) and all(result.get(k) == v for k, v in expect.items())
    return bool(result == expect)


@dataclass(frozen=True)
class RewindStepPlan:
    """One committed step's honest classification for the rewind plan (spec §10.1/§10.3)."""

    step_seq: int
    tool: str
    status: StepStatus
    reason: str | None = None
    compensation: dict[str, Any] | None = None  # {"tool", "args"} when status == "reversible"
    verification: dict[str, Any] | None = None  # {"tool", "args", "expect"} when declared
    # The contract's `capture` snapshot taken before the *original* action ran
    # -- the ground truth `restored` compares the post-compensation re-query
    # against. `None` when the contract declared no `capture` block.
    pre_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class RewindPlan:
    """The honest rewind plan (spec §10.1): ordered compensations plus what will remain."""

    session_id: str
    to_step: int | None
    steps: list[RewindStepPlan]  # already in strict reverse step_seq order

    @property
    def reversible(self) -> list[RewindStepPlan]:
        return [s for s in self.steps if s.status == "reversible"]

    @property
    def irreversible(self) -> list[RewindStepPlan]:
        return [s for s in self.steps if s.status == "irreversible"]

    @property
    def conditional_unmet(self) -> list[RewindStepPlan]:
        return [s for s in self.steps if s.status == "conditional_unmet"]

    @property
    def indeterminate(self) -> list[RewindStepPlan]:
        return [s for s in self.steps if s.status == "indeterminate"]


@dataclass(frozen=True)
class CompensationOutcome:
    """What actually happened to one in-scope step during a real rewind."""

    step_seq: int
    tool: str
    status: OutcomeStatus
    detail: dict[str, Any] = field(default_factory=dict)
    #: Verified Rewind's per-step honest claim. Only ever `restored` or
    #: `compensated` -- `partial`/`impossible` are session-level aggregates
    #: computed by `RewindReport.verified_result`, never a single step's own
    #: claim. `None` for outcomes that never got this far (`paused`, e.g.).
    result: Literal["restored", "compensated"] | None = None


@dataclass(frozen=True)
class RewindReport:
    """`RewindService.rewind()`'s result: the plan, what happened, and the honest verdict."""

    session_id: str
    dry_run: bool
    plan: RewindPlan
    outcomes: list[CompensationOutcome] = field(default_factory=list)
    halted: bool = False

    @property
    def fully_rewound(self) -> bool:
        """Spec §10.3: never true while any in-scope step wasn't compensated+verified."""
        if self.dry_run:
            return False
        if any(s.status != "reversible" for s in self.plan.steps):
            return False
        ok = {o.step_seq for o in self.outcomes if o.status == "compensated"}
        return all(s.step_seq in ok for s in self.plan.steps)

    @property
    def verified_result(self) -> RewindResult | None:
        """Verified Rewind's honest session-level claim (see module docstring).

        `None` for a `dry_run` -- nothing executed, so there is no claim to make
        about real-world state yet, only a plan.
        """
        if self.dry_run:
            return None
        if not self.plan.steps:
            return "restored"  # nothing was in scope: vacuously fully restored

        succeeded_by_step = {o.step_seq: o.result for o in self.outcomes if o.result is not None}
        n_succeeded = len(succeeded_by_step)
        n_total = len(self.plan.steps)

        if n_succeeded == 0:
            return "impossible"
        if n_succeeded < n_total:
            return "partial"
        if all(r == "restored" for r in succeeded_by_step.values()):
            return "restored"
        return "compensated"


async def compensate_one(
    ledger: LedgerStore,
    session_id: str,
    step_seq: int,
    comp: dict[str, Any],
    executor: Executor,
) -> dict[str, Any] | None:
    """Execute one materialized compensation as its own mini-step (spec §10.2).

    The single place that appends `compensation_executed`/`compensation_failed`
    -- shared by `RewindService.rewind()` and `belay.executor.saga.SagaExecutor
    .compensate` (E6's auto-unwind), so there is exactly one compensation
    code path rather than two copies of the same ledger bookkeeping.
    """
    if not comp.get("reversible"):
        return None
    try:
        result = await executor(comp["tool"], comp["args"])
    except Exception as exc:
        ledger.append(
            session_id,
            "compensation_failed",
            {"step_seq": step_seq, "tool": comp["tool"], "error": str(exc)},
            step_seq=step_seq,
        )
        raise
    ledger.append(
        session_id,
        "compensation_executed",
        {
            "step_seq": step_seq,
            "tool": comp["tool"],
            "args": comp["args"],
            "result": _as_dict(result),
        },
        step_seq=step_seq,
    )
    return _as_dict(result)


def is_fenced(ledger: LedgerStore, session_id: str) -> bool:
    """Whether `session_id` has been fenced (spec §10.1: no new steps once fenced)."""
    return any(e.type == "session_fenced" for e in ledger.read(session_id))


@dataclass
class RewindService:
    """`RewindService.rewind(session_id, ...) -> RewindReport` (spec §10)."""

    ledger: LedgerStore
    policy: PolicyDoc = field(default_factory=default_policy)
    policy_engine: PolicyEngine = field(default_factory=PolicyEngine)
    approvals: ApprovalQueue | None = None
    contract_set: ContractSet | None = None

    def __post_init__(self) -> None:
        if self.approvals is None:
            # Share the ledger's SQLite file, like `ApprovalStage` does (spec §7):
            # `belay approvals approve` runs as a separate CLI process.
            self.approvals = ApprovalQueue(engine=self.ledger.engine)

    def fence(self, session_id: str) -> None:
        """Close `session_id` to new steps (spec §10.1). Idempotent."""
        if not is_fenced(self.ledger, session_id):
            self.ledger.append(session_id, "session_fenced", {})

    def build_plan(self, session_id: str, to_step: int | None = None) -> RewindPlan:
        """The honest rewind plan (spec §10.1): scope, order, and classification."""
        events = self.ledger.read(session_id)
        by_step: dict[int, dict[str, list[Event]]] = {}
        for ev in events:
            if ev.step_seq is None:
                continue
            by_step.setdefault(ev.step_seq, {}).setdefault(ev.type, []).append(ev)

        steps: list[RewindStepPlan] = []
        for step_seq in sorted(by_step, reverse=True):
            if to_step is not None and step_seq <= to_step:
                continue
            types = by_step[step_seq]
            committed = "step_committed" in types
            indeterminate = "step_indeterminate" in types
            already_done = "compensation_executed" in types or "compensation_failed" in types
            if not committed and not indeterminate:
                continue  # never reached a reportable state (e.g. plain step_failed)
            if already_done:
                continue  # a previous rewind already resolved this step

            journaled = types.get("step_journaled", [])
            tool = str(journaled[0].payload.get("tool")) if journaled else "?"

            if indeterminate and not committed:
                steps.append(RewindStepPlan(step_seq, tool, "indeterminate"))
                continue

            comp_events = types.get("compensation_registered", [])
            comp_payload = comp_events[0].payload if comp_events else {"reversible": False}
            if not comp_payload.get("reversible"):
                reason = str(comp_payload.get("reason", "irreversible"))
                status: StepStatus = (
                    "conditional_unmet" if reason == "conditional_unmet" else "irreversible"
                )
                steps.append(RewindStepPlan(step_seq, tool, status, reason=reason))
                continue

            verification = self._verification_for(tool, step_seq, types)
            captures = types.get("state_captured", [])
            pre_snapshot = (
                captures[0].payload.get("snapshot") if captures else None
            )
            steps.append(
                RewindStepPlan(
                    step_seq,
                    tool,
                    "reversible",
                    compensation={
                        "reversible": True,
                        "tool": comp_payload["tool"],
                        "args": comp_payload["args"],
                    },
                    verification=verification,
                    pre_snapshot=pre_snapshot if isinstance(pre_snapshot, dict) else None,
                )
            )
        return RewindPlan(session_id=session_id, to_step=to_step, steps=steps)

    def _verification_for(
        self, tool: str, step_seq: int, types: dict[str, list[Event]]
    ) -> dict[str, Any] | None:
        if self.contract_set is None:
            return None
        contract = self.contract_set.resolve(tool)
        if contract is None or contract.verification is None:
            return None

        journaled = types.get("step_journaled", [])
        results = types.get("result_recorded", [])
        captures = types.get("state_captured", [])
        scope: dict[str, Any] = {
            "args": journaled[0].payload.get("args", {}) if journaled else {},
            "result": results[0].payload.get("result", {}) if results else {},
            "context": {"step_seq": step_seq},
            "state": {},
        }
        for cap_ev in captures:
            scope["state"][cap_ev.payload.get("as", "")] = cap_ev.payload.get("snapshot", {})

        raw = contract.verification
        return {
            "tool": raw["tool"],
            "args": _materialize(raw.get("args", {}), scope),
            "expect": raw.get("expect"),
        }

    def _compensation_plan(
        self, session_id: str, step_seq: int, comp_tool: str, comp_args: dict[str, Any]
    ) -> Plan:
        effects: list[EffectEstimate] = []
        reversibility: Literal["reversible", "irreversible", "conditional"] = "reversible"
        if self.contract_set is not None:
            contract: Contract | None = self.contract_set.resolve(comp_tool)
            if contract is not None:
                for e in contract.effects:
                    effects.append(
                        EffectEstimate(
                            type=e.type,
                            resource=e.resource,
                            count=e.count,
                            estimate=e.count is not None,
                            basis="contract",
                            amount=e.amount,
                            recipients=e.recipients,
                        )
                    )
        if not effects:
            effects = [EffectEstimate(type="execute", resource=comp_tool, count="1")]

        now = datetime.now(UTC)
        # Deterministic over (session_id, step_seq, tool, args) -- like
        # `belay.planner.planner._plan_id` -- so an approval granted for one
        # rewind attempt's compensation plan is still found by the next
        # attempt at the same step (spec §12 approver binding applies to
        # compensations too).
        digest_input = {
            "session_id": session_id,
            "step_seq": step_seq,
            "tool": comp_tool,
            "args": comp_args,
        }
        digest = sha256_hex(canonical_bytes(digest_input))
        return Plan(
            plan_id=f"rewind_{digest[:16]}",
            session_id=session_id,
            tool=comp_tool,
            args=comp_args,
            effects=effects,
            reversibility=reversibility,
            confidence="medium",
            unknown=[],
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=_PLAN_TTL_SECONDS)).isoformat(),
        )

    async def rewind(
        self,
        session_id: str,
        executor: Executor,
        *,
        to_step: int | None = None,
        dry_run: bool = False,
        by: str,
        halt_on_failure: bool = True,
        skip_and_continue: bool = False,
    ) -> RewindReport:
        """Rewind `session_id` (spec §10). `dry_run` never touches the ledger or upstream."""
        plan = self.build_plan(session_id, to_step)

        if dry_run:
            return RewindReport(session_id=session_id, dry_run=True, plan=plan)

        self.fence(session_id)
        self.ledger.append(
            session_id,
            "rewind_requested",
            {
                "by": by,
                "to_step": to_step,
                "halt_on_failure": halt_on_failure,
                "skip_and_continue": skip_and_continue,
            },
        )
        if skip_and_continue:
            # spec §10.2: an explicit skip-and-continue opt-in MUST be recorded.
            self.ledger.append(
                session_id, "config_override", {"reason": "skip_and_continue", "by": by}
            )

        outcomes: list[CompensationOutcome] = []
        halted = False
        for step in plan.steps:
            if step.status != "reversible":
                outcomes.append(
                    CompensationOutcome(
                        step.step_seq, step.tool, "skipped", {"reason": step.status}
                    )
                )
                continue
            assert step.compensation is not None
            comp_tool = step.compensation["tool"]
            comp_args = step.compensation["args"]

            # spec §12: compensations pass through the same policy engine as
            # forward actions -- an undo that exceeds a cap also pauses.
            comp_plan = self._compensation_plan(session_id, step.step_seq, comp_tool, comp_args)
            verdict = self.policy_engine.evaluate(comp_plan, self.policy)
            self.ledger.append(
                session_id,
                "policy_evaluated",
                {"tool": comp_tool, "verdict": verdict.verdict, "reasons": verdict.reasons},
                step_seq=step.step_seq,
            )

            if verdict.verdict == "deny":
                outcomes.append(
                    CompensationOutcome(
                        step.step_seq, comp_tool, "denied", {"reasons": verdict.reasons}
                    )
                )
                if halt_on_failure and not skip_and_continue:
                    halted = True
                    break
                continue

            if verdict.verdict == "pause":
                assert self.approvals is not None
                # Same binding as `ApprovalStage.check` (spec §12 approver
                # binding): a prior approval of this exact compensation
                # plan lets a later rewind attempt proceed instead of
                # parking it again.
                existing = self.approvals.for_plan(comp_plan.plan_id)
                if existing is None:
                    item = self.approvals.request(
                        session_id,
                        comp_plan.plan_id,
                        comp_plan.model_dump(mode="json"),
                        step_seq=step.step_seq,
                    )
                    self.ledger.append(
                        session_id,
                        "approval_requested",
                        {"approval_id": item.approval_id, "plan_id": comp_plan.plan_id},
                        step_seq=step.step_seq,
                    )
                    outcomes.append(
                        CompensationOutcome(
                            step.step_seq, comp_tool, "paused", {"approval_id": item.approval_id}
                        )
                    )
                    if halt_on_failure and not skip_and_continue:
                        halted = True
                        break
                    continue
                if existing.state != "approved":
                    outcomes.append(
                        CompensationOutcome(
                            step.step_seq,
                            comp_tool,
                            "paused",
                            {"approval_id": existing.approval_id, "state": existing.state},
                        )
                    )
                    if halt_on_failure and not skip_and_continue:
                        halted = True
                        break
                    continue
                # `existing.state == "approved"` -- fall through to execute.

            try:
                await compensate_one(
                    self.ledger, session_id, step.step_seq, step.compensation, executor
                )
            except Exception as exc:
                outcomes.append(
                    CompensationOutcome(
                        step.step_seq, comp_tool, "compensation_failed", {"error": str(exc)}
                    )
                )
                if halt_on_failure and not skip_and_continue:
                    halted = True
                    break
                continue

            v_dict: dict[str, Any] | None = None
            if step.verification is not None:
                try:
                    v_result = await executor(step.verification["tool"], step.verification["args"])
                except Exception as exc:
                    passed, v_dict = False, {"error": str(exc)}
                else:
                    v_dict = _as_dict(v_result)
                    passed = _check_verification(step.verification["expect"], v_dict)

                if not passed:
                    self.ledger.append(
                        session_id,
                        "step_failed",
                        {
                            "step_seq": step.step_seq,
                            "verification": step.verification,
                            "result": v_dict,
                            "error": {"code": "verification_failed", "detail": v_dict},
                        },
                        step_seq=step.step_seq,
                    )
                    # A verified failure does NOT count as compensated (spec
                    # §10.3): the undo call happened, but the step stays
                    # not-successfully-compensated for accounting purposes.
                    outcomes.append(
                        CompensationOutcome(
                            step.step_seq, comp_tool, "verification_failed", v_dict
                        )
                    )
                    if halt_on_failure and not skip_and_continue:
                        halted = True
                        break
                    continue

            restored = (
                v_dict is not None
                and step.pre_snapshot is not None
                and canonical_bytes(v_dict) == canonical_bytes(step.pre_snapshot)
            )
            outcomes.append(
                CompensationOutcome(
                    step.step_seq,
                    comp_tool,
                    "compensated",
                    result="restored" if restored else "compensated",
                )
            )

        report = RewindReport(
            session_id=session_id, dry_run=False, plan=plan, outcomes=outcomes, halted=halted
        )
        self.ledger.append(
            session_id,
            "rewind_completed",
            {
                "fully_rewound": report.fully_rewound,
                "verified_result": report.verified_result,
                "restored": [o.step_seq for o in outcomes if o.result == "restored"],
                "compensated": [o.step_seq for o in outcomes if o.status == "compensated"],
                "skipped": [o.step_seq for o in outcomes if o.status == "skipped"],
                "failed": [
                    o.step_seq
                    for o in outcomes
                    if o.status in ("compensation_failed", "verification_failed", "denied")
                ],
                "paused": [o.step_seq for o in outcomes if o.status == "paused"],
                "halted": halted,
            },
        )
        return report
