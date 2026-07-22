"""Tests for the L1 default rule and session set_hash pinning (spec §3, §4.6, §4.7)."""

from __future__ import annotations

import pytest
from belay.contracts.loader import load_contract_set
from belay.contracts.model import Contract, ContractSet, Effect
from belay.errors import BelayError
from belay.ledger.store import LedgerStore
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
    ledger = LedgerStore()
    session_id = "s_test"
    lifecycle = Lifecycle(
        contract_set=_empty_contract_set(),
        unsafe_passthrough_tools=frozenset({"fs.delete_file"}),
        ledger=ledger,
        session_id=session_id,
    )
    lifecycle.start_session()

    result = await lifecycle.govern_and_execute(
        "fs.delete_file", {"path": "a"}, read_only_hint=False, executor=_noop_executor
    )
    assert result == {"ok": True, "tool": "fs.delete_file"}

    events = ledger.read(session_id)
    call_events = [e for e in events if e.step_seq == 1]
    assert call_events, "expected events for the overridden call"
    for ev in call_events:
        assert ev.payload.get("config_override") is True or ev.type == "config_override"
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
    ledger = LedgerStore()
    session_id = "s_pin"

    cs_v1 = load_contract_set(["examples/contracts/fs.yaml"])
    lifecycle = Lifecycle(
        contract_set=cs_v1,
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id=session_id,
    )
    lifecycle.start_session()

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
