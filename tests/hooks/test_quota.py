"""belay/hooks/quota.py::HookQuotaTracker -- R1 fourth slice (ADR 0023):
per-OS-user rolling quota of approved hook-gated actions. Uses a real
`LedgerStore`, never global state, same rigor as `tests/policy/test_quota.py`
for E15's MCP-path tracker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from belay.clock import FixedClock
from belay.db.models import EventRow
from belay.hooks.quota import HookQuotaTracker, QuotaConfig
from belay.ledger.store import LedgerStore
from sqlalchemy import update
from sqlalchemy.orm import Session as DBSession

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)


def _set_event_at(ledger: LedgerStore, event_id: str, at: datetime) -> None:
    """Test-only: backdate one event's `at` for deterministic window-boundary
    tests -- production code never rewrites a ledger row."""
    with DBSession(ledger.engine) as db:
        db.execute(update(EventRow).where(EventRow.event_id == event_id).values(at=at.isoformat()))
        db.commit()


def _seed_approved_paused_action(
    ledger: LedgerStore, *, os_user: str, approval_id: str, at: datetime, session_id: str = "s1"
) -> None:
    ev = ledger.append(
        session_id,
        "hook_pre_tool_use",
        {"os_user": os_user, "verdict": "deny", "approval_id": approval_id},
    )
    _set_event_at(ledger, ev.event_id, at)
    ledger.append(
        session_id, "approval_resolved", {"approval_id": approval_id, "state": "approved"}
    )


def _seed_unapproved_paused_action(
    ledger: LedgerStore, *, os_user: str, approval_id: str, at: datetime, session_id: str = "s1"
) -> None:
    """Still pending, or rejected -- never resolved to 'approved'."""
    ev = ledger.append(
        session_id,
        "hook_pre_tool_use",
        {"os_user": os_user, "verdict": "deny", "approval_id": approval_id},
    )
    _set_event_at(ledger, ev.event_id, at)


class TestHookQuotaTracker:
    def test_no_events_counts_zero(self) -> None:
        ledger = LedgerStore()
        tracker = HookQuotaTracker(ledger)
        assert tracker.count("alice", now=NOW, window=timedelta(days=1)) == 0

    def test_counts_only_approved_actions(self) -> None:
        ledger = LedgerStore()
        _seed_approved_paused_action(ledger, os_user="alice", approval_id="ap1", at=NOW)
        _seed_unapproved_paused_action(ledger, os_user="alice", approval_id="ap2", at=NOW)
        tracker = HookQuotaTracker(ledger)
        assert tracker.count("alice", now=NOW, window=timedelta(days=1)) == 1

    def test_never_counts_a_different_os_user(self) -> None:
        ledger = LedgerStore()
        _seed_approved_paused_action(ledger, os_user="alice", approval_id="ap1", at=NOW)
        tracker = HookQuotaTracker(ledger)
        assert tracker.count("bob", now=NOW, window=timedelta(days=1)) == 0

    def test_never_counts_an_allowed_event_even_with_an_approval_id(self) -> None:
        """An 'allow' verdict (e.g. a declared read-only contract, R1 second
        slice) never reached the approval queue at all -- must not be
        miscounted as an approved pause just because some approval_id
        happens to exist elsewhere in the ledger."""
        ledger = LedgerStore()
        ev = ledger.append(
            "s1", "hook_pre_tool_use", {"os_user": "alice", "verdict": "allow", "approval_id": None}
        )
        _set_event_at(ledger, ev.event_id, NOW)
        tracker = HookQuotaTracker(ledger)
        assert tracker.count("alice", now=NOW, window=timedelta(days=1)) == 0

    def test_action_just_outside_window_does_not_count(self) -> None:
        ledger = LedgerStore()
        window = timedelta(days=1)
        _seed_approved_paused_action(
            ledger, os_user="alice", approval_id="ap1", at=NOW - window - timedelta(seconds=1)
        )
        tracker = HookQuotaTracker(ledger)
        assert tracker.count("alice", now=NOW, window=window) == 0

    def test_action_exactly_at_window_boundary_still_counts(self) -> None:
        ledger = LedgerStore()
        window = timedelta(days=1)
        _seed_approved_paused_action(ledger, os_user="alice", approval_id="ap1", at=NOW - window)
        tracker = HookQuotaTracker(ledger)
        assert tracker.count("alice", now=NOW, window=window) == 1


class TestQuotaConfig:
    def test_exceeded_for_is_false_below_max(self) -> None:
        ledger = LedgerStore()
        _seed_approved_paused_action(ledger, os_user="alice", approval_id="ap1", at=NOW)
        config = QuotaConfig(
            ledger=ledger, max_actions=2, window=timedelta(days=1), clock=FixedClock(NOW)
        )
        assert config.exceeded_for("alice") is False

    def test_exceeded_for_is_true_at_max(self) -> None:
        ledger = LedgerStore()
        _seed_approved_paused_action(ledger, os_user="alice", approval_id="ap1", at=NOW)
        _seed_approved_paused_action(ledger, os_user="alice", approval_id="ap2", at=NOW)
        config = QuotaConfig(
            ledger=ledger, max_actions=2, window=timedelta(days=1), clock=FixedClock(NOW)
        )
        assert config.exceeded_for("alice") is True
