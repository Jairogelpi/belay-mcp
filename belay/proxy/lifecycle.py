"""Request lifecycle (spec §3): resolve -> plan -> policy -> (approval) -> execute.

E3 implemented the L1 slice normatively: **resolve** (contract lookup + the
default rule of §4.6) and **execute** (passthrough), with a ledger event at
every stage transition (§9.1). E4 replaced `PlanStage`/`PolicyStage`'s bodies
with the real `Planner`/`PolicyEngine` (spec §5, §6). E5 does the same for
`ApprovalStage` (spec §7): a `pause` verdict now really parks the action in
`belay.approvals.queue.ApprovalQueue` and the agent gets back a structured
`pending_approval` result instead of the call completing anyway.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from belay.approvals.queue import ApprovalQueue
from belay.clock import Clock, SystemClock
from belay.contracts.model import Contract, ContractSet
from belay.errors import BelayError
from belay.executor.saga import SagaExecutor
from belay.ledger.store import LedgerStore
from belay.planner.model import NativeDryRunCaller, Plan, PlanningSession
from belay.planner.planner import Planner
from belay.policy.engine import PolicyEngine
from belay.policy.model import PolicyDoc, PolicyResult, default_policy


@dataclass(frozen=True)
class ResolvedCall:
    """Outcome of `resolve()`: the contract (if any) and effects that govern a call."""

    tool: str
    args: dict[str, Any]
    contract: Contract | None
    effects: list[dict[str, Any]]
    config_override: bool


def resolve(
    tool: str,
    args: dict[str, Any],
    contract_set: ContractSet,
    *,
    read_only_hint: bool,
    unsafe_passthrough: bool,
) -> ResolvedCall:
    """Resolve one call's governing contract, applying the default rule (spec §4.6).

    Verbatim from the spec, for a tool with **no contract**:

    - `readOnlyHint: true` => treat as `effects: []` (recorded here as an
      implicit `read` effect), allow.
    - Otherwise => `contract_missing`, unless the operator has explicitly
      configured `unsafe_passthrough: true` for this tool, which MUST be
      recorded in every affected ledger event.

    `destructiveHint` (or any hint other than `readOnlyHint`) never
    authorizes a call on its own — Appendix C is explicit that
    `destructiveHint: true` with no contract is still `contract_missing`.
    """
    contract = contract_set.resolve(tool)
    if contract is not None:
        effects = [e.model_dump(mode="json") for e in contract.effects]
        return ResolvedCall(tool, args, contract, effects, config_override=False)

    if read_only_hint:
        return ResolvedCall(
            tool, args, None, [{"type": "read", "resource": tool}], config_override=False
        )

    if unsafe_passthrough:
        return ResolvedCall(tool, args, None, [], config_override=True)

    raise BelayError("contract_missing", {"tool": tool})


class PlanStage:
    """Wraps `Planner.plan()` (spec §5, plan.md E4)."""

    def __init__(self, planner: Planner) -> None:
        self._planner = planner

    async def plan(self, resolved: ResolvedCall, session: PlanningSession) -> Plan:
        return await self._planner.plan(resolved.tool, resolved.args, session)


class PolicyStage:
    """Wraps `PolicyEngine.evaluate()` against one fixed policy document (spec §6, plan.md E4)."""

    def __init__(self, engine: PolicyEngine, policy: PolicyDoc) -> None:
        self._engine = engine
        self._policy = policy

    def evaluate(self, plan: Plan) -> PolicyResult:
        return self._engine.evaluate(plan, self._policy)


@dataclass(frozen=True)
class PendingApproval:
    """The structured, non-error shape the agent gets back while parked (spec §7.3)."""

    approval_id: str
    poll_after_ms: int = 5_000


@dataclass(frozen=True)
class ApprovalCheck:
    """Outcome of gating one call against the approval queue for its `plan_id`."""

    proceed: bool
    created: bool
    pending: PendingApproval | None = None


class ApprovalStage:
    """Gates a `pause` verdict through `belay.approvals.queue.ApprovalQueue` (spec §7).

    Bound to `plan_id` (spec §12 approver binding): a re-plan of the same
    logical call produces a new `Plan` with a new `plan_id`, so any approval
    item tied to the old `plan_id` is simply never found again -- it is
    invalidated by construction, not by an extra invalidation step.

    This class only ever *reads* queue state (`for_plan`) or *creates* a new
    pending item (`request`) on the agent's behalf. It has no `approve`/
    `reject` call sites -- those live only in `belay/cli/main.py`'s
    `approvals` subcommands, so the agent-facing proxy has no code path that
    can approve or reject anything (spec §12 no-self-approval).
    """

    def __init__(self, queue: ApprovalQueue | None = None) -> None:
        self.queue = queue or ApprovalQueue()

    def check(self, verdict: str, plan: Plan, session_id: str, step_seq: int) -> ApprovalCheck:
        if verdict != "pause":
            return ApprovalCheck(proceed=True, created=False)

        existing = self.queue.for_plan(plan.plan_id)
        if existing is None:
            item = self.queue.request(
                session_id, plan.plan_id, plan.model_dump(mode="json"), step_seq=step_seq
            )
            return ApprovalCheck(
                proceed=False, created=True, pending=PendingApproval(item.approval_id)
            )

        if existing.state == "approved":
            return ApprovalCheck(proceed=True, created=False)
        if existing.state == "rejected":
            raise BelayError(
                "approval_rejected",
                {"approval_id": existing.approval_id, "reason": existing.reason},
            )
        if existing.state == "expired":
            raise BelayError("approval_expired", {"approval_id": existing.approval_id})
        # still pending
        return ApprovalCheck(
            proceed=False, created=False, pending=PendingApproval(existing.approval_id)
        )


Executor = Callable[[str, dict[str, Any]], Awaitable[Any]]


class ExecuteStage:
    """Runs the real tool call through the saga step lifecycle (spec §8.1).

    Replaces the flat tool_called/result_recorded passthrough E3-E5 used:
    every call that reaches this stage now goes through the full six-stage
    cycle of `belay.executor.saga.SagaExecutor.run_step` -- journaled,
    capturing, calling, result_recorded, compensation_registered, committed.
    """

    def __init__(self, saga: SagaExecutor) -> None:
        self.saga = saga

    async def execute(
        self,
        session_id: str,
        step_seq: int,
        tool: str,
        args: dict[str, Any],
        contract: Contract | None,
        executor: Executor,
        *,
        set_hash: str | None = None,
    ) -> Any:
        outcome = await self.saga.run_step(
            session_id, step_seq, tool, args, contract, executor, set_hash=set_hash
        )
        return outcome.result


@dataclass
class Lifecycle:
    """Wires resolve -> plan -> policy -> (approval) -> execute (spec §3) for one session.

    `contract_set` is captured once at construction time and never swapped:
    per spec §4.7, a session pins the `set_hash` present at
    `session_started`, and changing contracts mid-session MUST NOT
    retroactively affect calls the session already governs.
    """

    contract_set: ContractSet
    unsafe_passthrough_tools: frozenset[str]
    ledger: LedgerStore
    session_id: str
    policy: PolicyDoc = field(default_factory=default_policy)
    clock: Clock = field(default_factory=SystemClock)
    native_dry_run: NativeDryRunCaller | None = None
    plan_stage: PlanStage | None = None
    policy_stage: PolicyStage | None = None
    approval_stage: ApprovalStage | None = None
    execute_stage: ExecuteStage | None = None
    _step_seq: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.plan_stage is None:
            self.plan_stage = PlanStage(Planner(clock=self.clock))
        if self.policy_stage is None:
            self.policy_stage = PolicyStage(
                PolicyEngine(clock=self.clock, ledger=self.ledger), self.policy
            )
        if self.approval_stage is None:
            # Share the ledger's SQLite file (spec §7): the CLI's `belay
            # approvals` subcommands run as a separate process and must see
            # the same queue.
            self.approval_stage = ApprovalStage(
                ApprovalQueue(engine=self.ledger.engine, clock=self.clock)
            )
        if self.execute_stage is None:
            self.execute_stage = ExecuteStage(
                SagaExecutor(ledger=self.ledger, contract_set=self.contract_set)
            )

    def start_session(self) -> None:
        """Emit `session_started` / `contract_set_pinned`, fixing this session's `set_hash`."""
        self.ledger.append(
            self.session_id,
            "session_started",
            {"tool_count": len(self.contract_set.contracts)},
            set_hash=self.contract_set.set_hash,
        )
        self.ledger.append(
            self.session_id,
            "contract_set_pinned",
            {"tools": sorted(self.contract_set.contracts)},
            set_hash=self.contract_set.set_hash,
        )

    async def govern_and_execute(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        read_only_hint: bool,
        executor: Executor,
    ) -> Any:
        """Run one call through resolve -> plan -> policy -> (approval) -> execute (spec §3)."""
        from belay.rewind.service import is_fenced

        if is_fenced(self.ledger, self.session_id):
            # spec §10.1: a fenced session is closed to new steps.
            raise BelayError("session_fenced", {"session_id": self.session_id})

        self._step_seq += 1
        step_seq = self._step_seq
        unsafe = tool in self.unsafe_passthrough_tools

        try:
            resolved = resolve(
                tool,
                args,
                self.contract_set,
                read_only_hint=read_only_hint,
                unsafe_passthrough=unsafe,
            )
        except BelayError as exc:
            self.ledger.append(
                self.session_id,
                "step_failed",
                {"tool": tool, "args": args, "error": exc.to_dict(), "config_override": unsafe},
                step_seq=step_seq,
                set_hash=self.contract_set.set_hash,
            )
            raise

        if resolved.config_override:
            self.ledger.append(
                self.session_id,
                "config_override",
                {"tool": tool, "reason": "unsafe_passthrough"},
                step_seq=step_seq,
                set_hash=self.contract_set.set_hash,
            )

        # Stages 2-4 (spec §3): plan (§5) -> policy (§6) -> approval (§7, still an E5 stub).
        assert self.plan_stage is not None and self.policy_stage is not None
        planning_session = PlanningSession(
            session_id=self.session_id,
            contract=resolved.contract,
            implicit_effects=resolved.effects if resolved.contract is None else [],
            native_dry_run=self.native_dry_run,
        )
        plan = await self.plan_stage.plan(resolved, planning_session)
        self.ledger.append(
            self.session_id,
            "plan_created",
            plan.model_dump(mode="json"),
            step_seq=step_seq,
            set_hash=self.contract_set.set_hash,
        )

        policy_result = self.policy_stage.evaluate(plan)
        plan = plan.with_policy(
            policy_result.verdict, policy_result.reasons, policy_result.requires_approval
        )
        self.ledger.append(
            self.session_id,
            "policy_evaluated",
            {
                "verdict": policy_result.verdict,
                "reasons": policy_result.reasons,
                "relaxations": policy_result.relaxations,
            },
            step_seq=step_seq,
            set_hash=self.contract_set.set_hash,
        )

        if policy_result.relaxations:
            # spec §6.4: per-tool relaxation of the irreversible default is
            # configuration, and MUST be visible in the ledger.
            self.ledger.append(
                self.session_id,
                "config_override",
                {
                    "tool": tool,
                    "reason": "irreversible_default_relaxed",
                    "rules": policy_result.relaxations,
                },
                step_seq=step_seq,
                set_hash=self.contract_set.set_hash,
            )

        if policy_result.verdict == "deny":
            deny_exc = BelayError("policy_denied", {"tool": tool, "reasons": policy_result.reasons})
            self.ledger.append(
                self.session_id,
                "step_failed",
                {"tool": tool, "args": args, "error": deny_exc.to_dict()},
                step_seq=step_seq,
                set_hash=self.contract_set.set_hash,
            )
            raise deny_exc

        # `pause` parks the action in the approval queue (spec §7). A
        # `rejected`/`expired` item raises (handled like any other §11 error
        # at the proxy boundary); a still-`pending` or newly created item
        # returns a structured `pending_approval` result instead of
        # proceeding to execute (spec §7.3) -- this is the one legitimate
        # early return from `govern_and_execute` that isn't an exception.
        assert self.approval_stage is not None
        check = self.approval_stage.check(policy_result.verdict, plan, self.session_id, step_seq)
        if check.created:
            assert check.pending is not None
            self.ledger.append(
                self.session_id,
                "approval_requested",
                {"approval_id": check.pending.approval_id, "plan_id": plan.plan_id},
                step_seq=step_seq,
                set_hash=self.contract_set.set_hash,
            )
        if not check.proceed:
            assert check.pending is not None
            return {
                "status": "pending_approval",
                "approval_id": check.pending.approval_id,
                "poll_after_ms": check.pending.poll_after_ms,
            }

        # Stage 5 (spec §3, §8): the real saga step lifecycle (§8.1) --
        # journaled -> capturing -> calling -> result_recorded ->
        # compensation_registered -> committed. `step_journaled`/`tool_called`/
        # `result_recorded` etc. below are emitted by `ExecuteStage`/
        # `SagaExecutor`, not by this method directly.
        assert self.execute_stage is not None
        return await self.execute_stage.execute(
            self.session_id,
            step_seq,
            tool,
            args,
            resolved.contract,
            executor,
            set_hash=self.contract_set.set_hash,
        )
