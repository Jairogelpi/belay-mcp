"""E10 demo (docs/plan-v2.md "E10 -- Statistical anomaly baselines"): "me salvo solo".

Runs the real `belay.policy.engine.PolicyEngine` / `belay.policy.baseline`
against a real `belay.ledger.store.LedgerStore` -- no mocked verdicts. There
is deliberately **no `Cap`** configured anywhere for `crm.bulk_delete` (see
`examples/contracts/crm.yaml`, `default_policy()`): a hand-set threshold
would defeat the point. The agent makes 12 normal ~10-row bulk deletes
(building its own baseline for free, spec §9.1's own ledger history), then
one 500-row bulk delete -- 50x its own trailing average -- and the anomaly
dimension pauses it on its own, explaining exactly why.

    $ python examples/demo_anomaly.py
"""

from __future__ import annotations

from belay.ledger.store import LedgerStore
from belay.planner.model import EffectEstimate, Plan
from belay.policy.engine import PolicyEngine
from belay.policy.model import default_policy

SESSION_ID = "demo-anomaly-session"


def _plan(plan_id: str, count: str) -> Plan:
    return Plan(
        plan_id=plan_id,
        session_id=SESSION_ID,
        tool="crm.bulk_delete",
        args={"before_year": 2023},
        effects=[EffectEstimate(type="delete", resource="crm.record", count=count, estimate=True)],
        reversibility="reversible",
        confidence="medium",
        created_at="2026-07-22T12:00:00+00:00",
        expires_at="2026-07-22T12:10:00+00:00",
    )


def main() -> None:
    ledger = LedgerStore()  # in-memory SQLite; a real run uses the session's own DB file
    engine = PolicyEngine(ledger=ledger)
    policy = default_policy()
    assert policy.caps == [], "no Cap configured anywhere -- the whole point"

    print("# agent: 12 routine bulk_delete calls, ~10 rows each (building its own baseline)")
    for i in range(12):
        plan = _plan(f"p{i}", count=str(10 + (i % 3)))
        ledger.append(plan.session_id, "plan_created", plan.model_dump(mode="json"))
        result = engine.evaluate(plan, policy)
        print(f"  call {i}: count={10 + (i % 3):>3} -> verdict={result.verdict}")
        assert result.verdict == "allow"

    print("\n# agent: one bulk_delete estimating 500 rows (50x its own normal behavior)")
    outlier = _plan("p_outlier", count="500")
    ledger.append(outlier.session_id, "plan_created", outlier.model_dump(mode="json"))
    result = engine.evaluate(outlier, policy)
    print(f"  call outlier: count=500 -> verdict={result.verdict}")
    for reason in result.reasons:
        print(f"    reason: {reason}")

    assert result.verdict == "pause"
    assert result.requires_approval is True
    print("\ncaught with zero manual thresholds configured -- demo complete.")


if __name__ == "__main__":
    main()
