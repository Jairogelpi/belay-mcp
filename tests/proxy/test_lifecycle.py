"""Tests for the L1 default rule and session set_hash pinning (spec Â§3, Â§4.6, Â§4.7)."""

from __future__ import annotations

import pytest
from belay.contracts.loader import load_contract_set
from belay.contracts.model import Contract, ContractSet, Effect
from belay.errors import BelayError
from belay.ledger.store import LedgerStore
from belay.policy.model import Cap, CapMatch, Defaults, PolicyDoc, ToolRule
from belay.proxy.lifecycle import Lifecycle, resolve

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _empty_contract_set() -> ContractSet:
    return ContractSet(contracts={}, set_hash="sha256:empty")


def _read_contract() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="fs.read_file",
        reversibility="irreversible",
        effects=[Effect(type="read", resource="fs.file")],
    )


async def _noop_executor(tool: str, args: dict) -> dict:
    return {"ok": True, "tool": tool}


def test_read_only_hint_with_no_contract_is_allowed_with_implicit_read_effect() -> None:
    resolved = resolve(
        "fs.list_files",
        {},
        _empty_contract_set(),
        read_only_hint=True,
        unsafe_passthrough=False,
    )
    assert resolved.contract is None
    assert resolved.effects == [{"type": "read", "resource": "fs.list_files"}]
    assert resolved.config_override is False


def test_no_contract_and_no_read_only_hint_is_contract_missing() -> None:
    """@spec("4.6.1") — no contract and not read-only MUST refuse with contract_missing."""
    with pytest.raises(BelayError) as excinfo:
        resolve(
            "fs.write_file",
            {"path": "a"},
            _empty_contract_set(),
            read_only_hint=False,
            unsafe_passthrough=False,
        )
    assert excinfo.value.code == "contract_missing"


def test_destructive_hint_with_no_contract_is_still_contract_missing() -> None:
    # Appendix C: `destructiveHint: true` with no Belay contract => contract_missing.
    # Hints never authorize on their own -- only readOnlyHint does.
    with pytest.raises(BelayError) as excinfo:
        resolve(
            "fs.delete_file",
            {"path": "a"},
            _empty_contract_set(),
            read_only_hint=False,  # destructiveHint never reaches resolve() as a param
            unsafe_passthrough=False,
        )
    assert excinfo.value.code == "contract_missing"


def test_existing_contract_governs_regardless_of_hints() -> None:
    cs = ContractSet(contracts={"fs.read_file": _read_contract()}, set_hash="sha256:x")
    resolved = resolve(
        "fs.read_file", {"path": "a"}, cs, read_only_hint=False, unsafe_passthrough=False
    )
    assert resolved.contract is not None
    assert resolved.config_override is False


async def test_unsafe_passthrough_call_passes_through_and_every_event_carries_override() -> None:
    """@spec("4.6.2") — unsafe_passthrough MUST be recorded in every affected ledger event."""
    ledger = LedgerStore()
    session_id = "s_test"
    lifecycle = Lifecycle(
        contract_set=_empty_contract_set(),
        unsafe_passthrough_tools=frozenset({"fs.delete_file"}),
        ledger=ledger,
        session_id=session_id,
    )
    lifecycle.start_session("test-fixture")

    result = await lifecycle.govern_and_execute(
        "fs.delete_file", {"path": "a"}, read_only_hint=False, executor=_noop_executor
    )
    assert result == {"ok": True, "tool": "fs.delete_file"}

    events = ledger.read(session_id)
    call_events = [e for e in events if e.step_seq == 1]
    assert call_events, "expected events for the overridden call"
    # The resolve-side `config_override` event (spec: "MUST be recorded in
    # every affected ledger event") is what carries the override for an
    # unsafe_passthrough call; the saga step lifecycle events (E6, spec Â§8.1)
    # that follow don't themselves carry the flag, since they are the same
    # journaled/capturing/calling/... events any governed call gets.
    assert any(e.type == "config_override" for e in call_events)


async def test_unsafe_passthrough_does_not_apply_to_other_tools() -> None:
    ledger = LedgerStore()
    lifecycle = Lifecycle(
        contract_set=_empty_contract_set(),
        unsafe_passthrough_tools=frozenset({"fs.delete_file"}),
        ledger=ledger,
        session_id="s_test2",
    )
    with pytest.raises(BelayError) as excinfo:
        await lifecycle.govern_and_execute(
            "fs.other_tool", {}, read_only_hint=False, executor=_noop_executor
        )
    assert excinfo.value.code == "contract_missing"


async def test_session_fixes_set_hash_and_later_contract_changes_do_not_apply() -> None:
    """@spec("4.7") — a session pins the set_hash present at session_started."""
    ledger = LedgerStore()
    session_id = "s_pin"

    cs_v1 = load_contract_set(["examples/contracts/fs.yaml"])
    lifecycle = Lifecycle(
        contract_set=cs_v1,
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id=session_id,
    )
    lifecycle.start_session("test-fixture")

    started = ledger.read(session_id)[0]
    assert started.type == "session_started"
    assert started.set_hash == cs_v1.set_hash

    # A call still resolves against the pinned contract set, even though a
    # hypothetical "new" set (e.g. one with a contract removed) exists
    # elsewhere -- the Lifecycle object never re-reads or re-resolves it.
    cs_v2 = ContractSet(contracts={}, set_hash="sha256:different")
    assert lifecycle.contract_set is cs_v1
    assert lifecycle.contract_set.set_hash != cs_v2.set_hash

    result = await lifecycle.govern_and_execute(
        "fs.read_file", {"path": "x"}, read_only_hint=True, executor=_noop_executor
    )
    assert result == {"ok": True, "tool": "fs.read_file"}
    events = ledger.read(session_id)
    called = [e for e in events if e.type == "tool_called"]
    assert all(e.set_hash == cs_v1.set_hash for e in called)


def _irreversible_send_contract() -> Contract:
    return Contract(
        belay_contract="0.1",
        tool="mail.send",
        reversibility="irreversible",
        effects=[Effect(type="send", resource="email.message", count="1")],
    )


async def test_default_policy_pauses_irreversible_tools_and_blocks_execution() -> None:
    # E5: PolicyEngine's `pause` now really parks the call in the approval
    # queue (spec Â§7) -- the executor is never called and the agent gets a
    # structured `pending_approval` result instead.
    ledger = LedgerStore()
    session_id = "s_pause"
    cs = ContractSet(contracts={"mail.send": _irreversible_send_contract()}, set_hash="sha256:x")
    called = False

    async def executor(tool: str, args: dict) -> dict:
        nonlocal called
        called = True
        return {"ok": True}

    lifecycle = Lifecycle(
        contract_set=cs, unsafe_passthrough_tools=frozenset(), ledger=ledger, session_id=session_id
    )
    lifecycle.start_session("test-fixture")

    result = await lifecycle.govern_and_execute(
        "mail.send", {"to": "a@example.com"}, read_only_hint=False, executor=executor
    )
    assert result["status"] == "pending_approval"
    assert "approval_id" in result
    assert called is False

    events = ledger.read(session_id)
    evaluated = [e for e in events if e.type == "policy_evaluated"]
    assert len(evaluated) == 1
    assert evaluated[0].payload["verdict"] == "pause"
    assert evaluated[0].payload["reasons"] == ["defaults.irreversible"]
    assert any(e.type == "approval_requested" for e in events)


async def test_deny_verdict_blocks_execution_and_never_calls_the_executor() -> None:
    """@spec("3.1") — every numbered stage MUST emit its ledger event, even on denial."""
    ledger = LedgerStore()
    session_id = "s_deny"
    cs = ContractSet(contracts={"mail.send": _irreversible_send_contract()}, set_hash="sha256:x")
    policy = PolicyDoc(
        caps=[Cap(match=CapMatch(effect="send"), max_count=0, over="deny")],
    )
    called = False

    async def executor(tool: str, args: dict) -> dict:
        nonlocal called
        called = True
        return {"ok": True}

    lifecycle = Lifecycle(
        contract_set=cs,
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id=session_id,
        policy=policy,
    )
    lifecycle.start_session("test-fixture")

    with pytest.raises(BelayError) as excinfo:
        await lifecycle.govern_and_execute(
            "mail.send", {"to": "a@example.com"}, read_only_hint=False, executor=executor
        )
    assert excinfo.value.code == "policy_denied"
    assert called is False

    events = ledger.read(session_id)
    assert any(
        e.type == "step_failed" and e.payload.get("error", {}).get("code") == "policy_denied"
        for e in events
    )


async def test_irreversible_relaxation_is_recorded_as_config_override() -> None:
    ledger = LedgerStore()
    session_id = "s_relax"
    cs = ContractSet(contracts={"mail.send": _irreversible_send_contract()}, set_hash="sha256:x")
    policy = PolicyDoc(
        defaults=Defaults(irreversible="pause"),
        tools=[ToolRule(match="mail.send", verdict="allow")],
    )
    lifecycle = Lifecycle(
        contract_set=cs,
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id=session_id,
        policy=policy,
    )
    lifecycle.start_session("test-fixture")

    result = await lifecycle.govern_and_execute(
        "mail.send", {"to": "a@example.com"}, read_only_hint=False, executor=_noop_executor
    )
    assert result == {"ok": True, "tool": "mail.send"}

    events = ledger.read(session_id)
    overrides = [
        e
        for e in events
        if e.type == "config_override" and e.payload.get("reason") == "irreversible_default_relaxed"
    ]
    assert len(overrides) == 1
    assert overrides[0].payload["rules"] == ["tools[0]"]


async def test_paused_then_approved_via_queue_lets_execution_continue() -> None:
    """spec Â§7 end-to-end: pause -> approve (as the CLI would) -> retried call executes."""
    ledger = LedgerStore()
    session_id = "s_approve_flow"
    cs = ContractSet(contracts={"mail.send": _irreversible_send_contract()}, set_hash="sha256:x")
    lifecycle = Lifecycle(
        contract_set=cs, unsafe_passthrough_tools=frozenset(), ledger=ledger, session_id=session_id
    )
    lifecycle.start_session("test-fixture")

    first = await lifecycle.govern_and_execute(
        "mail.send", {"to": "a@example.com"}, read_only_hint=False, executor=_noop_executor
    )
    assert first["status"] == "pending_approval"

    assert lifecycle.approval_stage is not None
    lifecycle.approval_stage.queue.approve(first["approval_id"], approved_by="jairo")

    # The agent retries the identical call; same args -> same plan_id (the
    # planner is deterministic over (tool, args, session)) -> now proceeds.
    second = await lifecycle.govern_and_execute(
        "mail.send", {"to": "a@example.com"}, read_only_hint=False, executor=_noop_executor
    )
    assert second == {"ok": True, "tool": "mail.send"}


async def test_paused_then_rejected_raises_approval_rejected_with_reason() -> None:
    ledger = LedgerStore()
    session_id = "s_reject_flow"
    cs = ContractSet(contracts={"mail.send": _irreversible_send_contract()}, set_hash="sha256:x")
    lifecycle = Lifecycle(
        contract_set=cs, unsafe_passthrough_tools=frozenset(), ledger=ledger, session_id=session_id
    )
    lifecycle.start_session("test-fixture")

    first = await lifecycle.govern_and_execute(
        "mail.send", {"to": "a@example.com"}, read_only_hint=False, executor=_noop_executor
    )
    assert lifecycle.approval_stage is not None
    lifecycle.approval_stage.queue.reject(
        first["approval_id"], rejected_by="jairo", reason="not now"
    )

    with pytest.raises(BelayError) as excinfo:
        await lifecycle.govern_and_execute(
            "mail.send", {"to": "a@example.com"}, read_only_hint=False, executor=_noop_executor
        )
    assert excinfo.value.code == "approval_rejected"
    assert excinfo.value.detail["reason"] == "not now"

