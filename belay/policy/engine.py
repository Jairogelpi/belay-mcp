"""PolicyEngine.evaluate(plan, policy) -> verdict + reasons.

Spec §6.2, §6.3, §6.4.

Evaluation model (spec §6.2): each dimension below independently produces at
most one `(verdict, rule_id)` -- "first match wins" within that dimension's
rule list -- except `caps`, where every cap is an independent constraint and
each breached cap fires on its own. The final verdict is the most restrictive
across every dimension that fired (`deny > pause > allow`); `reasons` carries
every rule id that fired, not just the one that decided the final verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from fnmatch import fnmatch

from belay.clock import Clock, SystemClock
from belay.ledger.store import LedgerStore
from belay.planner.model import EffectEstimate, Plan
from belay.policy.baseline import BaselineStore
from belay.policy.model import Cap, CapMatch, PolicyDoc, PolicyResult, Verdict
from belay.policy.quota import QuotaTracker, parse_window

_SEVERITY: dict[Verdict, int] = {"allow": 0, "pause": 1, "deny": 2}
_ANOMALY_EPSILON = 1e-9


def _matches(match: CapMatch, effect: EffectEstimate) -> bool:
    if match.effect is not None and match.effect != effect.type:
        return False
    return match.resource is None or fnmatch(effect.resource, match.resource)


def _parse_time(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def _in_window(now: datetime, between: tuple[str, str]) -> bool:
    start, end = _parse_time(between[0]), _parse_time(between[1])
    current = now.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end  # window wraps past midnight


def _evaluate_cap(cap: Cap, plan: Plan) -> Verdict | None:
    matching = [e for e in plan.effects if _matches(cap.match, e)]
    if not matching:
        return None

    if cap.max_count is not None:
        counted = [e.upper_bound() for e in matching]
        total = sum(c for c in counted if c is not None)
        if total > cap.max_count:
            return cap.over

    if cap.max_recipients is not None:
        recipients = [e.recipients_upper_bound() for e in matching]
        total_recipients = sum(r for r in recipients if r is not None)
        if total_recipients > cap.max_recipients:
            return cap.over

    if cap.max_amount is not None:
        total_amount = 0.0
        for e in matching:
            if e.amount is None:
                continue
            if e.amount.get("currency") != cap.max_amount.currency:
                continue
            value = e.amount.get("value")
            if isinstance(value, int | float):
                total_amount += float(value)
        if total_amount > cap.max_amount.value:
            return cap.over

    return None


def _scope_matches(scope: CapMatch, effects: list[EffectEstimate]) -> bool:
    return any(_matches(scope, e) for e in effects)


def _evaluate_anomaly(
    plan: Plan,
    policy: PolicyDoc,
    ledger: LedgerStore,
    covered: set[tuple[str, str]],
) -> tuple[Verdict, str] | None:
    """The `anomaly` dimension (plan-v2 E10): flag an effect count far above its own history.

    Cold start (fewer than `min_samples` prior calls for this `(tool,
    effect_type)` in this session) always contributes nothing -- never block
    on insufficient data. Skips effects a fired `Cap` already covers
    (`covered`), so a cap and the anomaly baseline don't double-fire on the
    same effect; an anomaly with no cap configured at all still fires on its
    own, which is the win condition (docs/adr/0010-e10-anomaly-baselines.md).
    """
    config = policy.defaults.anomaly
    if not config.enabled or any(fnmatch(plan.tool, pattern) for pattern in config.exclude):
        return None

    store = BaselineStore(ledger)
    for effect in plan.effects:
        if (effect.type, effect.resource) in covered:
            continue
        value = effect.upper_bound()
        if value is None:
            continue
        stats = store.stats(
            plan.session_id, plan.tool, effect.type, exclude_plan_id=plan.plan_id
        )
        if stats.n < config.min_samples:
            continue
        stddev = stats.stddev
        if stddev < _ANOMALY_EPSILON:
            anomalous = value > stats.mean
            z = float("inf") if anomalous else 0.0
        else:
            z = (value - stats.mean) / stddev
            anomalous = z >= config.z_score_threshold
        if not anomalous:
            continue
        ratio = value / stats.mean if stats.mean > _ANOMALY_EPSILON else float("inf")
        reason = (
            f"anomaly: {plan.tool} {effect.type} count {value:g} is {ratio:.1f}x the "
            f"trailing baseline of {stats.mean:.1f} (z={z:.2f}, n={stats.n}, "
            f"stddev={stddev:.2f})"
        )
        return config.verdict, reason
    return None


def _evaluate_quota(
    plan: Plan, policy: PolicyDoc, ledger: LedgerStore, now: datetime
) -> tuple[Verdict, str] | None:
    """The `quota` dimension (plan-v2 E15): a per-identity rolling cap on approved-and-
    executed irreversible actions, independent of any per-call `Cap` (E4).

    Only fires for irreversible-effect plans; only counts prior actions that
    were themselves approved (or auto-allowed) *and* actually executed
    (`QuotaTracker`), never denied or still-pending ones. No identity (no
    `session_started.initiated_by` on this plan's session) means quota simply
    doesn't contribute -- same "opt out by absence of data" spirit as E10's
    cold start.
    """
    config = policy.defaults.quota
    if not config.enabled or plan.reversibility != "irreversible":
        return None

    started = next(
        (e for e in ledger.read(plan.session_id) if e.type == "session_started"), None
    )
    identity = started.initiated_by if started is not None else None
    if identity is None:
        return None

    window = parse_window(config.window)
    tracker = QuotaTracker(ledger)
    count = tracker.count(identity, now=now, window=window)
    if count < config.max_irreversible_actions:
        return None

    reason = (
        f"quota: identity {identity!r} has {count} approved irreversible action(s) in the "
        f"trailing {config.window} window, at/over the configured max of "
        f"{config.max_irreversible_actions}"
    )
    return config.verdict, reason


@dataclass
class PolicyEngine:
    """`PolicyEngine.evaluate(plan, policy) -> PolicyResult` (spec §6)."""

    clock: Clock = field(default_factory=SystemClock)
    ledger: LedgerStore | None = None

    def evaluate(self, plan: Plan, policy: PolicyDoc) -> PolicyResult:
        fired: list[tuple[Verdict, str]] = []
        relaxations: list[str] = []

        tools_verdict: Verdict | None = None
        tools_reason: str | None = None
        for i, rule in enumerate(policy.tools):
            if fnmatch(plan.tool, rule.match):
                tools_verdict, tools_reason = rule.verdict, f"tools[{i}]"
                break

        # Irreversible default (spec §6.4), overridable per tool.
        if plan.reversibility == "irreversible":
            default_verdict = policy.defaults.irreversible
            if tools_verdict is not None and tools_reason is not None:
                if _SEVERITY[tools_verdict] < _SEVERITY[default_verdict]:
                    relaxations.append(tools_reason)
                fired.append((tools_verdict, tools_reason))
            else:
                fired.append((default_verdict, "defaults.irreversible"))
        elif tools_verdict is not None and tools_reason is not None:
            fired.append((tools_verdict, tools_reason))

        # Unknown effects are worst-case (spec §6.3).
        if plan.unknown:
            fired.append((policy.defaults.unknown_effects, "defaults.unknown_effects"))

        # Quiet hours -- first matching window wins.
        now = self.clock.now()
        for i, qh in enumerate(policy.quiet_hours):
            if _in_window(now, qh.between) and _scope_matches(qh.scope, plan.effects):
                fired.append((qh.verdict, f"quiet_hours[{i}]"))
                break

        # Caps -- each is an independent constraint.
        covered: set[tuple[str, str]] = set()
        for i, cap in enumerate(policy.caps):
            result = _evaluate_cap(cap, plan)
            if result is not None:
                fired.append((result, f"caps[{i}]"))
                covered |= {
                    (e.type, e.resource) for e in plan.effects if _matches(cap.match, e)
                }

        # Anomaly baseline (plan-v2 E10) -- only when a ledger is wired in
        # (spec: reads the session's own history, never global state).
        if self.ledger is not None:
            anomaly = _evaluate_anomaly(plan, policy, self.ledger, covered)
            if anomaly is not None:
                fired.append((anomaly[0], anomaly[1]))

            quota = _evaluate_quota(plan, policy, self.ledger, now)
            if quota is not None:
                fired.append((quota[0], quota[1]))

        if not fired:
            return PolicyResult(verdict="allow", reasons=[], requires_approval=False)

        verdict = max((v for v, _ in fired), key=lambda v: _SEVERITY[v])
        reasons = [r for _, r in fired]
        return PolicyResult(
            verdict=verdict,
            reasons=reasons,
            requires_approval=(verdict == "pause"),
            relaxations=relaxations,
        )
