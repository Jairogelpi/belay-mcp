"""belay/hooks/gate.py: host-agnostic PreToolUse decision wiring --
classify_bash's verdict routed through the real ApprovalQueue (spec §7),
same one `belay approvals` uses. No mocks of the queue: a real in-memory
SQLite-backed ApprovalQueue, exactly like `tests/approvals/test_queue.py`
uses it, so these tests exercise the actual state machine (pending ->
approved/rejected/expired), not an assumption about it.

Goes through `belay.hooks.claude_code_adapter.normalize()` to build the
`HookEvent` `evaluate()` actually consumes -- these are effectively the same
scenarios `test_gate.py` covered before the host-agnostic refactor, just
exercised through the real normalization path instead of hand-built dicts
shaped like the old raw-payload API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from belay.approvals.queue import ApprovalQueue
from belay.clock import FixedClock
from belay.hooks.claude_code_adapter import normalize
from belay.hooks.gate import evaluate
from belay.supervisor.protocol import HookEvent


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def queue(clock: FixedClock) -> ApprovalQueue:
    return ApprovalQueue(clock=clock)


def _event(command: str, session_id: str = "sess-1", tool_name: str = "Bash") -> HookEvent:
    raw = {
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "tool_use_id": f"toolu_{abs(hash((session_id, tool_name, command)))}",
    }
    return normalize(raw, installation_id="test-install")


def test_safe_command_is_allowed_without_touching_the_queue(queue: ApprovalQueue) -> None:
    result = evaluate(_event("git status"), queue)
    assert result.verdict == "allow"
    assert queue.list() == []


def test_unsafe_command_is_denied_and_queued(queue: ApprovalQueue) -> None:
    result = evaluate(_event("rm -rf /tmp/x"), queue)
    assert result.verdict == "deny"
    items = queue.list()
    assert len(items) == 1
    assert items[0].state == "pending"
    assert items[0].plan["command"] == "rm -rf /tmp/x"
    assert items[0].approval_id == result.approval_id


def test_retrying_the_same_pending_command_does_not_create_a_second_item(
    queue: ApprovalQueue,
) -> None:
    event = _event("rm -rf /tmp/x")
    evaluate(event, queue)
    result = evaluate(event, queue)
    assert result.verdict == "deny"
    assert "still pending" in result.reason
    assert len(queue.list()) == 1


def test_approving_then_retrying_the_same_command_allows_it(queue: ApprovalQueue) -> None:
    event = _event("rm -rf /tmp/x")
    evaluate(event, queue)
    approval_id = queue.list()[0].approval_id
    queue.approve(approval_id, approved_by="jairo")

    result = evaluate(event, queue)
    assert result.verdict == "allow"
    assert "already approved" in result.reason
    assert len(queue.list()) == 1  # still just the one item, never duplicated


def test_rejecting_then_retrying_the_same_command_stays_denied_without_a_new_item(
    queue: ApprovalQueue,
) -> None:
    event = _event("rm -rf /tmp/x")
    evaluate(event, queue)
    approval_id = queue.list()[0].approval_id
    queue.reject(approval_id, rejected_by="jairo", reason="too risky")

    result = evaluate(event, queue)
    assert result.verdict == "deny"
    assert "rejected" in result.reason
    assert "too risky" in result.reason
    assert len(queue.list()) == 1  # never re-queued after an explicit rejection


def test_different_commands_get_independent_approval_items(queue: ApprovalQueue) -> None:
    evaluate(_event("rm -rf /tmp/x"), queue)
    evaluate(_event("rm -rf /tmp/y"), queue)
    assert len(queue.list()) == 2


def test_expired_approval_is_treated_as_absent_and_requeued(
    queue: ApprovalQueue, clock: FixedClock
) -> None:
    event = _event("rm -rf /tmp/x")
    evaluate(event, queue)
    first_id = queue.list()[0].approval_id

    clock.set(clock.now() + timedelta(minutes=31))  # past DEFAULT_EXPIRY (30 min)

    result = evaluate(event, queue)
    assert result.verdict == "deny"
    items = queue.list()
    assert len(items) == 2  # the expired one, plus a fresh pending one
    states = {item.state for item in items}
    assert states == {"expired", "pending"}
    assert items[-1].approval_id != first_id


def test_non_bash_tool_is_denied_as_not_yet_handled(queue: ApprovalQueue) -> None:
    result = evaluate(_event("ignored", tool_name="Edit"), queue)
    assert result.verdict == "deny"
    assert "not yet handled" in result.reason
    assert queue.list() == []


def test_bash_call_missing_command_field_is_denied(queue: ApprovalQueue) -> None:
    raw = {
        "session_id": "s",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {},
        "tool_use_id": "toolu_missing_command",
    }
    result = evaluate(normalize(raw, installation_id="test-install"), queue)
    assert result.verdict == "deny"


def test_approval_item_carries_the_hook_payloads_session_id(queue: ApprovalQueue) -> None:
    evaluate(_event("rm -rf /tmp/x", session_id="claude-session-abc"), queue)
    assert queue.list()[0].session_id == "claude-session-abc"


def test_post_phase_event_is_denied_not_crashed(queue: ApprovalQueue) -> None:
    raw = {
        "session_id": "s",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "toolu_post",
    }
    result = evaluate(normalize(raw, installation_id="test-install"), queue)
    assert result.verdict == "deny"
    assert "post-phase" in result.reason


def test_missing_event_id_is_denied_as_ambiguous(queue: ApprovalQueue) -> None:
    raw = {
        "session_id": "s",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        # no tool_use_id at all
    }
    result = evaluate(normalize(raw, installation_id="test-install"), queue)
    assert result.verdict == "deny"
    assert "ambiguous identity" in result.reason
