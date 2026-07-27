"""belay/hooks/gate.py: PreToolUse decision wiring -- classify_bash's verdict
routed through the real ApprovalQueue (spec §7), same one `belay approvals`
uses. No mocks of the queue: a real in-memory SQLite-backed ApprovalQueue,
exactly like `tests/approvals/test_queue.py` uses it, so these tests exercise
the actual state machine (pending -> approved/rejected/expired), not an
assumption about it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from belay.approvals.queue import ApprovalQueue
from belay.clock import FixedClock
from belay.hooks.gate import handle_pre_tool_use


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def queue(clock: FixedClock) -> ApprovalQueue:
    return ApprovalQueue(clock=clock)


def _payload(command: str, session_id: str = "sess-1", tool_name: str = "Bash") -> dict:
    return {
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "tool_use_id": "toolu_test",
    }


def test_safe_command_is_allowed_without_touching_the_queue(queue: ApprovalQueue) -> None:
    result = handle_pre_tool_use(_payload("git status"), queue)
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert queue.list() == []


def test_unsafe_command_is_denied_and_queued(queue: ApprovalQueue) -> None:
    result = handle_pre_tool_use(_payload("rm -rf /tmp/x"), queue)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    items = queue.list()
    assert len(items) == 1
    assert items[0].state == "pending"
    assert items[0].plan["command"] == "rm -rf /tmp/x"
    assert items[0].approval_id in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_retrying_the_same_pending_command_does_not_create_a_second_item(
    queue: ApprovalQueue,
) -> None:
    handle_pre_tool_use(_payload("rm -rf /tmp/x"), queue)
    result = handle_pre_tool_use(_payload("rm -rf /tmp/x"), queue)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "still pending" in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert len(queue.list()) == 1


def test_approving_then_retrying_the_same_command_allows_it(queue: ApprovalQueue) -> None:
    handle_pre_tool_use(_payload("rm -rf /tmp/x"), queue)
    approval_id = queue.list()[0].approval_id
    queue.approve(approval_id, approved_by="jairo")

    result = handle_pre_tool_use(_payload("rm -rf /tmp/x"), queue)
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "already approved" in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert len(queue.list()) == 1  # still just the one item, never duplicated


def test_rejecting_then_retrying_the_same_command_stays_denied_without_a_new_item(
    queue: ApprovalQueue,
) -> None:
    handle_pre_tool_use(_payload("rm -rf /tmp/x"), queue)
    approval_id = queue.list()[0].approval_id
    queue.reject(approval_id, rejected_by="jairo", reason="too risky")

    result = handle_pre_tool_use(_payload("rm -rf /tmp/x"), queue)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "rejected" in reason
    assert "too risky" in reason
    assert len(queue.list()) == 1  # never re-queued after an explicit rejection


def test_different_commands_get_independent_approval_items(queue: ApprovalQueue) -> None:
    handle_pre_tool_use(_payload("rm -rf /tmp/x"), queue)
    handle_pre_tool_use(_payload("rm -rf /tmp/y"), queue)
    assert len(queue.list()) == 2


def test_expired_approval_is_treated_as_absent_and_requeued(
    queue: ApprovalQueue, clock: FixedClock
) -> None:
    handle_pre_tool_use(_payload("rm -rf /tmp/x"), queue)
    first_id = queue.list()[0].approval_id

    clock.set(clock.now() + timedelta(minutes=31))  # past DEFAULT_EXPIRY (30 min)

    result = handle_pre_tool_use(_payload("rm -rf /tmp/x"), queue)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    items = queue.list()
    assert len(items) == 2  # the expired one, plus a fresh pending one
    states = {item.state for item in items}
    assert states == {"expired", "pending"}
    assert items[-1].approval_id != first_id


def test_non_bash_tool_is_denied_as_not_yet_handled(queue: ApprovalQueue) -> None:
    result = handle_pre_tool_use(_payload("ignored", tool_name="Edit"), queue)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "not yet handled" in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert queue.list() == []


def test_bash_call_missing_command_field_is_denied() -> None:
    queue = ApprovalQueue()
    payload = {"session_id": "s", "tool_name": "Bash", "tool_input": {}}
    result = handle_pre_tool_use(payload, queue)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_approval_item_carries_the_hook_payloads_session_id(queue: ApprovalQueue) -> None:
    handle_pre_tool_use(_payload("rm -rf /tmp/x", session_id="claude-session-abc"), queue)
    assert queue.list()[0].session_id == "claude-session-abc"
