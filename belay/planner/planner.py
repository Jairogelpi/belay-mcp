"""Planner.plan(): dry-run bases and plan expiration (spec §5.3, §5.4).

Dry-run adapters (plan.md E4, plan-v2 E11):

- `contract` -- always available. Derives the estimate straight from the
  contract's declared `effects`. Every contract-basis count is honest about
  its own uncertainty (spec §5.3: "MUST NOT present contract-basis counts as
  exact"), so it is always marked `estimate: true`; a declared effect with no
  `count` at all goes to `unknown[]` instead of a guessed number.
- `sql_simulator` (plan-v2 E11) -- used when the contract declares a `sql`
  hint (`belay/contracts/model.py::SqlHint`) and the caller supplies a
  `PlanningSession.sql_runner`. Runs the real statement in a transaction that
  is always rolled back (`belay/planner/adapters/sql.py`) to get a real
  affected/matched row count -- `estimate: false`, since this is not a guess.
- `native_dry_run` -- used when the caller supplies a
  `PlanningSession.native_dry_run` callable (the tool exposes a `<tool>.dry_run`
  sibling) and it returns a result.
- `dry_run` (a generic `EXPLAIN`/`SELECT COUNT(*)` simulator not tied to a
  contract's `sql` hint) remains unimplemented -- `sql_simulator` supersedes
  the "future SQL dry-run adapter" issue plan.md §11 deferred. `Basis` still
  admits the `dry_run` literal for forward compatibility, but no code path in
  this module produces it.

Precedence (plan-v2 E11, docs/adr/0011-e11-sql-dry-run.md): `native_dry_run >
sql_simulator > dry_run > contract`. `Planner.plan()` builds the plan in that
reverse order below (weakest basis first), each stage overwriting the
previous one's `effects`/`basis` if it applies -- so the last stage that
fires wins, which is exactly the strongest-available basis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from belay.canonical import canonical_bytes, sha256_hex
from belay.clock import Clock, SystemClock
from belay.contracts.expressions import evaluate as evaluate_expression
from belay.contracts.expressions import parse as parse_expression
from belay.contracts.model import Contract
from belay.errors import BelayError
from belay.planner.model import EffectEstimate, Plan, PlanningSession, SqlRunner

DEFAULT_PLAN_TTL_SECONDS = 600  # 10 minutes (spec §5.4 default)


def _plan_id(session_id: str, tool: str, args: dict[str, Any]) -> str:
    """Deterministic over `(session_id, tool, args)` (spec §5.4/§12).

    A polling retry of the exact same call (spec §7.3's `poll_after_ms`)
    binds to the same `plan_id` and can pick up an already-resolved
    approval item; a genuine re-plan with different args (§12's
    "bait-and-switch via re-planning") gets a different `plan_id`, so any
    approval bound to the old one is never found for the new one.
    """
    digest = sha256_hex(canonical_bytes({"session_id": session_id, "tool": tool, "args": args}))
    return f"p_{digest[:16]}"


def _contract_effects(contract: Contract) -> tuple[list[EffectEstimate], list[dict[str, Any]]]:
    effects: list[EffectEstimate] = []
    unknown: list[dict[str, Any]] = []
    for effect in contract.effects:
        if effect.count is None:
            unknown.append({"type": effect.type, "resource": effect.resource})
            continue
        effects.append(
            EffectEstimate(
                type=effect.type,
                resource=effect.resource,
                count=effect.count,
                estimate=True,
                basis="contract",
                amount=effect.amount,
                recipients=effect.recipients,
            )
        )
    return effects, unknown


def _implicit_effects(raw: list[dict[str, Any]]) -> list[EffectEstimate]:
    # From resolve()'s §4.6 default rule (readOnlyHint implicit read, or
    # unsafe_passthrough with no declared effects): these are exact, not
    # estimates -- there is nothing uncertain about "this call did a read".
    return [
        EffectEstimate(
            type=e["type"],
            resource=e["resource"],
            count=None,
            estimate=False,
            basis="contract",
        )
        for e in raw
    ]


def _native_effects(
    result: dict[str, Any],
) -> tuple[list[EffectEstimate], list[dict[str, Any]]]:
    effects = [
        EffectEstimate(
            type=e["type"],
            resource=e["resource"],
            count=e.get("count"),
            estimate=bool(e.get("estimate", False)),
            basis="native_dry_run",
            amount=e.get("amount"),
            recipients=e.get("recipients"),
        )
        for e in result.get("effects", [])
    ]
    unknown = list(result.get("unknown", []))
    return effects, unknown


async def _sql_effects(
    contract: Contract, args: dict[str, Any], sql_runner: SqlRunner
) -> list[EffectEstimate]:
    """Run `contract.sql`'s statement for real via `sql_runner` (plan-v2 E11).

    Bind params are Belay expressions (spec §4.3), evaluated against
    `$args`/`$context` -- the same grammar and evaluator `undo.args` uses, no
    second templating engine. The resulting real row count replaces every
    declared effect's `count`, marked `estimate: false`: this is a measured
    number, not a guess.
    """
    assert contract.sql is not None
    scope = {"args": args, "context": {}}
    params = {
        name: evaluate_expression(parse_expression(expr), scope)
        for name, expr in (contract.sql.params or {}).items()
    }
    count = await sql_runner(contract.sql.statement, params)
    return [
        EffectEstimate(
            type=effect.type,
            resource=effect.resource,
            count=str(count),
            estimate=False,
            basis="sql_simulator",
            amount=effect.amount,
            recipients=effect.recipients,
        )
        for effect in contract.effects
    ]


def _confidence(basis: str, unknown: list[dict[str, Any]]) -> Literal["high", "medium", "low"]:
    if unknown:
        return "low"
    if basis in ("native_dry_run", "sql_simulator"):
        return "high"
    return "medium"


@dataclass
class Planner:
    """`Planner.plan(tool, args, session) -> Plan` (spec §5, plan.md E4)."""

    clock: Clock = field(default_factory=SystemClock)
    plan_ttl_seconds: int = DEFAULT_PLAN_TTL_SECONDS

    async def plan(self, tool: str, args: dict[str, Any], session: PlanningSession) -> Plan:
        """Predict `tool(args)`'s effects without executing it (spec §5.1)."""
        if session.contract is not None:
            effects, unknown = _contract_effects(session.contract)
            reversibility = session.contract.reversibility
            basis = "contract"
        else:
            effects = _implicit_effects(session.implicit_effects)
            unknown = []
            reversibility = "reversible"
            basis = "contract"

        if (
            session.contract is not None
            and session.contract.sql is not None
            and session.sql_runner is not None
        ):
            effects = await _sql_effects(session.contract, args, session.sql_runner)
            unknown = []
            basis = "sql_simulator"

        if session.native_dry_run is not None:
            native_result = await session.native_dry_run(tool, args)
            if native_result is not None:
                effects, unknown = _native_effects(native_result)
                basis = "native_dry_run"

        now = self.clock.now()
        expires = now + timedelta(seconds=self.plan_ttl_seconds)
        return Plan(
            plan_id=_plan_id(session.session_id, tool, args),
            session_id=session.session_id,
            tool=tool,
            args=args,
            effects=effects,
            reversibility=reversibility,
            confidence=_confidence(basis, unknown),
            unknown=unknown,
            created_at=now.isoformat(),
            expires_at=expires.isoformat(),
        )


def check_plan_binding(plan: Plan, tool: str, args: dict[str, Any], *, clock: Clock) -> None:
    """Re-validate a plan/execute binding at execution time (spec §5.4).

    Raises `plan_mismatch` if `args` are not byte-identical (compared via
    canonical serialization) to the planned args, or `plan_expired` if
    `clock.now()` is past the plan's `expires_at`. Order matters: a mismatch
    is checked first because it is never "retryable" (a caller must re-plan
    with the real args regardless of whether the old plan also expired).
    """
    if tool != plan.tool or canonical_bytes(args) != canonical_bytes(plan.args):
        raise BelayError(
            "plan_mismatch",
            {"plan_id": plan.plan_id, "planned_tool": plan.tool, "tool": tool},
        )
    now = clock.now()
    expires_at = datetime.fromisoformat(plan.expires_at)
    if now > expires_at:
        raise BelayError(
            "plan_expired",
            {"plan_id": plan.plan_id, "expires_at": plan.expires_at, "now": now.isoformat()},
        )
