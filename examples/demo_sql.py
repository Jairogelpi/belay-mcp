"""E11 demo (docs/plan-v2.md "E11 -- Real SQL dry-run adapter"): a real row count.

Loads the real `crm.bulk_delete` contract from `examples/contracts/crm.yaml`
(its `sql` hint added in E11), seeds a real SQLite database with real rows,
and runs `belay.planner.planner.Planner` against it with a real
`sql_runner` (`belay.planner.adapters.sql.make_sql_runner`). The resulting
`Plan` carries a `count` that is not a guess -- it comes from a real
`BEGIN; DELETE ...; ROLLBACK` against the database -- and is shown, still
paused for human approval by the real `PolicyEngine`, before anything is
ever committed.

    $ python examples/demo_sql.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from belay.contracts.loader import load_contract_set
from belay.planner.adapters.sql import make_sql_runner
from belay.planner.model import PlanningSession
from belay.planner.planner import Planner
from belay.policy.engine import PolicyEngine
from belay.policy.model import Cap, CapMatch, Defaults, PolicyDoc
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_ID = "demo-sql-session"


def _seed_database(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE records (id INTEGER PRIMARY KEY, last_seen INTEGER)"))
        for _ in range(342):
            conn.execute(text("INSERT INTO records (last_seen) VALUES (2024)"))  # fresh
        for _ in range(214):
            conn.execute(text("INSERT INTO records (last_seen) VALUES (2020)"))  # genuinely stale


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="belay-demo-sql-"))
    db_path = tmp / "crm.db"
    _seed_database(db_path)
    engine = create_engine(f"sqlite:///{db_path}")

    contract_set = load_contract_set([REPO_ROOT / "examples" / "contracts" / "crm.yaml"])
    contract = contract_set.resolve("crm.bulk_delete")
    assert contract is not None and contract.sql is not None, "crm.yaml must declare the sql hint"

    planner = Planner()
    session = PlanningSession(
        session_id=SESSION_ID, contract=contract, sql_runner=make_sql_runner(engine)
    )

    print('# agent: "clean stale records" -> crm.bulk_delete(before_year=2023)')
    plan = await planner.plan("crm.bulk_delete", {"before_year": 2023}, session)
    effect = plan.effects[0]
    print(f"  plan basis: {effect.basis}  (not a guess -- a real ROLLBACK'd DELETE)")
    print(f"  plan count: {effect.count}  estimate={effect.estimate}")
    assert effect.basis == "sql_simulator"
    assert effect.estimate is False
    assert effect.count == "214"  # the real, exact count of last_seen < 2023 rows

    policy = PolicyDoc(
        defaults=Defaults(),
        caps=[
            Cap(match=CapMatch(effect="delete", resource="crm.*"), max_count=100, over="pause")
        ],
    )
    result = PolicyEngine().evaluate(plan, policy)
    print(f"\n  policy verdict: {result.verdict}  (214 > cap of 100)")
    for reason in result.reasons:
        print(f"    reason: {reason}")
    assert result.verdict == "pause"
    assert result.requires_approval is True

    with engine.connect() as conn:
        total_after = conn.execute(text("SELECT COUNT(*) FROM records")).scalar_one()
    print(f"\n  db rows after dry-run: {total_after} (556 seeded -- nothing was ever committed)")
    assert total_after == 556

    print("\nreal row count, paused for human approval, DB provably untouched -- demo complete.")


if __name__ == "__main__":
    asyncio.run(main())
