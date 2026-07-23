"""`explain(policy_result, plan) -> Explanation` (plan-v2 E16).

A PURE FORMATTING function: every number in its output is traceable back to
`PolicyResult.reasons` (already computed by `PolicyEngine.evaluate` -- E4's
caps/tools/quiet_hours/irreversible-default, E10's anomaly baseline, E15's
identity quota) or to `Plan`'s own already-computed fields. This module never
re-evaluates policy, never re-reads the ledger, never re-derives a number --
it only classifies and templates strings that already exist.

Disclosure policy (documented in full in `docs/adr/0016-...md`): **full
transparency, applied uniformly**. `reasons` strings that already carry
configured-threshold numbers (currently only the `quota` dimension embeds its
configured `max_irreversible_actions`; `anomaly`'s reason carries only this
call's *observed* z/mean/stddev/n, no configured threshold at all; `tools`/
`quiet_hours`/`caps`/irreversible-default reasons are bare rule ids with no
numbers either way) are echoed verbatim, never redacted. The invariant this
buys: `dimension.rule in dimension.detail` always holds -- there is no
per-dimension special-casing to accidentally get inconsistent later.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from belay.contracts.expressions import BinOp, Coalesce, Expr, PathRef, UnaryNot, parse
from belay.contracts.model import Contract
from belay.planner.model import Plan
from belay.policy.model import PolicyResult, Verdict


class Dimension(BaseModel):
    """One fired policy dimension, formatted for both human and agent consumption."""

    model_config = ConfigDict(extra="forbid")

    name: str
    rule: str
    detail: str


class Explanation(BaseModel):
    """Structured, deterministic blast-radius explanation attached to a governed response."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    headline: str
    dimensions: list[Dimension] = Field(default_factory=list)
    suggested_action: str | None = None


def _dimension_name(rule: str) -> str:
    if rule.startswith("tools["):
        return "tools"
    if rule.startswith("quiet_hours["):
        return "quiet_hours"
    if rule.startswith("caps["):
        return "caps"
    if rule.startswith("anomaly:"):
        return "anomaly"
    if rule.startswith("quota:"):
        return "quota"
    if rule == "defaults.irreversible":
        return "irreversible_default"
    if rule == "defaults.unknown_effects":
        return "unknown_effects"
    return "other"  # pragma: no cover - defensive, no known rule id shape falls here


def _detail(name: str, rule: str, plan: Plan) -> str:
    # anomaly/quota reasons are already a full, number-bearing sentence
    # (built once by PolicyEngine.evaluate) -- echoed verbatim, not reformatted.
    if name in ("anomaly", "quota"):
        return rule
    if name == "irreversible_default":
        return (
            f"tool {plan.tool!r} is irreversible; the irreversible-default policy "
            f"applies ({rule})"
        )
    if name == "unknown_effects":
        return f"plan for {plan.tool!r} has one or more effects of unknown blast radius ({rule})"
    if name == "tools":
        return f"a `tools` rule matched {plan.tool!r} ({rule})"
    if name == "quiet_hours":
        return f"a `quiet_hours` rule matched the current time window for {plan.tool!r} ({rule})"
    if name == "caps":
        return f"a blast-radius cap was exceeded by this plan's effects for {plan.tool!r} ({rule})"
    return rule  # pragma: no cover - defensive


def _args_paths(expr: Expr) -> list[str]:
    """Every `$args.<path>` referenced by `expr`, in traversal order."""
    if isinstance(expr, PathRef):
        return [".".join(expr.path)] if expr.root == "args" and expr.path else []
    if isinstance(expr, UnaryNot):
        return _args_paths(expr.operand)
    if isinstance(expr, BinOp):
        return _args_paths(expr.left) + _args_paths(expr.right)
    if isinstance(expr, Coalesce):
        paths: list[str] = []
        for arg in expr.args:
            paths.extend(_args_paths(arg))
        return paths
    return []


def _narrowing_arg(contract: Contract | None) -> str | None:
    """The first `$args.<path>` a contract mechanically declares as narrowing this call's scope.

    Deterministic, contract-shape-derived signal only -- never a guess:
    a `conditions` expression (conditional contracts, spec §4.2) or an
    `sql.params` bind value (E11) that references `$args.<path>` both name an
    argument the caller can narrow to change this call's blast radius. If
    neither is present, there is nothing mechanical to suggest.
    """
    if contract is None:
        return None
    for cond in contract.conditions or []:
        paths = _args_paths(parse(cond))
        if paths:
            return paths[0]
    if contract.sql is not None:
        for expr_text in (contract.sql.params or {}).values():
            paths = _args_paths(parse(expr_text))
            if paths:
                return paths[0]
    return None


def explain(
    policy_result: PolicyResult, plan: Plan, contract: Contract | None = None
) -> Explanation:
    """Format `policy_result`/`plan` into an `Explanation` -- no new computation.

    `contract` is optional and, when given, only feeds the deterministic
    `suggested_action` narrowing-argument rule; omitting it simply means no
    `suggested_action` is offered (never a guessed one).
    """
    dimensions = []
    for rule in policy_result.reasons:
        name = _dimension_name(rule)
        dimensions.append(Dimension(name=name, rule=rule, detail=_detail(name, rule, plan)))

    headline = (
        "; ".join(d.detail for d in dimensions)
        if dimensions
        else f"allow: no policy dimension fired for {plan.tool!r}"
    )

    suggested_action: str | None = None
    if policy_result.verdict != "allow":
        arg_path = _narrowing_arg(contract)
        if arg_path is not None:
            suggested_action = f"narrow `args.{arg_path}` and re-plan the call to {plan.tool!r}"

    return Explanation(
        verdict=policy_result.verdict,
        headline=headline,
        dimensions=dimensions,
        suggested_action=suggested_action,
    )
