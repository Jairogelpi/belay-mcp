"""Request lifecycle (spec §3): resolve -> plan -> policy -> (approval) -> execute.

E3 implements the L1 slice normatively: **resolve** (contract lookup + the
default rule of §4.6) and **execute** (passthrough), with a ledger event at
every stage transition (§9.1) even though plan/policy/approval are trivial
pass-through stubs here. E4 (planner/policy), E5 (approvals), and E6 (saga
step lifecycle) replace `PlanStage`/`PolicyStage`/`ApprovalStage`'s bodies
without changing `Lifecycle`'s shape or call sites.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from belay.contracts.model import Contract, ContractSet
from belay.errors import BelayError
from belay.ledger.store import LedgerStore


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
    """L1 stub for spec §5 (planner lands in E4): a static contract-basis plan."""

    def plan(self, resolved: ResolvedCall) -> dict[str, Any]:
        return {"basis": "contract", "effects": resolved.effects}


class PolicyStage:
    """L1 stub for spec §6 (policy engine lands in E4): always allow."""

    def evaluate(self, plan: dict[str, Any]) -> str:
        return "allow"


class ApprovalStage:
    """L1 stub for spec §7 (approvals land in E5): nothing ever pauses yet."""

    async def maybe_park(self, verdict: str, plan: dict[str, Any]) -> None:
        return None


Executor = Callable[[str, dict[str, Any]], Awaitable[Any]]


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
    plan_stage: PlanStage = field(default_factory=PlanStage)
    policy_stage: PolicyStage = field(default_factory=PolicyStage)
    approval_stage: ApprovalStage = field(default_factory=ApprovalStage)
    _step_seq: int = field(default=0, init=False, repr=False)

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

        # Stages 2-4 (spec §3): no-op stubs in L1, wired for E4-E6 to fill in.
        plan = self.plan_stage.plan(resolved)
        self.policy_stage.evaluate(plan)
        await self.approval_stage.maybe_park("allow", plan)

        self.ledger.append(
            self.session_id,
            "tool_called",
            {"tool": tool, "args": args, "config_override": resolved.config_override},
            step_seq=step_seq,
            set_hash=self.contract_set.set_hash,
        )

        try:
            result = await executor(tool, args)
        except Exception as exc:
            self.ledger.append(
                self.session_id,
                "step_failed",
                {
                    "tool": tool,
                    "error": str(exc),
                    "config_override": resolved.config_override,
                },
                step_seq=step_seq,
                set_hash=self.contract_set.set_hash,
            )
            raise

        self.ledger.append(
            self.session_id,
            "result_recorded",
            {
                "tool": tool,
                "config_override": resolved.config_override,
                "effects": resolved.effects,
            },
            step_seq=step_seq,
            set_hash=self.contract_set.set_hash,
        )
        return result
