"""E15 demo (docs/plan-v2.md "E15 -- Per-identity irreversible-action quota"):
"I approved one bulk-delete, I did not approve the agent doing that 200 times."

Runs the real `belay.proxy.lifecycle.Lifecycle` / `belay.policy.engine.PolicyEngine`
/ `belay.policy.quota.QuotaTracker` against a real `belay.ledger.store.LedgerStore`
-- no mocked verdicts. There is deliberately **no per-call `Cap`** configured for
`mail.send` (E15's whole point: quota is a per-identity rolling budget, not a
per-call blast-radius limit -- E4's `Cap` still exists and composes with this,
it isn't replaced). The irreversible default is relaxed to `allow` for
`mail.send` so calls execute normally up to the identity's quota; the
`quota` dimension alone pauses the identity once it crosses its configured
`max_irreversible_actions` within the rolling window.

    $ python examples/demo_quota.py
"""

from __future__ import annotations

import anyio
from belay.contracts.model import Contract, ContractSet, Effect
from belay.ledger.store import LedgerStore
from belay.policy.model import Defaults, PolicyDoc, QuotaDefaults, ToolRule
from belay.proxy.lifecycle import Lifecycle

SESSION_ID = "demo-quota-session"
IDENTITY = "agent-bot"
MAX_IRREVERSIBLE = 3


def _contract_set() -> ContractSet:
    contract = Contract(
        belay_contract="0.1",
        tool="mail.send",
        reversibility="irreversible",
        effects=[Effect(type="send", resource="email.message", count="1")],
    )
    return ContractSet(contracts={"mail.send": contract}, set_hash="sha256:demo-quota")


def _policy() -> PolicyDoc:
    return PolicyDoc(
        # Relax the irreversible default: without this, every mail.send would
        # already pause on its own (spec §6.4) and quota would never get a
        # chance to be the thing that fires.
        tools=[ToolRule(match="mail.send", verdict="allow")],
        defaults=Defaults(
            quota=QuotaDefaults(
                enabled=True, window="1d", max_irreversible_actions=MAX_IRREVERSIBLE
            )
        ),
    )


async def _executor(tool: str, args: dict) -> dict:
    return {"ok": True, "tool": tool}


async def main() -> None:
    ledger = LedgerStore()
    policy = _policy()
    assert policy.caps == [], "no per-call Cap configured for mail.send -- the whole point"

    lifecycle = Lifecycle(
        contract_set=_contract_set(),
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id=SESSION_ID,
        policy=policy,
    )
    lifecycle.start_session(IDENTITY)

    print(f"# identity {IDENTITY!r}: quota = {MAX_IRREVERSIBLE} irreversible actions / 1 day")
    for i in range(MAX_IRREVERSIBLE + 1):
        result = await lifecycle.govern_and_execute(
            "mail.send", {"to": f"user{i}@example.com"}, read_only_hint=False, executor=_executor
        )
        status = result.get("status", "executed")
        print(f"  send {i}: {status}")
        if i < MAX_IRREVERSIBLE:
            assert status != "pending_approval"
        else:
            assert status == "pending_approval"

    events = ledger.read(SESSION_ID)
    last_eval = [e for e in events if e.type == "policy_evaluated"][-1]
    print("\nreasons for the final call:")
    for reason in last_eval.payload["reasons"]:
        print(f"  {reason}")

    print(
        f"\n{IDENTITY!r} paused after {MAX_IRREVERSIBLE + 1}th irreversible action -- "
        "purely by E15 quota, no per-call Cap involved for mail.send. Demo complete."
    )


if __name__ == "__main__":
    anyio.run(main)
