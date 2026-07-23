"""Per-session, per-(tool, effect_type) rolling baselines from the ledger (plan-v2 E10).

No LLM, no network call, no opaque ML model: a streaming mean/stddev
(Welford's algorithm) computed from `belay.ledger.store.LedgerStore`'s own
`plan_created` events for one session. Deliberately per-session (never
global in-memory state) so two sessions never cross-contaminate each
other's baseline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from belay.ledger.store import LedgerStore


def _upper_bound(count: str | None) -> float | None:
    """Parse an `EffectEstimate.count` string (`"N"` or `"~N"`) into a float, if possible."""
    if count is None:
        return None
    text = count.lstrip("~")
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class Welford:
    """Streaming mean/variance (Welford's online algorithm) -- O(1) memory, one pass."""

    n: int = 0
    mean: float = 0.0
    _m2: float = field(default=0.0, repr=False)

    def update(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self._m2 += delta * (x - self.mean)

    @property
    def variance(self) -> float:
        return self._m2 / self.n if self.n else 0.0

    @property
    def stddev(self) -> float:
        return float(self.variance**0.5)


@dataclass
class BaselineStore:
    """Reads one session's `plan_created` history from the ledger to build a `Welford` baseline."""

    ledger: LedgerStore

    def stats(
        self,
        session_id: str,
        tool: str,
        effect_type: str,
        *,
        exclude_plan_id: str | None = None,
    ) -> Welford:
        """Rolling stats for `(tool, effect_type)` in this session's history only.

        Excludes `exclude_plan_id` -- the plan currently being evaluated -- since
        `Lifecycle.govern_and_execute()` already appends `plan_created` for the
        current plan before `PolicyEngine.evaluate()` runs (spec §3), and a plan
        must never be its own baseline sample.
        """
        welford = Welford()
        for event in self.ledger.read(session_id):
            if event.type != "plan_created":
                continue
            payload = event.payload
            if payload.get("tool") != tool:
                continue
            if exclude_plan_id is not None and payload.get("plan_id") == exclude_plan_id:
                continue
            for effect in payload.get("effects", []) or []:
                if effect.get("type") != effect_type:
                    continue
                value = _upper_bound(effect.get("count"))
                if value is not None:
                    welford.update(value)
        return welford
