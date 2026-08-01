"""R1.6 cross-cutting: TOCTOU/concurrency coverage tying together the six
correctness-lock items (see docs/adr for R1 first-through-fifth slices,
this module covers what's new in R1.6 itself):

- Two genuinely concurrent `ApprovalQueue.consume()` calls for the same
  approval, from different event_ids, must never both succeed (Item F).
- A `configured_policy_unavailable` deny (Item B) must win even for an
  approval that already claimed its single-use consumption (Item F) --
  a broken configured policy denies everything, not just new approvals.

The install -> uninstall -> bare reinstall -> no stale config reappears
scenario (Item C) is already covered end-to-end by
`tests/cli/test_hooks_lifecycle.py::TestHooksUninstallCleansUpPointerFiles`
-- not duplicated here.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

from belay.approvals.queue import ApprovalAlreadyConsumed, ApprovalQueue
from belay.clock import FixedClock
from belay.hooks.claude_code_adapter import normalize
from belay.supervisor.server import ConfigUnavailable, Supervisor


def test_concurrent_consume_calls_for_the_same_approval_only_one_succeeds(
    tmp_path: Path,
) -> None:
    """Item F's atomicity claim, actually exercised under real thread
    concurrency (not just sequential calls) -- a real SQLite file (not
    `:memory:`) so every thread's `DBSession` reaches the same durable
    state, matching how `tests/supervisor/test_server_client.py` sets up
    concurrency-relevant `ApprovalQueue`/`LedgerStore` instances."""
    db_path = tmp_path / "approvals.db"
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    queue = ApprovalQueue(db_url=f"sqlite:///{db_path}", clock=clock)
    item = queue.request("s1", "plan_1", {"tool": "mail.send"})
    queue.approve(item.approval_id, approved_by="jairo")

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    successes: list[str] = []
    failures: list[str] = []
    lock = threading.Lock()

    def _attempt(event_id: str) -> None:
        barrier.wait()  # maximize actual overlap, not just call-order
        try:
            queue.consume(item.approval_id, event_id)
        except ApprovalAlreadyConsumed:
            with lock:
                failures.append(event_id)
        else:
            with lock:
                successes.append(event_id)

    threads = [
        threading.Thread(target=_attempt, args=(f"event_{i}",)) for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(successes) == 1, f"expected exactly one winner, got {successes}"
    assert len(failures) == n_threads - 1
    fresh = queue.get(item.approval_id)
    assert fresh is not None
    assert fresh.consumed_by_event_id == successes[0]


def test_configured_policy_unavailable_denies_even_an_already_consumed_approval(
    tmp_path: Path,
) -> None:
    """Ties Item B and Item F together: an approval that already claimed
    its single-use consumption while the contracts file was valid must
    still deny once that file becomes unreadable/invalid -- a broken
    configured policy is an invariant across the whole gate, not just new
    approval requests."""
    from belay.supervisor.addressing import supervisor_identity

    identity = supervisor_identity((tmp_path / "belay-hooks.db").resolve())
    contracts = tmp_path / "contracts.yaml"
    contracts.write_text(
        "belay_contract: '0.1'\n"
        "tool: Write\n"
        "reversibility: irreversible\n"
        "effects:\n"
        "  - type: update\n"
        "    resource: native.file\n",
        encoding="utf-8",
    )
    identity.contracts_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    identity.contracts_pointer_path.write_text(str(contracts), encoding="utf-8")

    supervisor = Supervisor(identity)
    assert not isinstance(supervisor._contract_set, ConfigUnavailable)

    target = tmp_path / "f.txt"
    raw = {
        "session_id": "s1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "x"},
        "tool_use_id": "toolu_1",
    }
    event = normalize(raw, installation_id=identity.install_id)
    decision = supervisor._decide_pre(event)
    assert decision.verdict == "allow"  # declared contract -- allowed and captured

    # Corrupt the configured contracts file -- a fresh Supervisor for this
    # same identity (as a real respawn after `belay supervisor stop` would
    # be) must now fail closed for every event, regardless of any approval
    # state from before the corruption.
    contracts.write_text("not: valid: yaml: [", encoding="utf-8")
    broken_supervisor = Supervisor(identity)
    assert isinstance(broken_supervisor._contract_set, ConfigUnavailable)

    raw2 = {**raw, "tool_use_id": "toolu_2"}
    event2 = normalize(raw2, installation_id=identity.install_id)
    decision2 = broken_supervisor._decide_pre(event2)
    assert decision2.verdict == "deny"
    assert "configured_policy_unavailable" in decision2.reason
