"""Planner.plan(): dry-run bases and plan expiration (spec §5.3, §5.4).

Dry-run adapters v0.1 (plan.md E4):

- `contract` -- always available. Derives the estimate straight from the
  contract's declared `effects`. Every contract-basis count is honest about
  its own uncertainty (spec §5.3: "MUST NOT present contract-basis counts as
  exact"), so it is always marked `estimate: true`; a declared effect with no
  `count` at all goes to `unknown[]` instead of a guessed number.
- `native_dry_run` -- used when the caller supplies a
  `PlanningSession.native_dry_run` callable (the tool exposes a `<tool>.dry_run`
  sibling) and it returns a result. Precedence is `native_dry_run > dry_run >
  contract` (spec §5.3); this Planner tries `native_dry_run` first and falls
  back to `contract`.
- `dry_run` (e.g. `EXPLAIN`/`SELECT COUNT(*)` simulation for SQL) is
  deliberately **not implemented** in v0.1 -- see plan.md §11 ("SQL dry-run
  adapter" is listed as a post-v0.1 issue). `Basis` still admits the literal
  for forward compatibility with a future adapter, but no code path in this
  module produces it.
  # ponytail: no SQL/generic simulator adapter; add one (and wire it between
  # native_dry_run and contract below) when a concrete tool needs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from belay.canonical import canonical_bytes, sha256_hex
from belay.clock import Clock, SystemClock
from belay.contracts.model import Contract
from belay.errors import BelayError
from belay.planner.model import EffectEstimate, Plan, PlanningSession

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


def _confidence(basis: str, unknown: list[dict[str, Any]]) -> Literal["high", "medium", "low"]:
    if unknown:
        return "low"
    if basis == "native_dry_run":
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
