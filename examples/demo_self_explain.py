"""E16 demo (docs/plan-v2.md "E16 -- Blast-radius self-explanation returned to the agent"):
the "wow, it caught itself" moment -- an agent reads its OWN pause reason and fixes it,
with no human approval step anywhere in this script.

Runs the real `belay.proxy.lifecycle.Lifecycle` / `belay.policy.engine.PolicyEngine` /
`belay.policy.explain.explain` against a real `belay.ledger.store.LedgerStore` -- no
mocked verdicts, no real LLM (the "agent" below is a scripted Python function, same
spirit as `examples/demo_quota.py`/`demo_sql.py`). A real `native_dry_run` adapter
counts real rows in an in-memory CRM dict -- the 500/80 numbers below are computed,
never hardcoded twice.

    1. agent calls crm.bulk_delete(before_year=2030) -> real count 500 -> a Cap of 100
       fires -> `pending_approval`, with a full `Explanation` inline.
    2. the SAME script reads `explanation["suggested_action"]`, which names
       `args.before_year` as the tool's declared narrowing argument (from the
       contract's `sql.params` -- E11 -- referencing `$args.before_year`).
    3. the agent narrows and resubmits -- crm.bulk_delete(before_year=2023) -> real
       count 80, under the cap -> `allow`, executes. No human in the loop.

    $ python examples/demo_self_explain.py
"""

from __future__ import annotations

import re

import anyio
from belay.contracts.model import Contract, ContractSet, SqlHint, Undo
from belay.contracts.model import Effect as ContractEffect
from belay.ledger.store import LedgerStore
from belay.policy.model import Cap, CapMatch, PolicyDoc
from belay.proxy.lifecycle import Lifecycle

SESSION_ID = "demo-self-explain-session"
CAP_MAX = 100

# A real in-memory "CRM": 80 genuinely stale records, 420 fresh ones.
RECORDS = {f"stale-{i}": 2022 for i in range(80)} | {f"fresh-{i}": 2024 for i in range(420)}


def _contract_set() -> ContractSet:
    contract = Contract(
        belay_contract="0.1",
        tool="crm.bulk_delete",
        reversibility="reversible",
        undo=Undo(tool="crm.import_records", args={}),
        effects=[ContractEffect(type="delete", resource="crm.record")],
        # E11's sql hint declares which argument narrows this call's scope --
        # explain()'s suggested_action reads this mechanically, never guesses it.
        sql=SqlHint(
            statement="DELETE FROM records WHERE last_seen < :cutoff",
            params={"cutoff": "$args.before_year"},
        ),
    )
    return ContractSet(contracts={"crm.bulk_delete": contract}, set_hash="sha256:demo-e16")


async def _native_dry_run(tool: str, args: dict) -> dict | None:
    """A real dry-run adapter: counts real matching rows, no sql_runner needed for this demo."""
    if tool != "crm.bulk_delete":
        return None
    cutoff = args["before_year"]
    count = sum(1 for last_seen in RECORDS.values() if last_seen < cutoff)
    return {"effects": [{"type": "delete", "resource": "crm.record", "count": str(count)}]}


async def _executor(tool: str, args: dict) -> dict:
    cutoff = args["before_year"]
    deleted = [k for k, v in RECORDS.items() if v < cutoff]
    for k in deleted:
        del RECORDS[k]
    return {"ok": True, "deleted": deleted}


def _extract_narrowing_arg(suggested_action: str) -> str:
    """Parse the arg name explain() named, e.g. "narrow `args.before_year` and re-plan..."."""
    match = re.search(r"`args\.([A-Za-z0-9_.]+)`", suggested_action)
    assert match is not None, suggested_action
    return match.group(1)


async def main() -> None:
    ledger = LedgerStore()
    policy = PolicyDoc(
        caps=[
            Cap(
                match=CapMatch(effect="delete", resource="crm.record"),
                max_count=CAP_MAX,
                over="pause",
            )
        ]
    )
    lifecycle = Lifecycle(
        contract_set=_contract_set(),
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id=SESSION_ID,
        policy=policy,
        native_dry_run=_native_dry_run,
    )
    lifecycle.start_session("agent-bot")

    print(f"# {len(RECORDS)} real records seeded; cap = {CAP_MAX} deletes / call")
    print('# agent: "clean stale records" -> crm.bulk_delete(before_year=2030)')
    result = await lifecycle.govern_and_execute(
        "crm.bulk_delete", {"before_year": 2030}, read_only_hint=False, executor=_executor
    )
    assert result["status"] == "pending_approval", result
    explanation = result["explanation"]
    print(f"  -> pending_approval (verdict={explanation['verdict']!r})")
    print(f"  headline: {explanation['headline']}")
    assert explanation["suggested_action"] is not None, "no deterministic suggestion offered"
    print(f"  suggested_action: {explanation['suggested_action']}")

    # --- the "wow" moment: the agent self-corrects from the Explanation alone ---
    narrow_arg = _extract_narrowing_arg(explanation["suggested_action"])
    assert narrow_arg == "before_year"
    print(f"\n# agent: reads suggested_action, narrows `{narrow_arg}` -> 2023, re-plans")
    retry = await lifecycle.govern_and_execute(
        "crm.bulk_delete", {"before_year": 2023}, read_only_hint=False, executor=_executor
    )

    assert "status" not in retry or retry.get("status") != "pending_approval", retry
    print(f"  -> executed, no human approval: deleted {len(retry['deleted'])} records")
    assert len(retry["deleted"]) == 80

    print(
        "\nagent paused itself, read WHY from the same MCP response, narrowed, and "
        "resubmitted to `allow` -- zero human approval steps. Demo complete."
    )


if __name__ == "__main__":
    anyio.run(main)
