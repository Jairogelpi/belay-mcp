"""Tests for the `quota` policy dimension (plan-v2 E15): per-identity rolling quota of
approved-and-executed irreversible actions. Uses a real `LedgerStore`, never global state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from belay.clock import FixedClock
from belay.contracts.model import Contract as ContractModel
from belay.contracts.model import ContractSet, Effect
from belay.db.models import EventRow
from belay.ledger.store import LedgerStore
from belay.planner.model import EffectEstimate, Plan
from belay.policy.engine import PolicyEngine
from belay.policy.model import Cap, CapMatch, Defaults, PolicyDoc, QuotaDefaults, ToolRule
from belay.policy.quota import QuotaTracker, parse_window
from belay.proxy.lifecycle import Lifecycle
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import update
from sqlalchemy.orm import Session as DBSession

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)


def _plan(
    *,
    plan_id: str,
    session_id: str = "s1",
    tool: str = "mail.send",
    reversibility: str = "irreversible",
) -> Plan:
    return Plan(
        plan_id=plan_id,
        session_id=session_id,
        tool=tool,
        args={},
        effects=[EffectEstimate(type="send", resource="email.message", count="1", estimate=False)],
        reversibility=reversibility,  # type: ignore[arg-type]
        confidence="high",
        created_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(minutes=10)).isoformat(),
    )


def _set_event_at(ledger: LedgerStore, event_id: str, at: datetime) -> None:
    """Test-only: backdate one event's `at` for deterministic window-boundary tests.
    Production code never rewrites a ledger row -- this only simulates events that
    really happened at a given instant, it does not mutate the append-only guarantee."""
    with DBSession(ledger.engine) as db:
        db.execute(update(EventRow).where(EventRow.event_id == event_id).values(at=at.isoformat()))
        db.commit()


def _seed_approved_executed_irreversible(
    ledger: LedgerStore,
    *,
    session_id: str,
    identity: str,
    step_seq: int,
    at: datetime,
    tool: str = "mail.send",
) -> None:
    """Append one full approved-and-executed irreversible action at a controlled `at`."""
    ledger.append(
        session_id, "session_started", {}, initiated_by=identity
    ) if not ledger.read(session_id) else None
    plan = _plan(plan_id=f"{session_id}-{step_seq}", session_id=session_id, tool=tool)
    ledger.append(session_id, "plan_created", plan.model_dump(mode="json"), step_seq=step_seq)
    ev = ledger.append(
        session_id, "policy_evaluated", {"verdict": "allow", "reasons": []}, step_seq=step_seq
    )
    _set_event_at(ledger, ev.event_id, at)
    ledger.append(session_id, "step_committed", {"tool": tool}, step_seq=step_seq)


def _seed_denied_or_pending(
    ledger: LedgerStore,
    *,
    session_id: str,
    identity: str,
    step_seq: int,
    at: datetime,
    state: str,  # "denied" or "pending"
) -> None:
    if not ledger.read(session_id):
        ledger.append(session_id, "session_started", {}, initiated_by=identity)
    plan = _plan(plan_id=f"{session_id}-{step_seq}", session_id=session_id)
    ledger.append(session_id, "plan_created", plan.model_dump(mode="json"), step_seq=step_seq)
    verdict = "deny" if state == "denied" else "pause"
    ev = ledger.append(session_id, "policy_evaluated", {"verdict": verdict}, step_seq=step_seq)
    _set_event_at(ledger, ev.event_id, at)
    # No approval_resolved (still pending) / no step_committed either way.


# -- 1. below quota allows; at/over quota pauses with an explanatory reason --


def test_below_quota_contributes_nothing() -> None:
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger, clock=FixedClock(NOW))
    policy = PolicyDoc(
        defaults=Defaults(quota=QuotaDefaults(enabled=True, max_irreversible_actions=5))
    )
    for i in range(3):
        _seed_approved_executed_irreversible(
            ledger, session_id="s1", identity="alice", step_seq=i, at=NOW
        )

    new_plan = _plan(plan_id="p_new", session_id="s1")
    ledger.append("s1", "plan_created", new_plan.model_dump(mode="json"), step_seq=99)
    result = engine.evaluate(new_plan, policy)
    assert not any(r.startswith("quota:") for r in result.reasons)


def test_at_or_over_quota_triggers_configured_verdict_with_explainable_reason() -> None:
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger, clock=FixedClock(NOW))
    policy = PolicyDoc(
        defaults=Defaults(
            quota=QuotaDefaults(enabled=True, max_irreversible_actions=3, window="1d")
        )
    )
    for i in range(3):
        _seed_approved_executed_irreversible(
            ledger, session_id="s1", identity="alice", step_seq=i, at=NOW
        )

    new_plan = _plan(plan_id="p_new", session_id="s1")
    ledger.append("s1", "plan_created", new_plan.model_dump(mode="json"), step_seq=99)
    result = engine.evaluate(new_plan, policy)

    assert result.verdict == "pause"
    reason = next(r for r in result.reasons if r.startswith("quota:"))
    assert "alice" in reason
    assert "3" in reason  # current count
    assert "1d" in reason  # window
    assert "3" in reason  # configured max


# -- 2. rolling window boundary correctness, both edges ---------------------


def test_action_just_inside_window_counts() -> None:
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger, clock=FixedClock(NOW))
    policy = PolicyDoc(
        defaults=Defaults(quota=
            QuotaDefaults(enabled=True, max_irreversible_actions=1, window="1d"))
    )
    # exactly at the cutoff (now - window) -- still inside, per the inclusive boundary rule.
    _seed_approved_executed_irreversible(
        ledger, session_id="s1", identity="alice", step_seq=0, at=NOW - timedelta(days=1)
    )
    new_plan = _plan(plan_id="p_new", session_id="s1")
    ledger.append("s1", "plan_created", new_plan.model_dump(mode="json"), step_seq=99)
    result = engine.evaluate(new_plan, policy)
    assert result.verdict == "pause"
    assert any(r.startswith("quota:") for r in result.reasons)


def test_action_aged_out_of_window_does_not_count() -> None:
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger, clock=FixedClock(NOW))
    policy = PolicyDoc(
        defaults=Defaults(quota=
            QuotaDefaults(enabled=True, max_irreversible_actions=1, window="1d"))
    )
    # one second older than the window -- aged out.
    _seed_approved_executed_irreversible(
        ledger,
        session_id="s1",
        identity="alice",
        step_seq=0,
        at=NOW - timedelta(days=1, seconds=1),
    )
    new_plan = _plan(plan_id="p_new", session_id="s1")
    ledger.append("s1", "plan_created", new_plan.model_dump(mode="json"), step_seq=99)
    result = engine.evaluate(new_plan, policy)
    assert not any(r.startswith("quota:") for r in result.reasons)


# -- 3. per-identity isolation, no cross-identity leakage --------------------


def test_two_identities_each_individually_under_quota_despite_combined_total() -> None:
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger, clock=FixedClock(NOW))
    policy = PolicyDoc(
        defaults=Defaults(quota=
            QuotaDefaults(enabled=True, max_irreversible_actions=3, window="1d"))
    )
    for i in range(2):
        _seed_approved_executed_irreversible(
            ledger, session_id="s_alice", identity="alice", step_seq=i, at=NOW
        )
    for i in range(2):
        _seed_approved_executed_irreversible(
            ledger, session_id="s_bob", identity="bob", step_seq=i, at=NOW
        )
    # Combined total (4) would exceed max=3, but each identity individually is at 2 < 3.
    alice_new = _plan(plan_id="p_alice_new", session_id="s_alice")
    ledger.append("s_alice", "plan_created", alice_new.model_dump(mode="json"), step_seq=99)
    assert not any(
        r.startswith("quota:") for r in engine.evaluate(alice_new, policy).reasons
    )

    bob_new = _plan(plan_id="p_bob_new", session_id="s_bob")
    ledger.append("s_bob", "plan_created", bob_new.model_dump(mode="json"), step_seq=99)
    assert not any(r.startswith("quota:") for r in engine.evaluate(bob_new, policy).reasons)


# -- 4. only approved+executed actions count, not denied/pending ------------


def test_denied_and_pending_actions_never_count_toward_quota() -> None:
    ledger = LedgerStore()
    tracker = QuotaTracker(ledger)
    for i in range(5):
        _seed_denied_or_pending(
            ledger, session_id="s1", identity="alice", step_seq=i, at=NOW, state="denied"
        )
    for i in range(5, 10):
        _seed_denied_or_pending(
            ledger, session_id="s1", identity="alice", step_seq=i, at=NOW, state="pending"
        )
    assert tracker.count("alice", now=NOW, window=timedelta(days=1)) == 0

    # But an actually approved+executed one right after does count.
    _seed_approved_executed_irreversible(
        ledger, session_id="s1", identity="alice", step_seq=10, at=NOW
    )
    assert tracker.count("alice", now=NOW, window=timedelta(days=1)) == 1


# -- 5. composition with an existing cap/irreversible verdict, max-severity --


def test_quota_composes_with_cap_via_max_severity() -> None:
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger, clock=FixedClock(NOW))
    policy = PolicyDoc(
        caps=[Cap(match=CapMatch(effect="send"), max_count=0, over="deny")],
        defaults=Defaults(quota=
            QuotaDefaults(enabled=True, max_irreversible_actions=1, window="1d")),
    )
    _seed_approved_executed_irreversible(
        ledger, session_id="s1", identity="alice", step_seq=0, at=NOW
    )
    new_plan = _plan(plan_id="p_new", session_id="s1")
    ledger.append("s1", "plan_created", new_plan.model_dump(mode="json"), step_seq=99)
    result = engine.evaluate(new_plan, policy)

    assert result.verdict == "deny"  # cap's deny beats quota's pause
    assert any(r.startswith("quota:") for r in result.reasons)
    assert any(r.startswith("caps[") for r in result.reasons)


def test_quota_composes_with_irreversible_default_max_severity() -> None:
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger, clock=FixedClock(NOW))
    policy = PolicyDoc(
        defaults=Defaults(
            irreversible="allow",
            quota=QuotaDefaults(
                enabled=True, max_irreversible_actions=1, window="1d", verdict="deny"
            ),
        )
    )
    _seed_approved_executed_irreversible(
        ledger, session_id="s1", identity="alice", step_seq=0, at=NOW
    )
    new_plan = _plan(plan_id="p_new", session_id="s1")
    ledger.append("s1", "plan_created", new_plan.model_dump(mode="json"), step_seq=99)
    result = engine.evaluate(new_plan, policy)
    assert result.verdict == "deny"  # quota's configured deny wins over allow


# -- 6. property test: the (M+1)th irreversible action always triggers ------


@given(
    m=st.integers(min_value=1, max_value=6),
    n_extra=st.integers(min_value=0, max_value=4),
)
@settings(max_examples=40, deadline=None)
def test_property_mth_plus_one_action_always_triggers_never_allow(m: int, n_extra: int) -> None:
    """For any M and any N >= M prior approved+executed irreversible actions by one identity,
    the (M+1)th (and every action after it) within the window triggers the configured verdict,
    never `allow`."""
    ledger = LedgerStore()
    engine = PolicyEngine(ledger=ledger, clock=FixedClock(NOW))
    policy = PolicyDoc(
        defaults=Defaults(quota=
            QuotaDefaults(enabled=True, max_irreversible_actions=m, window="1d"))
    )
    session_id = "s_prop"
    identity = "alice"
    total_prior = m + n_extra  # at least M prior actions -> next one is beyond the (M+1)th
    for i in range(total_prior):
        _seed_approved_executed_irreversible(
            ledger, session_id=session_id, identity=identity, step_seq=i, at=NOW
        )

    probe = _plan(plan_id="p_probe", session_id=session_id)
    ledger.append(session_id, "plan_created", probe.model_dump(mode="json"), step_seq=9999)
    result = engine.evaluate(probe, policy)
    assert result.verdict != "allow"
    assert any(r.startswith("quota:") for r in result.reasons)


# -- parse_window ------------------------------------------------------------


def test_parse_window_supports_common_suffixes() -> None:
    assert parse_window("1d") == timedelta(days=1)
    assert parse_window("7d") == timedelta(days=7)
    assert parse_window("12h") == timedelta(hours=12)
    assert parse_window("30m") == timedelta(minutes=30)
    assert parse_window("45s") == timedelta(seconds=45)


# -- 7. CLI/end-to-end: a real session paused purely by quota, no per-call Cap --


@pytest.mark.anyio
async def test_end_to_end_nth_bulk_action_paused_purely_by_quota_no_cap() -> None:
    contract = ContractModel(
        belay_contract="0.1",
        tool="mail.send",
        reversibility="irreversible",
        effects=[Effect(type="send", resource="email.message", count="1")],
    )
    cs = ContractSet(contracts={"mail.send": contract}, set_hash="sha256:x")
    ledger = LedgerStore()
    policy = PolicyDoc(
        # Relax the irreversible default so calls auto-allow (and thus execute)
        # until quota fires -- no per-call Cap anywhere for mail.send.
        tools=[ToolRule(match="mail.send", verdict="allow")],
        defaults=Defaults(
            quota=QuotaDefaults(enabled=True, max_irreversible_actions=3, window="1d")
        ),
    )
    assert policy.caps == []

    # Real `SystemClock` (default): `LedgerStore.append` always stamps real
    # wall-clock time (spec §9.1), so quota's "now" must track it too for
    # this end-to-end test -- a `FixedClock` in the past would put every
    # ledger event outside its own rolling window.
    lifecycle = Lifecycle(
        contract_set=cs,
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id="s_e2e",
        policy=policy,
    )
    lifecycle.start_session("agent-bot")

    async def executor(tool: str, args: dict) -> dict:
        return {"ok": True}

    results = []
    for i in range(4):
        result = await lifecycle.govern_and_execute(
            "mail.send", {"to": f"user{i}@example.com"}, read_only_hint=False, executor=executor
        )
        results.append(result)

    # first 3 auto-allow and execute; the 4th is paused purely by quota.
    for r in results[:3]:
        assert "status" not in r or r.get("status") != "pending_approval"
    assert results[3]["status"] == "pending_approval"

    events = ledger.read("s_e2e")
    last_eval = [e for e in events if e.type == "policy_evaluated"][-1]
    assert any(r.startswith("quota:") for r in last_eval.payload["reasons"])


# -- 8. regression: approved retry re-plans under a new step_seq, must still count --


@pytest.mark.anyio
async def test_approved_retry_under_new_step_seq_still_counts_toward_quota() -> None:
    """The agent's paused call gets `step_seq=1`; after a human approves it, the
    agent's retry is a brand-new `govern_and_execute` call that re-plans and
    re-evaluates policy under `step_seq=2` (only `ApprovalStage.check` -- keyed
    by `plan_id` -- lets it proceed). `QuotaTracker` must follow that same
    `plan_id` link, not assume the approval and the execution share a `step_seq`."""
    from belay.approvals.queue import ApprovalQueue

    contract = ContractModel(
        belay_contract="0.1",
        tool="mail.send",
        reversibility="irreversible",
        effects=[Effect(type="send", resource="email.message", count="1")],
    )
    cs = ContractSet(contracts={"mail.send": contract}, set_hash="sha256:x")
    ledger = LedgerStore()
    policy = PolicyDoc(defaults=Defaults(quota=QuotaDefaults(enabled=True, max_irreversible_actions=99)))
    queue = ApprovalQueue(engine=ledger.engine)
    lifecycle = Lifecycle(
        contract_set=cs,
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id="s_retry",
        policy=policy,
        approval_stage=None,
    )
    lifecycle.approval_stage.queue = queue  # type: ignore[union-attr]
    lifecycle.start_session("agent-bot")

    async def executor(tool: str, args: dict) -> dict:
        return {"ok": True}

    args = {"to": "user@example.com"}
    first = await lifecycle.govern_and_execute(
        "mail.send", args, read_only_hint=False, executor=executor
    )
    assert first["status"] == "pending_approval"

    item = queue.approve(first["approval_id"], approved_by="human")
    ledger.append(
        item.session_id,
        "approval_resolved",
        {"approval_id": item.approval_id, "plan_id": item.plan_id, "state": "approved"},
        step_seq=item.step_seq,
    )

    second = await lifecycle.govern_and_execute(
        "mail.send", args, read_only_hint=False, executor=executor
    )
    assert "status" not in second or second["status"] != "pending_approval"

    tracker = QuotaTracker(ledger)
    assert tracker.count("agent-bot", now=datetime.now(UTC), window=timedelta(days=1)) == 1


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
