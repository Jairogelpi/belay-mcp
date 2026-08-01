"""Approval queue: states, transitions, expiration (spec §7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from belay.approvals.queue import ApprovalQueue
from belay.clock import FixedClock
from belay.errors import BelayError


def _clock(at: datetime | None = None) -> FixedClock:
    return FixedClock(at or datetime(2026, 1, 1, tzinfo=UTC))


def test_approve_transitions_pending_to_approved() -> None:
    """@spec("7.2.1") — the approving principal MUST be recorded (approved_by)."""
    queue = ApprovalQueue(clock=_clock())
    item = queue.request("s1", "plan_1", {"tool": "mail.send"})
    approved = queue.approve(item.approval_id, approved_by="jairo")
    assert approved.state == "approved"
    assert approved.approved_by == "jairo"


def test_reject_transitions_pending_to_rejected_with_reason() -> None:
    queue = ApprovalQueue(clock=_clock())
    item = queue.request("s1", "plan_1", {"tool": "mail.send"})
    rejected = queue.reject(item.approval_id, rejected_by="jairo", reason="too risky")
    assert rejected.state == "rejected"
    assert rejected.reason == "too risky"


def test_transitions_are_unidirectional_approved_cannot_be_rejected_or_reapproved() -> None:
    queue = ApprovalQueue(clock=_clock())
    item = queue.request("s1", "plan_1", {"tool": "mail.send"})
    queue.approve(item.approval_id, approved_by="a")

    with pytest.raises(ValueError):
        queue.reject(item.approval_id, rejected_by="b")
    with pytest.raises(ValueError):
        queue.approve(item.approval_id, approved_by="c")


def test_transitions_are_unidirectional_rejected_cannot_be_approved_or_rerejected() -> None:
    queue = ApprovalQueue(clock=_clock())
    item = queue.request("s1", "plan_1", {"tool": "mail.send"})
    queue.reject(item.approval_id, rejected_by="a")

    with pytest.raises(ValueError):
        queue.approve(item.approval_id, approved_by="b")
    with pytest.raises(ValueError):
        queue.reject(item.approval_id, rejected_by="c")


def test_expired_item_is_never_executable_via_approve() -> None:
    """@spec("7.1") — an expired approval item MUST NOT be executable."""
    clock = _clock()
    queue = ApprovalQueue(clock=clock)
    item = queue.request("s1", "plan_1", {"tool": "mail.send"}, expiry=timedelta(minutes=1))

    clock.set(item.requested_at + timedelta(minutes=2))
    with pytest.raises(BelayError) as excinfo:
        queue.approve(item.approval_id, approved_by="a")
    assert excinfo.value.code == "approval_expired"

    fetched = queue.get(item.approval_id)
    assert fetched is not None
    assert fetched.state == "expired"


def test_exact_tie_between_approval_and_expiration_expiration_wins() -> None:
    """spec §7.1: an expired item MUST NOT be executable, even if the
    approval and the expiration happen at the exact same instant."""
    clock = _clock()
    queue = ApprovalQueue(clock=clock)
    item = queue.request("s1", "plan_1", {"tool": "mail.send"}, expiry=timedelta(minutes=1))

    # Force "now" to be exactly `expires_at`, the race the spec calls out.
    clock.set(item.expires_at)
    with pytest.raises(BelayError) as excinfo:
        queue.approve(item.approval_id, approved_by="a")
    assert excinfo.value.code == "approval_expired"


def test_list_and_for_plan_lazily_reflect_expiration() -> None:
    clock = _clock()
    queue = ApprovalQueue(clock=clock)
    item = queue.request("s1", "plan_1", {"tool": "mail.send"}, expiry=timedelta(minutes=1))
    clock.set(item.expires_at + timedelta(seconds=1))

    [listed] = queue.list()
    assert listed.state == "expired"
    assert queue.for_plan("plan_1") is not None
    assert queue.for_plan("plan_1").state == "expired"  # type: ignore[union-attr]


def test_approval_item_is_bound_to_its_plan_id_and_replanning_invalidates_it() -> None:
    """@spec("12.3") spec §12 approver binding: re-planning the same logical call produces
    a new `plan_id`; the old approval item is bound to the old `plan_id` and
    is never surfaced for the new one, even after being approved."""
    queue = ApprovalQueue(clock=_clock())
    old_item = queue.request("s1", "plan_v1", {"tool": "crm.bulk_delete", "count": "~512"})
    queue.approve(old_item.approval_id, approved_by="jairo")

    # A re-plan (narrower filter) gets a new plan_id -- nothing is found for
    # it, even though the old plan's item is `approved`.
    assert queue.for_plan("plan_v2_narrowed") is None

    # The old item is untouched and still only bound to the old plan.
    stale = queue.get(old_item.approval_id)
    assert stale is not None
    assert stale.plan_id == "plan_v1"
    assert stale.state == "approved"


def test_approve_unknown_approval_id_raises() -> None:
    queue = ApprovalQueue(clock=_clock())
    with pytest.raises(BelayError) as excinfo:
        queue.approve("ap_does_not_exist", approved_by="a")
    assert excinfo.value.code == "approval_expired"


class TestConsume:
    """R1.6: `ApprovalQueue.consume()` -- closes the gap where an
    `approved` item allowed an unbounded number of separate future action
    instances (a new `event_id`, not merely the host redelivering the
    exact same PreToolUse dispatch) to reuse one human decision forever.
    """

    def test_first_consumption_claims_the_approval(self) -> None:
        queue = ApprovalQueue(clock=_clock())
        item = queue.request("s1", "plan_1", {"tool": "mail.send"})
        queue.approve(item.approval_id, approved_by="jairo")

        consumed = queue.consume(item.approval_id, "event_1")
        assert consumed.consumed_by_event_id == "event_1"
        assert consumed.consumed_at is not None

    def test_same_event_id_retried_is_idempotent(self) -> None:
        """The host redelivering the identical PreToolUse dispatch (a real
        occurrence in these protocols) must not be treated as a reuse
        attempt."""
        queue = ApprovalQueue(clock=_clock())
        item = queue.request("s1", "plan_1", {"tool": "mail.send"})
        queue.approve(item.approval_id, approved_by="jairo")

        first = queue.consume(item.approval_id, "event_1")
        second = queue.consume(item.approval_id, "event_1")
        assert first.consumed_by_event_id == second.consumed_by_event_id == "event_1"

    def test_host_and_policy_hash_are_recorded_on_first_consumption(self) -> None:
        """Post-R1.6 review: a step toward a fuller Capability Lease --
        which adapter/audience actually consumed the grant, and under
        what policy config, recorded for audit."""
        queue = ApprovalQueue(clock=_clock())
        item = queue.request("s1", "plan_1", {"tool": "mail.send"})
        queue.approve(item.approval_id, approved_by="jairo")

        consumed = queue.consume(
            item.approval_id, "event_1", host="claude-code", policy_hash="decision_logic=1"
        )
        assert consumed.consumed_by_host == "claude-code"
        assert consumed.consumed_policy_hash == "decision_logic=1"

    def test_host_and_policy_hash_are_not_overwritten_by_a_same_event_id_retry(self) -> None:
        """A retry of the same event_id returns the ORIGINAL recorded
        host/policy_hash, not whatever the retry happened to pass --
        the record reflects first consumption, not the latest call."""
        queue = ApprovalQueue(clock=_clock())
        item = queue.request("s1", "plan_1", {"tool": "mail.send"})
        queue.approve(item.approval_id, approved_by="jairo")

        queue.consume(item.approval_id, "event_1", host="claude-code", policy_hash="v1")
        retried = queue.consume(item.approval_id, "event_1", host="codex", policy_hash="v2")
        assert retried.consumed_by_host == "claude-code"
        assert retried.consumed_policy_hash == "v1"

    def test_host_and_policy_hash_default_to_none(self) -> None:
        queue = ApprovalQueue(clock=_clock())
        item = queue.request("s1", "plan_1", {"tool": "mail.send"})
        queue.approve(item.approval_id, approved_by="jairo")

        consumed = queue.consume(item.approval_id, "event_1")
        assert consumed.consumed_by_host is None
        assert consumed.consumed_policy_hash is None

    def test_a_different_event_id_is_refused(self) -> None:
        from belay.approvals.queue import ApprovalAlreadyConsumed

        queue = ApprovalQueue(clock=_clock())
        item = queue.request("s1", "plan_1", {"tool": "mail.send"})
        queue.approve(item.approval_id, approved_by="jairo")

        queue.consume(item.approval_id, "event_1")
        with pytest.raises(ApprovalAlreadyConsumed):
            queue.consume(item.approval_id, "event_2")

    def test_consuming_a_still_pending_item_is_refused(self) -> None:
        from belay.approvals.queue import ApprovalNotConsumable

        queue = ApprovalQueue(clock=_clock())
        item = queue.request("s1", "plan_1", {"tool": "mail.send"})
        with pytest.raises(ApprovalNotConsumable):
            queue.consume(item.approval_id, "event_1")

    def test_consuming_a_rejected_item_is_refused(self) -> None:
        from belay.approvals.queue import ApprovalNotConsumable

        queue = ApprovalQueue(clock=_clock())
        item = queue.request("s1", "plan_1", {"tool": "mail.send"})
        queue.reject(item.approval_id, rejected_by="jairo")
        with pytest.raises(ApprovalNotConsumable):
            queue.consume(item.approval_id, "event_1")

    def test_consuming_an_unknown_approval_id_is_refused(self) -> None:
        from belay.approvals.queue import ApprovalNotConsumable

        queue = ApprovalQueue(clock=_clock())
        with pytest.raises(ApprovalNotConsumable):
            queue.consume("ap_does_not_exist", "event_1")
