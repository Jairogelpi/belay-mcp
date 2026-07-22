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
    """spec §12 approver binding: re-planning the same logical call produces
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
