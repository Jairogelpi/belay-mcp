"""RewindService (spec Â§10): order, dry-run honesty, fencing, halt/skip,
verification accounting, the Â§10.3 honesty rule, and compensations routed
through the policy engine (spec Â§12)."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from belay.contracts.model import Contract, ContractSet, Effect, Undo
from belay.errors import BelayError
from belay.executor.saga import SagaExecutor
from belay.ledger.store import LedgerStore
from belay.policy.model import Cap, CapMatch, PolicyDoc
from belay.proxy.lifecycle import Lifecycle
from belay.rewind.service import RewindService, is_fenced

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeStore:
    """A tiny in-memory object store standing in for a real upstream tool server."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def executor(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, dict(args)))
        if tool == "obj.create":
            self.records[args["id"]] = dict(args.get("fields", {}))
            return {"id": args["id"]}
        if tool == "obj.delete":
            existed = args["id"] in self.records
            self.records.pop(args["id"], None)
            return {"id": args["id"], "existed": existed}
        if tool == "obj.get":
            rec = self.records.get(args["id"])
            if rec is None:
                return {"id": args["id"], "existed": False}
            return {"id": args["id"], "existed": True, "fields": rec}
        if tool == "mail.send":
            return {"sent": True}
        raise AssertionError(f"unexpected tool {tool}")


def _create_contract(*, verification: bool = False) -> Contract:
    verification_block = (
        {"tool": "obj.get", "args": {"id": "$args.id"}, "expect": "not_found"}
        if verification
        else None
    )
    return Contract(
        belay_contract="0.1",
        tool="obj.create",
        reversibility="reversible",
        undo=Undo(tool="obj.delete", args={"id": "$args.id"}),
        effects=[Effect(type="create", resource="obj.record", count="1")],
        verification=verification_block,
    )


def _send_contract() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="mail.send",
        reversibility="irreversible",
        effects=[Effect(type="send", resource="email.message", count="1")],
    )


def _delete_contract() -> Contract:
    # Only used by `RewindService._compensation_plan` to look up `obj.delete`'s
    # own declared effects (for the cap check) -- it is never itself routed
    # through the lifecycle, so it doesn't need (and per spec Â§4.2 must not
    # declare) an `undo` of its own.
    return Contract(
        belay_contract="0.1",
        tool="obj.delete",
        reversibility="irreversible",
        effects=[Effect(type="delete", resource="obj.record", count="1")],
    )


def _get_contract() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="obj.get",
        reversibility="irreversible",
        effects=[Effect(type="read", resource="obj.record", count="1")],
    )


def _contract_set(*extra: Contract) -> ContractSet:
    contracts = {"obj.delete": _delete_contract(), "obj.get": _get_contract()}
    for c in extra:
        contracts[c.tool] = c
    return ContractSet(contracts=contracts, set_hash="sha256:test")


async def _commit_mixed_session(
    ledger: LedgerStore, session_id: str, store: FakeStore, *, verification: bool = False
) -> None:
    """3 steps: reversible create, irreversible send, reversible create."""
    saga = SagaExecutor(ledger=ledger)
    create = _create_contract(verification=verification)
    send = _send_contract()
    await saga.run_step(session_id, 1, "obj.create", {"id": "a"}, create, store.executor)
    await saga.run_step(session_id, 2, "mail.send", {"to": "x@example.com"}, send, store.executor)
    await saga.run_step(session_id, 3, "obj.create", {"id": "b"}, create, store.executor)


# --- order + mini-step ledger -------------------------------------------------


async def test_compensations_run_in_strict_reverse_step_seq_order_as_mini_steps() -> None:
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_order"
    await _commit_mixed_session(ledger, session_id, store)

    service = RewindService(ledger=ledger, contract_set=_contract_set(_create_contract()))
    report = await service.rewind(session_id, store.executor, by="jairo")

    # Steps 3 and 1 are reversible (2 is irreversible): compensations must
    # execute in strict reverse order, 3 then 1.
    executed_order = [
        (ev.step_seq, ev.type)
        for ev in ledger.read(session_id)
        if ev.type == "compensation_executed"
    ]
    assert [s for s, _ in executed_order] == [3, 1]
    assert report.outcomes[0].step_seq == 3
    assert "a" not in store.records and "b" not in store.records


# --- dry run honesty -----------------------------------------------------------


async def test_dry_run_enumerates_honestly_and_touches_nothing() -> None:
    """@spec("10.3.1") — rewind result MUST enumerate compensated/skipped/irreversible/indeterm."""
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_dry"
    await _commit_mixed_session(ledger, session_id, store)
    before_events = ledger.read(session_id)
    calls_before_rewind = list(store.calls)

    async def _forbidden(tool: str, args: dict) -> dict:
        raise AssertionError("dry-run must never call the executor")

    service = RewindService(ledger=ledger, contract_set=_contract_set(_create_contract()))
    report = await service.rewind(session_id, _forbidden, dry_run=True, by="jairo")

    assert report.dry_run is True
    assert {s.step_seq for s in report.plan.reversible} == {1, 3}
    assert {s.step_seq for s in report.plan.irreversible} == {2}
    assert report.plan.conditional_unmet == []
    assert report.plan.indeterminate == []
    # No ledger events appended, no upstream calls made (beyond the setup
    # session's own committed steps, captured above the dry run).
    assert ledger.read(session_id) == before_events
    assert store.calls == calls_before_rewind


# --- fencing (with an explicit race) -------------------------------------------


async def test_new_step_after_fence_raises_session_fenced() -> None:
    """@spec("10.1") — rewind of a live session MUST first fence the session."""
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_fence"
    cs = _contract_set(_create_contract())
    lifecycle = Lifecycle(
        contract_set=cs, unsafe_passthrough_tools=frozenset(), ledger=ledger, session_id=session_id
    )
    lifecycle.start_session("test-fixture")
    await lifecycle.govern_and_execute(
        "obj.create", {"id": "a"}, read_only_hint=False, executor=store.executor
    )

    service = RewindService(ledger=ledger, contract_set=cs)
    assert is_fenced(ledger, session_id) is False
    service.fence(session_id)
    assert is_fenced(ledger, session_id) is True

    with pytest.raises(BelayError) as excinfo:
        await lifecycle.govern_and_execute(
            "obj.create", {"id": "b"}, read_only_hint=False, executor=store.executor
        )
    assert excinfo.value.code == "session_fenced"


async def test_fencing_race_fence_wins_over_a_step_racing_to_start() -> None:
    """Explicit race: a step attempt and a fence both fire; whichever the fence
    precedes must be rejected -- fencing is a ledger fact any concurrent
    `govern_and_execute` call observes."""
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_race"
    cs = _contract_set(_create_contract())
    lifecycle = Lifecycle(
        contract_set=cs, unsafe_passthrough_tools=frozenset(), ledger=ledger, session_id=session_id
    )
    lifecycle.start_session("test-fixture")
    service = RewindService(ledger=ledger, contract_set=cs)

    results: list[str] = []

    async def racing_step() -> None:
        # Give the fence a head start so the race is deterministic but still
        # exercises the exact same fence-then-attempt code path a real race
        # would hit under the GIL/event loop's cooperative scheduling.
        await anyio.sleep(0.01)
        try:
            await lifecycle.govern_and_execute(
                "obj.create", {"id": "late"}, read_only_hint=False, executor=store.executor
            )
            results.append("proceeded")
        except BelayError as exc:
            results.append(exc.code)

    async with anyio.create_task_group() as tg:
        tg.start_soon(racing_step)
        service.fence(session_id)

    assert results == ["session_fenced"]


# --- halt vs skip-and-continue --------------------------------------------------


async def test_halt_on_failure_is_the_default_and_stops_at_first_failure() -> None:
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_halt"
    saga = SagaExecutor(ledger=ledger)
    create = _create_contract()
    await saga.run_step(session_id, 1, "obj.create", {"id": "a"}, create, store.executor)
    await saga.run_step(session_id, 2, "obj.create", {"id": "b"}, create, store.executor)

    async def flaky(tool: str, args: dict) -> dict:
        if tool == "obj.delete" and args.get("id") == "b":
            raise RuntimeError("boom")
        return await store.executor(tool, args)

    service = RewindService(ledger=ledger, contract_set=_contract_set(_create_contract()))
    report = await service.rewind(session_id, flaky, by="jairo")

    assert report.halted is True
    assert [o.status for o in report.outcomes] == ["compensation_failed"]
    # step 1 was never attempted because rewind halted at step 2's failure.
    assert "a" in store.records


async def test_skip_and_continue_is_explicit_and_recorded_in_the_ledger() -> None:
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_skip"
    saga = SagaExecutor(ledger=ledger)
    create = _create_contract()
    await saga.run_step(session_id, 1, "obj.create", {"id": "a"}, create, store.executor)
    await saga.run_step(session_id, 2, "obj.create", {"id": "b"}, create, store.executor)

    async def flaky(tool: str, args: dict) -> dict:
        if tool == "obj.delete" and args.get("id") == "b":
            raise RuntimeError("boom")
        return await store.executor(tool, args)

    service = RewindService(ledger=ledger, contract_set=_contract_set(_create_contract()))
    report = await service.rewind(session_id, flaky, by="jairo", skip_and_continue=True)

    assert report.halted is False
    assert {o.status for o in report.outcomes} == {"compensation_failed", "compensated"}
    # step 1 (obj a) still got compensated despite step 2 failing.
    assert "a" not in store.records

    events = ledger.read(session_id)
    assert any(
        e.type == "config_override" and e.payload.get("reason") == "skip_and_continue"
        for e in events
    )


# --- verification: executed, recorded, and accounted for -----------------------


async def test_verification_is_executed_and_a_failure_does_not_count_as_compensated() -> None:
    """@spec("10.2") — declared verification blocks MUST be checked and recorded."""
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_verify"
    saga = SagaExecutor(ledger=ledger)
    create = _create_contract(verification=True)
    await saga.run_step(session_id, 1, "obj.create", {"id": "a"}, create, store.executor)

    async def lying_executor(tool: str, args: dict) -> dict:
        result = await store.executor(tool, args)
        if tool == "obj.get":
            # Simulate a verification check that (falsely) still finds the record.
            return {"id": args["id"], "existed": True}
        return result

    cs = _contract_set(_create_contract(verification=True))
    service = RewindService(ledger=ledger, contract_set=cs)
    report = await service.rewind(session_id, lying_executor, by="jairo")

    assert report.outcomes[0].status == "verification_failed"
    assert report.fully_rewound is False
    events = ledger.read(session_id)
    assert any(
        e.type == "step_failed" and e.payload.get("error", {}).get("code") == "verification_failed"
        for e in events
    )
    # The undo call itself DID happen (accounting, not just the error code).
    assert "a" not in store.records
    assert any(e.type == "compensation_executed" for e in events)


async def test_verification_passing_counts_as_compensated() -> None:
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_verify_ok"
    saga = SagaExecutor(ledger=ledger)
    create = _create_contract(verification=True)
    await saga.run_step(session_id, 1, "obj.create", {"id": "a"}, create, store.executor)

    cs = _contract_set(_create_contract(verification=True))
    service = RewindService(ledger=ledger, contract_set=cs)
    report = await service.rewind(session_id, store.executor, by="jairo")

    assert report.outcomes[0].status == "compensated"
    assert report.fully_rewound is True
    # No `capture` block was declared on this contract, so there's nothing to
    # compare the re-query against -- Verified Rewind must not claim byte-exact
    # `restored` on no evidence, only the weaker "effect neutralized" claim.
    assert report.outcomes[0].result == "compensated"
    assert report.verified_result == "compensated"


# --- Verified Rewind: restored vs compensated vs partial vs impossible ---------


def _create_contract_with_capture() -> Contract:
    from belay.contracts.model import Capture

    return Contract(
        belay_contract="0.1",
        tool="obj.create",
        reversibility="reversible",
        undo=Undo(tool="obj.delete", args={"id": "$args.id"}),
        effects=[Effect(type="create", resource="obj.record", count="1")],
        capture=Capture(tool="obj.get", args={"id": "$args.id"}, **{"as": "before"}),
        verification={"tool": "obj.get", "args": {"id": "$args.id"}, "expect": "not_found"},
    )


async def test_restored_when_post_compensation_state_matches_pre_action_snapshot() -> None:
    """A `capture` before create snapshots "doesn't exist yet"; after compensating
    (delete) the object, re-querying finds the same "doesn't exist" -- byte-exact
    match to the pre-action snapshot earns the strong `restored` claim."""
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_restored"
    saga = SagaExecutor(ledger=ledger)
    create = _create_contract_with_capture()
    await saga.run_step(session_id, 1, "obj.create", {"id": "a"}, create, store.executor)

    cs = _contract_set(create)
    service = RewindService(ledger=ledger, contract_set=cs)
    report = await service.rewind(session_id, store.executor, by="jairo")

    assert report.outcomes[0].result == "restored"
    assert report.verified_result == "restored"


async def test_compensated_when_post_state_differs_from_pre_action_snapshot() -> None:
    """Verification passes (declared `expect` is satisfied) but the re-queried
    state isn't identical to what was captured before the original action --
    the business effect is neutralized, but it's dishonest to call that
    byte-exact `restored`."""
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_compensated_not_restored"
    saga = SagaExecutor(ledger=ledger)
    create = _create_contract_with_capture()
    await saga.run_step(session_id, 1, "obj.create", {"id": "a"}, create, store.executor)

    async def executor_with_side_effect(tool: str, args: dict) -> dict:
        result = await store.executor(tool, args)
        if tool == "obj.get":
            # Real system's re-query differs from the pre-action snapshot
            # (e.g. an unrelated field changed elsewhere) even though the
            # declared `expect` ("not_found") is still satisfied.
            return {**result, "extra_unrelated_field": "changed"}
        return result

    cs = _contract_set(create)
    service = RewindService(ledger=ledger, contract_set=cs)
    report = await service.rewind(session_id, executor_with_side_effect, by="jairo")

    assert report.outcomes[0].status == "compensated"
    assert report.outcomes[0].result == "compensated"
    assert report.verified_result == "compensated"


async def test_impossible_when_every_in_scope_step_is_irreversible() -> None:
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_impossible"
    saga = SagaExecutor(ledger=ledger)
    send = _send_contract()
    await saga.run_step(session_id, 1, "mail.send", {"to": "x@example.com"}, send, store.executor)

    service = RewindService(ledger=ledger, contract_set=_contract_set())
    report = await service.rewind(session_id, store.executor, by="jairo")

    assert report.verified_result == "impossible"


async def test_partial_when_some_but_not_all_in_scope_steps_recover() -> None:
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_partial"
    await _commit_mixed_session(ledger, session_id, store)  # reversible, irreversible, reversible

    service = RewindService(ledger=ledger, contract_set=_contract_set(_create_contract()))
    report = await service.rewind(session_id, store.executor, by="jairo")

    assert report.verified_result == "partial"


async def test_dry_run_has_no_verified_result() -> None:
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_dry_vr"
    await _commit_mixed_session(ledger, session_id, store)

    service = RewindService(ledger=ledger, contract_set=_contract_set(_create_contract()))
    report = await service.rewind(session_id, store.executor, by="jairo", dry_run=True)

    assert report.verified_result is None


# --- honesty (Â§10.3): never "fully rewound" with irreversible steps remaining --


async def test_honesty_mixed_reversible_and_irreversible_never_reports_fully_rewound() -> None:
    """@spec("10.3.2") — MUST NOT report "fully rewound" unless every step compensated+verified."""
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_honest"
    await _commit_mixed_session(ledger, session_id, store)

    service = RewindService(ledger=ledger, contract_set=_contract_set(_create_contract()))
    report = await service.rewind(session_id, store.executor, by="jairo")

    # Both reversible steps succeeded; the irreversible one is honestly skipped...
    statuses = {o.step_seq: o.status for o in report.outcomes}
    assert statuses[1] == "compensated"
    assert statuses[3] == "compensated"
    assert statuses[2] == "skipped"
    # ...but the session is NOT fully rewound because step 2 is irreversible
    # and remains in scope. This is the honesty property (spec Â§10.3).
    assert report.fully_rewound is False


# --- compensations pass through the policy engine (spec Â§12) -------------------


async def test_compensation_over_a_cap_pauses_like_a_forward_action() -> None:
    ledger = LedgerStore()
    store = FakeStore()
    session_id = "s_cap"
    saga = SagaExecutor(ledger=ledger)
    create = _create_contract()
    await saga.run_step(session_id, 1, "obj.create", {"id": "a"}, create, store.executor)

    cap_match = CapMatch(effect="delete", resource="obj.record")
    policy = PolicyDoc(caps=[Cap(match=cap_match, max_count=0, over="pause")])
    service = RewindService(
        ledger=ledger, policy=policy, contract_set=_contract_set(_create_contract())
    )
    report = await service.rewind(session_id, store.executor, by="jairo")

    assert report.outcomes[0].status == "paused"
    assert "a" in store.records  # compensation never actually ran
    assert report.fully_rewound is False

    events = ledger.read(session_id)
    assert any(e.type == "approval_requested" for e in events)

    # Approve the parked compensation, then rewind again: it now proceeds.
    assert service.approvals is not None
    pending = next(i for i in service.approvals.list() if i.session_id == session_id)
    service.approvals.approve(pending.approval_id, approved_by="jairo")

    report2 = await service.rewind(session_id, store.executor, by="jairo")
    assert report2.outcomes[0].status == "compensated"
    assert "a" not in store.records
    assert report2.fully_rewound is True

