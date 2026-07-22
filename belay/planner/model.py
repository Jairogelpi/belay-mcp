"""Plan and EffectEstimate models (spec §5.1, §5.2)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from typing import Literal as TLiteral

from pydantic import BaseModel, ConfigDict, Field

from belay.contracts.model import Contract

EffectType = TLiteral["create", "update", "delete", "send", "spend", "execute", "read"]
Basis = TLiteral["native_dry_run", "dry_run", "contract"]
Verdict = TLiteral["allow", "pause", "deny"]


class EffectEstimate(BaseModel):
    """One predicted effect of a plan (spec §5.2)."""

    model_config = ConfigDict(extra="forbid")

    type: EffectType
    resource: str
    count: str | None = None
    estimate: bool = False
    basis: Basis = "contract"
    amount: dict[str, Any] | None = None
    recipients: str | None = None

    def upper_bound(self) -> int | None:
        """Parse `count` (`"N"` or `"~N"`) into an integer upper bound, if possible."""
        if self.count is None:
            return None
        text = self.count.lstrip("~")
        try:
            return int(text)
        except ValueError:
            return None

    def recipients_upper_bound(self) -> int | None:
        if self.recipients is None:
            return None
        text = self.recipients.lstrip("~")
        try:
            return int(text)
        except ValueError:
            return None


class Plan(BaseModel):
    """A predicted set of effects for one action, produced without executing it (spec §5.1)."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    session_id: str
    tool: str
    args: dict[str, Any]
    effects: list[EffectEstimate]
    reversibility: TLiteral["reversible", "irreversible", "conditional"]
    policy_verdict: Verdict = "allow"
    policy_reasons: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    confidence: TLiteral["high", "medium", "low"]
    unknown: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str
    expires_at: str

    def with_policy(self, verdict: Verdict, reasons: list[str], requires_approval: bool) -> Plan:
        """Return a copy of this plan with policy-evaluation fields filled in.

        `Planner.plan()` produces a plan with the default `allow`/no-reasons
        fields; `PolicyEngine.evaluate()` is a pure function of `(plan,
        policy)` that does not mutate the plan itself, so this is how callers
        (the lifecycle, the CLI) merge its `PolicyResult` back onto the plan
        object for display/ledger purposes.
        """
        return self.model_copy(
            update={
                "policy_verdict": verdict,
                "policy_reasons": reasons,
                "requires_approval": requires_approval,
            }
        )


NativeDryRunCaller = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]


@dataclass
class PlanningSession:
    """Everything `Planner.plan()` needs about the call's session and provenance.

    `contract`/`implicit_effects` mirror the outcome `resolve()` (spec §4.6)
    already computed in `belay/proxy/lifecycle.py` -- the planner does not
    re-derive the default rule, it consumes its result so there is exactly
    one place that implements §4.6.
    """

    session_id: str
    contract: Contract | None = None
    implicit_effects: list[dict[str, Any]] = field(default_factory=list)
    native_dry_run: NativeDryRunCaller | None = None
