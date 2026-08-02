"""Anomaly-baseline reuse for the Native Agent Gate (R1.8.x, ADR 0026).

Deliberately NOT a parallel tracker, unlike `belay/hooks/quota.py`'s
`HookQuotaTracker` (ADR 0023): `belay/policy/baseline.py::BaselineStore`
has zero identity dependency -- it only reads `plan_created` events
matching `(tool, effect_type)` from the ledger, which R1.8 (ADR 0026)
now makes real for hooks. Quota needed a parallel tracker because its
blocker was identity (`session_started.initiated_by`, which hooks
structurally has no equivalent of and deliberately still doesn't add
one for, per ADR 0026's R1.8.x resolution); anomaly's blocker was only
ever missing ledger events, and that's already fixed. Reusing
`BaselineStore` directly here is the correct move *because* the two
blockers were never the same kind of problem, not a change of mind
about R1.7.3's or ADR 0023's own reasoning.

Honest limitation, stronger than it first looks, not silently glossed
over: anomaly detection compares a numeric `effect.count` against
trailing history (`belay/policy/baseline.py::upper_bound`). Hooks' own
coarse, no-contract guesses (`belay/hooks/gate.py::resolve_effects`'s
fallback effects for Bash/file surfaces) never set a `count` at all --
there is no inherent numeric magnitude to a bare `Write` or
`git status`, so this check is silently skipped there, same as
`belay/policy/engine.py::_evaluate_anomaly` already does when a count
can't be parsed.

But even a *configured* `ContractSet` doesn't make this check "live" in
practice today. `resolve_effects` uses `contract.effects` verbatim (spec
§5.3's "contract"-basis) -- a literal count declared once in the YAML,
identical on every single call to that tool, never recomputed per call.
The MCP proxy path only ever sees a genuinely varying, real per-call
count via `sql_simulator` (`belay/planner/planner.py::_sql_effects`,
ADR 0011) or `native_dry_run` (the tool itself executed dry and asked
what it would do) -- both requiring something to actually run at
decision time. Hooks has neither: it only observes a `PreToolUse` event
and a static contract file, so the "outlier" case this z-score check
exists to catch structurally cannot occur yet against a real,
literal-count contract -- `value` will equal `stats.mean` (once a
baseline exists) on every call, never exceed it. This is real, tested
machinery, not dead code, and the correct foundation for the day hooks
gains its own dynamic count source (mirroring one of those two proxy
mechanisms) -- but as of R1.8.x it cannot actually pause anything for a
genuine anomaly, and tests/docs must say so plainly rather than imply
otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from belay.ledger.store import LedgerStore
from belay.policy.baseline import BaselineStore, upper_bound
from belay.supervisor.protocol import HookEvent

#: Same defaults as `belay/policy/model.py::AnomalyDefaults` -- not
#: independently invented. This first cut has no per-run tuning surface
#: (`belay hooks install --anomaly` is a bare opt-in flag); revisit only
#: if an operator actually needs different numbers.
DEFAULT_Z_SCORE_THRESHOLD = 3.0
DEFAULT_MIN_SAMPLES = 10

#: Below this, treat stddev as zero (matches
#: `belay/policy/engine.py::_ANOMALY_EPSILON`'s own tie-breaking so the
#: two implementations agree on the edge case, not just the common one).
_ANOMALY_EPSILON = 1e-9


@dataclass(frozen=True)
class AnomalyConfig:
    """Bundles what `belay/hooks/gate.py::evaluate_anomaly` needs,
    mirroring `belay/hooks/quota.py::QuotaConfig`'s pointer-file-loaded,
    configured-opt-in, off-by-default posture exactly -- loaded once by
    the supervisor at construction."""

    ledger: LedgerStore
    z_score_threshold: float = DEFAULT_Z_SCORE_THRESHOLD
    min_samples: int = DEFAULT_MIN_SAMPLES


def evaluate_anomaly(
    event: HookEvent, effects: list[dict[str, Any]], config: AnomalyConfig
) -> str | None:
    """Checks each declared effect's count against this session's own
    trailing history via `BaselineStore`, the same z-score check
    `belay/policy/engine.py::_evaluate_anomaly` already runs for the MCP
    proxy path (same math, same defaults) -- reused directly, not
    reimplemented a second time. Returns a human-readable reason if any
    effect is anomalous, else `None`.

    An effect with no parseable `count` is silently skipped (`continue`,
    never a crash or a false anomaly) -- see the module docstring for
    when that's every hook-gated call vs. a real, configured
    count-bearing contract."""
    from belay.hooks.gate import ledger_session_id

    store = BaselineStore(config.ledger)
    session_id = ledger_session_id(event)
    for effect in effects:
        value = upper_bound(effect.get("count"))
        if value is None:
            continue
        effect_type = effect.get("type")
        if not isinstance(effect_type, str):
            continue
        stats = store.stats(session_id, event.tool_name, effect_type)
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
        return (
            f"anomaly: {event.tool_name} {effect_type} count {value:g} is {ratio:.1f}x the "
            f"trailing baseline of {stats.mean:.1f} (z={z:.2f}, n={stats.n}, "
            f"stddev={stddev:.2f})"
        )
    return None
