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
from pathlib import Path

import pytest
from belay.approvals.queue import ApprovalQueue
from belay.clock import FixedClock
from belay.contracts.model import ContractSet
from belay.hooks.claude_code_adapter import normalize
from belay.hooks.file_snapshot import SnapshotStore
from belay.hooks.gate import (
    GateDecision,
    evaluate,
    evaluate_file_edit,
    evaluate_mcp_call,
    ledger_session_id,
    post_event_evidence,
    pre_event_evidence,
    session_key,
)
from belay.hooks.quota import QuotaConfig
from belay.supervisor.protocol import HookEvent
from sqlalchemy import create_engine


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def queue(clock: FixedClock) -> ApprovalQueue:
    return ApprovalQueue(clock=clock)


def _event(
    command: str, session_id: str = "sess-1", tool_name: str = "Bash", cwd: str = "/repo-a"
) -> HookEvent:
    raw = {
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "tool_use_id": f"toolu_{abs(hash((session_id, tool_name, command, cwd)))}",
        "cwd": cwd,
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


def test_same_command_different_cwd_gets_independent_approvals(queue: ApprovalQueue) -> None:
    """The P0 fix itself: plan_id used to be sha256(command) alone, so
    approving 'rm -rf /tmp/x' in one directory would silently also allow
    the identical string run from a completely different directory/repo.
    Now cwd (and repo HEAD, session, host, tool) are part of the binding."""
    event_a = _event("rm -rf /tmp/x", cwd="/repo-a")
    event_b = _event("rm -rf /tmp/x", cwd="/repo-b")

    result_a = evaluate(event_a, queue)
    result_b = evaluate(event_b, queue)
    assert result_a.verdict == "deny"
    assert result_b.verdict == "deny"
    assert result_a.approval_id != result_b.approval_id
    assert len(queue.list()) == 2

    # Approving the /repo-a item must NOT allow the identical command in /repo-b.
    queue.approve(result_a.approval_id, approved_by="jairo")
    still_denied = evaluate(event_b, queue)
    assert still_denied.verdict == "deny"

    now_allowed = evaluate(event_a, queue)
    assert now_allowed.verdict == "allow"


def test_same_command_different_session_gets_independent_approvals(queue: ApprovalQueue) -> None:
    event_a = _event("rm -rf /tmp/x", session_id="session-a")
    event_b = _event("rm -rf /tmp/x", session_id="session-b")

    result_a = evaluate(event_a, queue)
    result_b = evaluate(event_b, queue)
    assert result_a.approval_id != result_b.approval_id

    queue.approve(result_a.approval_id, approved_by="jairo")
    assert evaluate(event_b, queue).verdict == "deny"
    assert evaluate(event_a, queue).verdict == "allow"


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


class TestBelayHomeProtection:
    """The P0 fix: an otherwise-allowlisted "safe read" command must never
    be allowed to read belay's own private storage (the capability token,
    the approvals database) -- even though that storage lives outside the
    project, Claude Code's Bash tool runs as the same OS user and can reach
    anything that user can read."""

    @pytest.fixture(autouse=True)
    def _belay_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        home = tmp_path / "belay-home"
        monkeypatch.setenv("BELAY_HOME", str(home))
        (home / "keys").mkdir(parents=True)
        (home / "keys" / "install1.key").write_bytes(b"secret-token-bytes")
        return home

    def test_cat_of_the_capability_key_is_denied_not_allowed(
        self, queue: ApprovalQueue, _belay_home: Path
    ) -> None:
        key_path = _belay_home / "keys" / "install1.key"
        raw = {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"cat {key_path}"},
            "tool_use_id": "toolu_1",
        }
        result = evaluate(normalize(raw, installation_id="test-install"), queue)
        assert result.verdict == "deny"
        assert "private storage" in result.reason
        assert queue.list() == []  # never queued as a normal pause either -- denied outright

    def test_cat_of_an_unrelated_file_is_still_allowed(
        self, queue: ApprovalQueue, tmp_path: Path
    ) -> None:
        other = tmp_path / "README.md"
        other.write_text("hello", encoding="utf-8")
        raw = {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"cat {other}"},
            "tool_use_id": "toolu_1",
        }
        result = evaluate(normalize(raw, installation_id="test-install"), queue)
        assert result.verdict == "allow"

    def test_relative_path_resolved_against_cwd_is_also_caught(
        self, queue: ApprovalQueue, _belay_home: Path
    ) -> None:
        raw = {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cat keys/install1.key"},
            "tool_use_id": "toolu_1",
            "cwd": str(_belay_home),
        }
        result = evaluate(normalize(raw, installation_id="test-install"), queue)
        assert result.verdict == "deny"
        assert "private storage" in result.reason

    def test_reading_a_sibling_of_belay_home_is_not_falsely_blocked(
        self, queue: ApprovalQueue, tmp_path: Path
    ) -> None:
        """Guards against an overly broad prefix check -- a directory that
        merely starts with the same characters as belay_home (but isn't
        actually inside it) must not be treated as belay-internal."""
        sibling = Path(str(tmp_path / "belay-home") + "-not-actually-it")
        sibling.mkdir()
        target = sibling / "innocent.txt"
        target.write_text("hi", encoding="utf-8")
        raw = {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"cat {target}"},
            "tool_use_id": "toolu_1",
        }
        result = evaluate(normalize(raw, installation_id="test-install"), queue)
        assert result.verdict == "allow"


def _mcp_event(
    tool_name: str,
    args: dict[str, object],
    session_id: str = "sess-1",
    cwd: str = "/repo-a",
    event_id: str = "toolu_mcp1",
) -> HookEvent:
    raw = {
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": args,
        "tool_use_id": event_id,
        "cwd": cwd,
    }
    return normalize(raw, installation_id="test-install")


class TestMcpCall:
    """Native `mcp__<server>__<tool>` calls (E18.4) -- always PAUSE, no
    exception for a server that happens to be named "belay", since this
    host-agnostic module has no reliable way to confirm a call actually
    went through belay's own contract-enforcing proxy."""

    def test_unrecognized_mcp_call_pauses_and_queues_approval(self, queue: ApprovalQueue) -> None:
        event = _mcp_event("mcp__github__create_issue", {"title": "x"})
        result = evaluate_mcp_call(event, queue)
        assert result.verdict == "deny"
        items = queue.list()
        assert len(items) == 1
        assert items[0].state == "pending"
        assert items[0].plan["tool"] == "mcp__github__create_issue"
        assert items[0].approval_id == result.approval_id

    def test_a_server_named_belay_gets_no_free_pass(self, queue: ApprovalQueue) -> None:
        event = _mcp_event("mcp__belay__run_step", {"tool": "whatever"})
        result = evaluate_mcp_call(event, queue)
        assert result.verdict == "deny"
        assert len(queue.list()) == 1

    def test_retrying_the_same_pending_call_does_not_create_a_second_item(
        self, queue: ApprovalQueue
    ) -> None:
        event = _mcp_event("mcp__github__create_issue", {"title": "x"})
        evaluate_mcp_call(event, queue)
        result = evaluate_mcp_call(event, queue)
        assert result.verdict == "deny"
        assert "still pending" in result.reason
        assert len(queue.list()) == 1

    def test_approving_then_retrying_the_same_call_allows_it(self, queue: ApprovalQueue) -> None:
        event = _mcp_event("mcp__github__create_issue", {"title": "x"})
        evaluate_mcp_call(event, queue)
        approval_id = queue.list()[0].approval_id
        queue.approve(approval_id, approved_by="jairo")
        result = evaluate_mcp_call(event, queue)
        assert result.verdict == "allow"
        assert result.approval_id == approval_id

    def test_rejecting_then_retrying_stays_denied_without_a_new_item(
        self, queue: ApprovalQueue
    ) -> None:
        event = _mcp_event("mcp__github__create_issue", {"title": "x"})
        evaluate_mcp_call(event, queue)
        approval_id = queue.list()[0].approval_id
        queue.reject(approval_id, rejected_by="jairo", reason="too risky")
        result = evaluate_mcp_call(event, queue)
        assert result.verdict == "deny"
        assert "rejected" in result.reason
        assert len(queue.list()) == 1

    def test_different_arguments_for_the_same_tool_are_a_different_approval(
        self, queue: ApprovalQueue
    ) -> None:
        a = _mcp_event("mcp__github__create_issue", {"title": "x"}, event_id="e1")
        b = _mcp_event("mcp__github__create_issue", {"title": "y"}, event_id="e2")
        evaluate_mcp_call(a, queue)
        approval_id = queue.list()[0].approval_id
        queue.approve(approval_id, approved_by="jairo")

        result_a = evaluate_mcp_call(a, queue)
        result_b = evaluate_mcp_call(b, queue)
        assert result_a.verdict == "allow"
        assert result_b.verdict == "deny"  # different args -- a fresh, still-pending approval
        assert len(queue.list()) == 2

    def test_a_different_repo_never_inherits_an_approval_granted_elsewhere(
        self, queue: ApprovalQueue
    ) -> None:
        a = _mcp_event("mcp__github__create_issue", {"title": "x"}, cwd="/repo-a", event_id="e1")
        b = _mcp_event("mcp__github__create_issue", {"title": "x"}, cwd="/repo-b", event_id="e2")
        evaluate_mcp_call(a, queue)
        queue.approve(queue.list()[0].approval_id, approved_by="jairo")
        result_b = evaluate_mcp_call(b, queue)
        assert result_b.verdict == "deny"

    def test_missing_event_id_denies_without_touching_the_queue(self, queue: ApprovalQueue) -> None:
        event = _mcp_event("mcp__github__create_issue", {"title": "x"}, event_id="")
        result = evaluate_mcp_call(event, queue)
        assert result.verdict == "deny"
        assert queue.list() == []

    def test_post_phase_denies_without_touching_the_queue(self, queue: ApprovalQueue) -> None:
        raw = {
            "session_id": "s1",
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__github__create_issue",
            "tool_input": {"title": "x"},
            "tool_use_id": "toolu_post",
        }
        event = normalize(raw, installation_id="i")
        result = evaluate_mcp_call(event, queue)
        assert result.verdict == "deny"
        assert queue.list() == []


class TestMcpCallContractCheck:
    """R1, ADR 0021's second slice: `contract_set` only ever narrows the
    default PAUSE-everything behavior, never widens it -- a declared,
    all-read contract auto-allows (matching the MCP proxy's own
    readOnlyHint default rule), anything else still pauses exactly as
    before."""

    def test_no_contract_set_is_unchanged_always_pauses(self, queue: ApprovalQueue) -> None:
        event = _mcp_event("mcp__github__list_issues", {})
        result = evaluate_mcp_call(event, queue)
        assert result.verdict == "deny"

    def test_declared_all_read_contract_auto_allows_without_touching_the_queue(
        self, queue: ApprovalQueue
    ) -> None:
        from belay.contracts.model import Contract, ContractSet, Effect

        read_contract = Contract(
            belay_contract="0.1",
            tool="mcp__github__list_issues",
            reversibility="irreversible",
            effects=[Effect(type="read", resource="github.issues")],
        )
        contract_set = ContractSet(
            contracts={"mcp__github__list_issues": read_contract}, set_hash="sha256:read-only"
        )
        event = _mcp_event("mcp__github__list_issues", {})
        result = evaluate_mcp_call(event, queue, contract_set=contract_set)
        assert result.verdict == "allow"
        assert queue.list() == []

    def test_declared_contract_with_a_write_effect_still_pauses(
        self, queue: ApprovalQueue
    ) -> None:
        from belay.contracts.model import Contract, ContractSet, Effect

        write_contract = Contract(
            belay_contract="0.1",
            tool="mcp__github__create_issue",
            reversibility="irreversible",
            effects=[Effect(type="create", resource="github.issues")],
        )
        contract_set = ContractSet(
            contracts={"mcp__github__create_issue": write_contract},
            set_hash="sha256:has-write",
        )
        event = _mcp_event("mcp__github__create_issue", {"title": "x"})
        result = evaluate_mcp_call(event, queue, contract_set=contract_set)
        assert result.verdict == "deny"
        assert len(queue.list()) == 1

    def test_contract_set_configured_but_no_matching_contract_still_pauses(
        self, queue: ApprovalQueue
    ) -> None:
        from belay.contracts.model import ContractSet

        empty_set = ContractSet(contracts={}, set_hash="sha256:empty")
        event = _mcp_event("mcp__github__list_issues", {})
        result = evaluate_mcp_call(event, queue, contract_set=empty_set)
        assert result.verdict == "deny"

    def test_a_server_named_belay_with_no_contract_still_gets_no_free_pass(
        self, queue: ApprovalQueue
    ) -> None:
        from belay.contracts.model import ContractSet

        empty_set = ContractSet(contracts={}, set_hash="sha256:empty")
        event = _mcp_event("mcp__belay__run_step", {"tool": "whatever"})
        result = evaluate_mcp_call(event, queue, contract_set=empty_set)
        assert result.verdict == "deny"


def _file_event(tool_name: str, path: str, session_id: str = "sess-1") -> HookEvent:
    raw = {
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": path, "old_string": "a", "new_string": "b"},
        "tool_use_id": f"toolu_{abs(hash((session_id, tool_name, path)))}",
    }
    return normalize(raw, installation_id="test-install")


class TestFileEditContractCheck:
    """R1 first slice: closes the exact divergence an audit of this module
    against `belay/proxy/lifecycle.py` found -- the MCP proxy's `resolve()`
    denies `contract_missing` for a tool with no declared contract, this
    gate used to allow one unconditionally. `contract_set=None` (the
    default, no `belay hooks install --contracts` configured) must stay
    fully unchanged; only an explicitly configured `ContractSet` turns the
    check on."""

    def _snapshots(self, tmp_path: Path) -> SnapshotStore:
        engine = create_engine("sqlite:///:memory:", future=True)
        return SnapshotStore(engine, tmp_path / "snaps")

    def test_no_contract_set_configured_is_unchanged_allow_by_default(
        self, queue: ApprovalQueue, tmp_path: Path
    ) -> None:
        target = tmp_path / "f.txt"
        target.write_text("original", encoding="utf-8")
        event = _file_event("Write", str(target))
        result = evaluate_file_edit(event, queue, self._snapshots(tmp_path))
        assert result.verdict == "allow"
        assert queue.list() == []

    def test_contract_set_configured_but_no_matching_contract_denies(
        self, queue: ApprovalQueue, tmp_path: Path
    ) -> None:
        target = tmp_path / "f.txt"
        target.write_text("original", encoding="utf-8")
        event = _file_event("Write", str(target))
        empty_set = ContractSet(contracts={}, set_hash="sha256:empty")

        snapshots = self._snapshots(tmp_path)
        result = evaluate_file_edit(event, queue, snapshots, contract_set=empty_set)

        assert result.verdict == "deny"
        assert "contract_missing" in result.reason
        assert "'Write'" in result.reason
        # Denied before ever reaching capture -- no snapshot, no queued item.
        assert snapshots.get(event.event_id) is None
        assert queue.list() == []

    def test_contract_set_configured_with_a_matching_contract_falls_through_to_allow(
        self, queue: ApprovalQueue, tmp_path: Path
    ) -> None:
        from belay.contracts.model import Contract, Effect

        target = tmp_path / "f.txt"
        target.write_text("original", encoding="utf-8")
        event = _file_event("Write", str(target))
        write_contract = Contract(
            belay_contract="0.1",
            tool="Write",
            reversibility="irreversible",
            effects=[Effect(type="update", resource="native.file")],
        )
        configured_set = ContractSet(
            contracts={"Write": write_contract}, set_hash="sha256:has-write"
        )

        snapshots = self._snapshots(tmp_path)
        result = evaluate_file_edit(event, queue, snapshots, contract_set=configured_set)

        assert result.verdict == "allow"
        assert snapshots.get(event.event_id) is not None

    def test_contract_missing_denial_happens_before_the_no_path_check(
        self, queue: ApprovalQueue, tmp_path: Path
    ) -> None:
        """A call with neither a resolvable path nor a matching contract
        must report contract_missing, not the unrelated "no recognizable
        path argument" reason -- the config problem is the one worth
        surfacing first."""
        raw = {
            "session_id": "s",
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {},  # no file_path/path key at all
            "tool_use_id": "toolu_nopath",
        }
        event = normalize(raw, installation_id="test-install")
        empty_set = ContractSet(contracts={}, set_hash="sha256:empty")
        result = evaluate_file_edit(event, queue, self._snapshots(tmp_path), contract_set=empty_set)
        assert result.verdict == "deny"
        assert "contract_missing" in result.reason


class TestQuotaEnforcement:
    """R1 fourth slice (ADR 0023): `quota` only ever escalates a *new*
    pause to a hard deny -- never touches an existing pending/approved/
    rejected lookup, and never applies when no quota is configured
    (`quota=None`, the default, is fully unchanged legacy behavior)."""

    def _quota_at_max(self, max_actions: int = 2) -> QuotaConfig:
        from belay.ledger.store import LedgerStore
        from belay.supervisor.protocol import local_os_user

        ledger = LedgerStore()
        os_user = local_os_user()
        for i in range(max_actions):
            ledger.append(
                f"quota-seed-{i}",
                "hook_pre_tool_use",
                {"os_user": os_user, "verdict": "deny", "approval_id": f"ap-seed-{i}"},
            )
            ledger.append(
                f"quota-seed-{i}",
                "approval_resolved",
                {"approval_id": f"ap-seed-{i}", "state": "approved"},
            )
        return QuotaConfig(ledger=ledger, max_actions=max_actions, window=timedelta(days=1))

    def test_bash_new_pause_denies_hard_when_quota_exceeded_no_item_queued(
        self, queue: ApprovalQueue
    ) -> None:
        quota = self._quota_at_max()
        result = evaluate(_event("rm -rf /tmp/x"), queue, quota=quota)
        assert result.verdict == "deny"
        assert "quota exceeded" in result.reason
        assert queue.list() == []

    def test_bash_below_quota_still_queues_normally(self, queue: ApprovalQueue) -> None:
        from belay.ledger.store import LedgerStore

        quota = QuotaConfig(ledger=LedgerStore(), max_actions=2, window=timedelta(days=1))
        result = evaluate(_event("rm -rf /tmp/x"), queue, quota=quota)
        assert result.verdict == "deny"
        assert "quota exceeded" not in result.reason
        assert len(queue.list()) == 1

    def test_no_quota_configured_is_unchanged(self, queue: ApprovalQueue) -> None:
        result = evaluate(_event("rm -rf /tmp/x"), queue, quota=None)
        assert result.verdict == "deny"
        assert "quota exceeded" not in result.reason
        assert len(queue.list()) == 1

    def test_quota_does_not_touch_an_already_pending_item(self, queue: ApprovalQueue) -> None:
        event = _event("rm -rf /tmp/x")
        evaluate(event, queue)  # creates the pending item, no quota involved
        quota = self._quota_at_max()
        result = evaluate(event, queue, quota=quota)
        assert result.verdict == "deny"
        assert "still pending" in result.reason
        assert len(queue.list()) == 1  # not a second item, and not a quota denial either

    def test_mcp_call_new_pause_denies_hard_when_quota_exceeded(
        self, queue: ApprovalQueue
    ) -> None:
        quota = self._quota_at_max()
        event = _mcp_event("mcp__github__create_issue", {"title": "x"})
        result = evaluate_mcp_call(event, queue, quota=quota)
        assert result.verdict == "deny"
        assert "quota exceeded" in result.reason
        assert queue.list() == []

    def test_oversized_file_edit_new_pause_denies_hard_when_quota_exceeded(
        self, queue: ApprovalQueue, tmp_path: Path
    ) -> None:
        engine = create_engine("sqlite:///:memory:", future=True)
        snapshots = SnapshotStore(engine, tmp_path / "snaps")
        big_file = tmp_path / "big.bin"
        big_file.write_bytes(b"x" * (6 * 1024 * 1024))  # over MAX_SNAPSHOT_BYTES (5 MiB)
        event = _file_event("Write", str(big_file))

        quota = self._quota_at_max()
        result = evaluate_file_edit(event, queue, snapshots, quota=quota)
        assert result.verdict == "deny"
        assert "quota exceeded" in result.reason
        assert queue.list() == []


class TestLedgerEvidenceHelpers:
    """gate.py's pure evidence-shaping functions (belay/supervisor/server.py
    owns the actual LedgerStore.append() calls; these are tested in
    isolation from any real ledger/store)."""

    def test_ledger_session_id_is_prefixed_and_namespaced_by_host(self) -> None:
        event = _event("git status", session_id="abc123")
        assert ledger_session_id(event) == "hook-claude-code-abc123"

    def test_session_key_matches_ledger_session_id_for_the_same_event(self) -> None:
        """`belay hooks fence` (R1 third slice) computes the fencing key from
        a bare host/host_session_id pair via `session_key`, not a full
        `HookEvent` -- must match `ledger_session_id`'s own key exactly, or
        fencing a session by its printed session id would silently fence
        the wrong ledger key."""
        event = _event("git status", session_id="abc123")
        assert session_key(event.host, event.host_session_id) == ledger_session_id(event)

    def test_ledger_session_id_never_collides_across_hosts_for_the_same_session_string(
        self,
    ) -> None:
        a = _event("git status", session_id="shared-id")
        # Simulate a different host reusing the same session_id string via
        # dataclasses.replace (normalize() always sets host="claude-code"
        # today, but the function itself must still discriminate on host).
        import dataclasses

        b = dataclasses.replace(a, host="codex")
        assert ledger_session_id(a) != ledger_session_id(b)

    def test_pre_event_evidence_captures_the_decision_and_context(
        self, queue: ApprovalQueue
    ) -> None:
        event = _event("git status")
        decision = GateDecision("allow", "belay: matches safe-read allowlist entry: git status")
        evidence = pre_event_evidence(event, decision)
        assert evidence["verdict"] == "allow"
        assert evidence["reason"] == decision.reason
        assert evidence["tool_name"] == "Bash"
        assert evidence["cwd"] == event.cwd
        assert evidence["event_id"] == event.event_id

    def test_pre_event_evidence_includes_approval_id_when_present(self) -> None:
        event = _event("rm -rf /tmp/x")
        decision = GateDecision("deny", "belay: paused", approval_id="ap_123")
        evidence = pre_event_evidence(event, decision)
        assert evidence["approval_id"] == "ap_123"

    def test_post_event_evidence_captures_result_and_duration(self) -> None:
        raw = {
            "session_id": "s1",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_use_id": "toolu_post",
            "tool_response": {"exit_code": 0, "stdout": "clean"},
        }
        event = normalize(raw, installation_id="i")
        evidence = post_event_evidence(event, duration_ms=42.5)
        assert evidence["exit_code"] == 0
        assert evidence["result_status"] == "success"
        assert evidence["duration_ms"] == 42.5
        assert evidence["output_digest"] == event.output_digest

    def test_post_event_evidence_duration_none_when_no_matching_pre_seen(self) -> None:
        raw = {
            "session_id": "s1",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_use_id": "toolu_post",
        }
        event = normalize(raw, installation_id="i")
        evidence = post_event_evidence(event, duration_ms=None)
        assert evidence["duration_ms"] is None
