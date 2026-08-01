"""End-to-end lifecycle tests for `belay hooks install/uninstall/doctor` and
`belay hooks run` (plan-v2 E18, first slice: Claude Code / Bash only).

Mirrors `test_init_uninstall_lifecycle.py`'s rigor -- `belay hooks
install/uninstall` reuse the exact same manifest/atomic-write safety
guarantees as `belay init`/`belay uninstall` (see `_write_client_config` in
belay/cli/main.py), so the same regression classes apply: reinstall must not
clobber the original backup, a config that never existed pre-install must be
deleted (not left as a stub) on uninstall, and doctor must catch a manifest
that no longer matches reality.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from belay.cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    # Without this, every test here (and the real subprocess `belay hooks
    # run` spawns) resolves BELAY_HOME to this machine's REAL
    # ~/.belay / %LOCALAPPDATA%\belay -- confirmed the hard way: an earlier
    # run of this suite left dozens of real key/data files there.
    # subprocess.Popen inherits os.environ by default, so this env var
    # reaches the detached supervisor process too, not just this process.
    monkeypatch.setenv("BELAY_HOME", str(tmp_path / "belay-home"))
    yield
    # `belay hooks run` may have spawned a real detached supervisor process
    # (tests below that exercise it end-to-end deliberately do, to prove the
    # spawn+connect+respond round-trip actually works, not just the
    # decision logic) -- stop it so test runs don't accumulate orphaned
    # background processes. Harmless (a no-op) for tests that never touched
    # a supervisor at all.
    from belay.supervisor.addressing import supervisor_identity
    from belay.supervisor.client import send_shutdown

    for db_name in ("test.db", "belay-hooks.db"):
        send_shutdown(supervisor_identity((tmp_path / db_name).resolve()))


def _settings_path(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.json"


def test_install_creates_settings_with_pretooluse_and_posttooluse_hooks(tmp_path: Path) -> None:
    result = runner.invoke(app, ["hooks", "install", "--yes"])
    assert result.exit_code == 0, result.output
    target = _settings_path(tmp_path)
    assert target.is_file()
    doc = json.loads(target.read_text(encoding="utf-8"))
    for hook_event in ("PreToolUse", "PostToolUse"):
        entry = doc["hooks"][hook_event][0]
        assert entry["matcher"] == "Bash"
        command = entry["hooks"][0]["command"]
        assert "belay.cli.main hooks run" in command
        assert f"hooks run {hook_event}" in command


def test_install_install_uninstall_restores_original_exact(tmp_path: Path) -> None:
    target = _settings_path(tmp_path)
    target.parent.mkdir(parents=True)
    original = json.dumps({"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": []}]}}) + "\n"
    target.write_text(original, encoding="utf-8")

    assert runner.invoke(app, ["hooks", "install", "--yes"]).exit_code == 0
    assert runner.invoke(app, ["hooks", "install", "--yes"]).exit_code == 0  # reinstall

    result = runner.invoke(app, ["hooks", "uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8") == original


def test_install_on_missing_file_then_uninstall_removes_file(tmp_path: Path) -> None:
    target = _settings_path(tmp_path)
    assert not target.is_file()

    assert runner.invoke(app, ["hooks", "install", "--yes"]).exit_code == 0
    assert target.is_file()

    result = runner.invoke(app, ["hooks", "uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert not target.is_file()


def test_uninstall_after_external_addition_is_surgical(tmp_path: Path) -> None:
    target = _settings_path(tmp_path)
    assert runner.invoke(app, ["hooks", "install", "--yes"]).exit_code == 0

    doc = json.loads(target.read_text(encoding="utf-8"))
    doc["hooks"]["PostToolUse"] = [{"matcher": "*", "hooks": [{"command": "my-lint.sh"}]}]
    target.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    result = runner.invoke(app, ["hooks", "uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["hooks"].get("PreToolUse", []) == []
    assert doc["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "my-lint.sh"


def test_doctor_reports_broken_when_entry_hand_removed(tmp_path: Path) -> None:
    target = _settings_path(tmp_path)
    assert runner.invoke(app, ["hooks", "install", "--yes"]).exit_code == 0

    target.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")

    result = runner.invoke(app, ["hooks", "doctor"])
    assert result.exit_code == 0, result.output
    assert "BROKEN" in result.output


def test_doctor_reports_unchanged_since_install(tmp_path: Path) -> None:
    assert runner.invoke(app, ["hooks", "install", "--yes"]).exit_code == 0
    result = runner.invoke(app, ["hooks", "doctor"])
    assert "unchanged since install" in result.output


class TestDeepDoctor:
    """E19.3: `belay hooks doctor --deep` -- real reachability checks
    (interpreter exists and can import belay, supervisor is genuinely
    reachable), not just "the config file parses correctly"."""

    def test_without_deep_flag_behavior_is_unchanged(self, tmp_path: Path) -> None:
        assert runner.invoke(app, ["hooks", "install", "--yes"]).exit_code == 0
        result = runner.invoke(app, ["hooks", "doctor"])
        assert result.exit_code == 0, result.output
        assert "DEEP CHECK" not in result.output
        assert "deep check" not in result.output

    def test_deep_check_passes_for_a_real_working_install(self, tmp_path: Path) -> None:
        """The install this suite itself creates uses sys.executable (a
        real, working interpreter with belay importable, since that's
        what's running this test) -- --deep should report success, and
        genuinely spawn/connect to a real supervisor as a side effect."""
        assert runner.invoke(app, ["hooks", "install", "--yes"]).exit_code == 0
        result = runner.invoke(app, ["hooks", "doctor", "--deep"])
        assert result.exit_code == 0, result.output
        assert "deep check OK" in result.output
        assert "DEEP CHECK FAILED" not in result.output

    def test_deep_check_reports_a_missing_interpreter(self, tmp_path: Path) -> None:
        """Simulates a moved/uninstalled interpreter without needing to
        actually break the real one running this test -- hand-crafts a
        settings.json pointing at a path that doesn't exist."""
        from belay.cli.client_configs import write_manifest

        target = _settings_path(tmp_path)
        target.parent.mkdir(parents=True)
        bogus_python = str(tmp_path / "no-such-python.exe")
        command = f'"{bogus_python}" -m belay.cli.main hooks run PreToolUse --db "belay-hooks.db"'
        hook_entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}
        doc = {"hooks": {"PreToolUse": [hook_entry], "PostToolUse": [hook_entry]}}
        text = json.dumps(doc, indent=2) + "\n"
        target.write_text(text, encoding="utf-8")
        write_manifest("claude-code-hooks", target, "belay-hooks", None, text, None)

        result = runner.invoke(app, ["hooks", "doctor", "--deep"])
        assert result.exit_code == 0, result.output
        assert "DEEP CHECK FAILED" in result.output
        assert "interpreter not found" in result.output


def test_hooks_run_pretooluse_allows_safe_command_end_to_end(tmp_path: Path) -> None:
    payload = json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_use_id": "toolu_allow_test",
        }
    )
    result = runner.invoke(app, ["hooks", "run", "PreToolUse", "--db", "test.db"], input=payload)
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_hooks_run_pretooluse_denies_and_queues_unsafe_command(tmp_path: Path) -> None:
    payload = json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/x"},
            "tool_use_id": "toolu_deny_test",
        }
    )
    result = runner.invoke(app, ["hooks", "run", "PreToolUse", "--db", "test.db"], input=payload)
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    # "test.db" is only ever an identity anchor now (P0 fix: the real data
    # lives outside the project, never in a file the gated agent's own
    # tools could reach) -- resolve the real path the same way `belay hooks
    # install`'s own printed message tells a human to.
    from belay.supervisor.addressing import supervisor_identity

    data_path = supervisor_identity((tmp_path / "test.db").resolve()).data_path
    assert not (tmp_path / "test.db").exists()  # nothing was ever created in the project

    list_result = runner.invoke(app, ["approvals", "list", "--db", str(data_path)])
    assert "pending" in list_result.output
    assert "rm -rf /tmp/x" not in list_result.output  # plan dict isn't dumped raw, just tool/id


def test_hooks_run_posttooluse_records_evidence_and_acks_empty(tmp_path: Path) -> None:
    pre_payload = json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_use_id": "toolu_post_e2e",
        }
    )
    pre_result = runner.invoke(
        app, ["hooks", "run", "PreToolUse", "--db", "test.db"], input=pre_payload
    )
    assert pre_result.exit_code == 0, pre_result.output

    post_payload = json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_use_id": "toolu_post_e2e",
            "tool_response": {"exit_code": 0, "stdout": "clean", "stderr": ""},
        }
    )
    post_result = runner.invoke(
        app, ["hooks", "run", "PostToolUse", "--db", "test.db"], input=post_payload
    )
    assert post_result.exit_code == 0, post_result.output
    # PostToolUse never has a permission decision to make -- an empty ack.
    assert json.loads(post_result.stdout) == {}

    from belay.ledger.store import LedgerStore
    from belay.supervisor.addressing import supervisor_identity

    data_path = supervisor_identity((tmp_path / "test.db").resolve()).data_path
    ledger = LedgerStore(db_url=f"sqlite:///{data_path}")
    events = ledger.read("hook-claude-code-s1")
    types = [e.type for e in events]
    assert types == ["hook_pre_tool_use", "hook_post_tool_use"]

    pre_event, post_event = events
    assert pre_event.payload["verdict"] == "allow"
    assert post_event.payload["exit_code"] == 0
    assert post_event.payload["result_status"] == "success"
    assert post_event.payload["duration_ms"] is not None  # correlated with the pre event
    assert post_event.payload["duration_ms"] >= 0


def test_hooks_run_posttooluse_without_matching_pretooluse_records_null_duration(
    tmp_path: Path,
) -> None:
    post_payload = json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_use_id": "toolu_orphan_post",
            "tool_response": {"exit_code": 0},
        }
    )
    result = runner.invoke(
        app, ["hooks", "run", "PostToolUse", "--db", "test.db"], input=post_payload
    )
    assert result.exit_code == 0, result.output

    from belay.ledger.store import LedgerStore
    from belay.supervisor.addressing import supervisor_identity

    data_path = supervisor_identity((tmp_path / "test.db").resolve()).data_path
    ledger = LedgerStore(db_url=f"sqlite:///{data_path}")
    events = ledger.read("hook-claude-code-s1")
    assert len(events) == 1
    assert events[0].payload["duration_ms"] is None


def test_hooks_run_posttooluse_retry_does_not_duplicate_ledger_entry(tmp_path: Path) -> None:
    payload = json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_use_id": "toolu_retry_post",
            "tool_response": {"exit_code": 0},
        }
    )
    first = runner.invoke(app, ["hooks", "run", "PostToolUse", "--db", "test.db"], input=payload)
    second = runner.invoke(app, ["hooks", "run", "PostToolUse", "--db", "test.db"], input=payload)
    assert first.exit_code == 0 and second.exit_code == 0

    from belay.ledger.store import LedgerStore
    from belay.supervisor.addressing import supervisor_identity

    data_path = supervisor_identity((tmp_path / "test.db").resolve()).data_path
    ledger = LedgerStore(db_url=f"sqlite:///{data_path}")
    events = ledger.read("hook-claude-code-s1")
    assert len(events) == 1  # idempotency (ARCH-006) applies to post events too, not just pre


def test_hooks_run_unhandled_event_exits_zero_with_no_output() -> None:
    result = runner.invoke(app, ["hooks", "run", "SessionStart", "--db", "test.db"], input="{}")
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_hooks_run_malformed_stdin_exits_zero_with_no_output() -> None:
    result = runner.invoke(
        app, ["hooks", "run", "PreToolUse", "--db", "test.db"], input="not json"
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_install_reject_unsupported_client() -> None:
    result = runner.invoke(app, ["hooks", "install", "--client", "codex", "--yes"])
    assert result.exit_code != 0
    assert "unsupported" in result.output


class TestInstallWithContracts:
    """R1 first slice: `belay hooks install --contracts <file>` -- opt-in,
    off by default. Validates the contracts file up front (nothing written
    on a bad one) and records its path in the per-install pointer file
    `belay/supervisor/server.py::Supervisor._load_contract_set` reads."""

    def _contracts_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "hook-contracts.yaml"
        path.write_text(
            "belay_contract: '0.1'\n"
            "tool: Write\n"
            "reversibility: irreversible\n"
            "effects:\n"
            "  - type: update\n"
            "    resource: native.file\n",
            encoding="utf-8",
        )
        return path

    def test_valid_contracts_file_is_recorded_in_the_pointer_file(
        self, tmp_path: Path
    ) -> None:
        contracts = self._contracts_file(tmp_path)
        result = runner.invoke(
            app, ["hooks", "install", "--contracts", str(contracts), "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert "will now be checked against" in result.output

        from belay.supervisor.addressing import supervisor_identity

        identity = supervisor_identity((tmp_path / "belay-hooks.db").resolve())
        assert identity.contracts_pointer_path.is_file()
        assert identity.contracts_pointer_path.read_text(encoding="utf-8").strip() == str(
            contracts.resolve()
        )

    def test_invalid_contracts_file_fails_and_writes_nothing(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("belay_contract: '0.1'\ntool: Write\n", encoding="utf-8")  # missing effects

        result = runner.invoke(app, ["hooks", "install", "--contracts", str(bad), "--yes"])
        assert result.exit_code != 0
        assert "invalid" in result.output
        assert not _settings_path(tmp_path).is_file()

        from belay.supervisor.addressing import supervisor_identity

        identity = supervisor_identity((tmp_path / "belay-hooks.db").resolve())
        assert not identity.contracts_pointer_path.is_file()

    def test_nonexistent_contracts_file_fails_and_writes_nothing(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["hooks", "install", "--contracts", str(tmp_path / "missing.yaml"), "--yes"],
        )
        assert result.exit_code != 0
        assert not _settings_path(tmp_path).is_file()

    def test_omitting_contracts_writes_no_pointer_file(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["hooks", "install", "--yes"])
        assert result.exit_code == 0, result.output

        from belay.supervisor.addressing import supervisor_identity

        identity = supervisor_identity((tmp_path / "belay-hooks.db").resolve())
        assert not identity.contracts_pointer_path.is_file()


class TestFileEditContractCheckEndToEnd:
    """R1 first slice, exercised through the real CLI end to end: `belay
    hooks install --contracts <file>` followed by a real `belay hooks run`
    against a real (detached, subprocess-spawned) supervisor -- not just
    the unit-level `evaluate_file_edit`/`Supervisor._load_contract_set`
    coverage elsewhere. Proves the pointer file written at install time is
    actually picked up by the supervisor process that later spawns."""

    def _install_with_contracts(self, tmp_path: Path) -> None:
        contracts = tmp_path / "hook-contracts.yaml"
        contracts.write_text(
            "belay_contract: '0.1'\n"
            "tool: Write\n"
            "reversibility: irreversible\n"
            "effects:\n"
            "  - type: update\n"
            "    resource: native.file\n",
            encoding="utf-8",
        )
        result = runner.invoke(
            app, ["hooks", "install", "--contracts", str(contracts), "--yes"]
        )
        assert result.exit_code == 0, result.output

    def _run_pre(self, tool_name: str, target: Path, event_id: str) -> dict:
        payload = json.dumps(
            {
                "session_id": "s1",
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": {"file_path": str(target), "content": "new content"},
                "tool_use_id": event_id,
            }
        )
        result = runner.invoke(
            app, ["hooks", "run", "PreToolUse", "--db", "belay-hooks.db"], input=payload
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)  # type: ignore[no-any-return]

    def test_tool_with_a_declared_contract_is_allowed(self, tmp_path: Path) -> None:
        self._install_with_contracts(tmp_path)
        target = tmp_path / "f.txt"
        out = self._run_pre("Write", target, "toolu_has_contract")
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_tool_with_no_declared_contract_is_denied_contract_missing(
        self, tmp_path: Path
    ) -> None:
        self._install_with_contracts(tmp_path)  # only declares "Write", not "Edit"
        target = tmp_path / "f.txt"
        target.write_text("original", encoding="utf-8")
        out = self._run_pre("Edit", target, "toolu_no_contract")
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "contract_missing" in out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_without_contracts_configured_edit_is_still_allowed_by_default(
        self, tmp_path: Path
    ) -> None:
        result = runner.invoke(app, ["hooks", "install", "--yes"])  # no --contracts
        assert result.exit_code == 0, result.output
        target = tmp_path / "f.txt"
        out = self._run_pre("Edit", target, "toolu_legacy_default")
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestMcpCallContractCheckEndToEnd:
    """R1, ADR 0021's second slice, end to end through the real CLI and a
    real spawned supervisor: a native `mcp__server__tool` call declared
    all-read in the configured contracts file auto-allows; anything else
    (no contract, or a contract with a non-read effect) still pauses,
    exactly like before this slice existed."""

    def _install_with_read_only_mcp_contract(self, tmp_path: Path) -> None:
        contracts = tmp_path / "hook-contracts.yaml"
        contracts.write_text(
            "belay_contract: '0.1'\n"
            "tool: mcp__github__list_issues\n"
            "reversibility: irreversible\n"
            "effects:\n"
            "  - type: read\n"
            "    resource: github.issues\n",
            encoding="utf-8",
        )
        result = runner.invoke(
            app, ["hooks", "install", "--contracts", str(contracts), "--yes"]
        )
        assert result.exit_code == 0, result.output

    def _run_pre_mcp(self, tool_name: str, args: dict, event_id: str) -> dict:
        payload = json.dumps(
            {
                "session_id": "s1",
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": args,
                "tool_use_id": event_id,
            }
        )
        result = runner.invoke(
            app, ["hooks", "run", "PreToolUse", "--db", "belay-hooks.db"], input=payload
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)  # type: ignore[no-any-return]

    def test_declared_read_only_mcp_tool_auto_allows(self, tmp_path: Path) -> None:
        self._install_with_read_only_mcp_contract(tmp_path)
        out = self._run_pre_mcp("mcp__github__list_issues", {}, "toolu_mcp_read")
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_mcp_tool_not_in_the_contract_set_still_pauses(self, tmp_path: Path) -> None:
        self._install_with_read_only_mcp_contract(tmp_path)  # only declares list_issues
        out = self._run_pre_mcp(
            "mcp__github__create_issue", {"title": "x"}, "toolu_mcp_undeclared"
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_without_contracts_configured_mcp_call_still_pauses(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["hooks", "install", "--yes"])  # no --contracts
        assert result.exit_code == 0, result.output
        out = self._run_pre_mcp("mcp__github__list_issues", {}, "toolu_mcp_legacy")
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestHooksFence:
    """R1 third slice (ADR 0021): `belay hooks fence <session>` closes a
    hook session to every surface, end to end through the real CLI and a
    real spawned supervisor."""

    def _run_pre(self, tool_name: str, tool_input: dict, session_id: str, event_id: str) -> dict:
        payload = json.dumps(
            {
                "session_id": session_id,
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": event_id,
            }
        )
        result = runner.invoke(
            app, ["hooks", "run", "PreToolUse", "--db", "belay-hooks.db"], input=payload
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)  # type: ignore[no-any-return]

    def test_fence_then_new_bash_call_in_that_session_is_denied(self, tmp_path: Path) -> None:
        assert runner.invoke(app, ["hooks", "install", "--yes"]).exit_code == 0
        # Establish the session first (a real Bash call, allowed).
        out = self._run_pre("Bash", {"command": "git status"}, "s-fence-1", "toolu_pre_fence")
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

        fence = runner.invoke(app, ["hooks", "fence", "s-fence-1", "--yes"])
        assert fence.exit_code == 0, fence.output
        assert "fenced s-fence-1" in fence.output

        out = self._run_pre("Bash", {"command": "git status"}, "s-fence-1", "toolu_post_fence")
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "fenced" in out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_fencing_one_session_does_not_affect_another(self, tmp_path: Path) -> None:
        assert runner.invoke(app, ["hooks", "install", "--yes"]).exit_code == 0
        fence = runner.invoke(app, ["hooks", "fence", "s-fence-2", "--yes"])
        assert fence.exit_code == 0, fence.output

        out = self._run_pre(
            "Bash", {"command": "git status"}, "a-different-session", "toolu_unrelated"
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_fencing_an_already_fenced_session_is_a_no_op_not_an_error(
        self, tmp_path: Path
    ) -> None:
        assert runner.invoke(app, ["hooks", "install", "--yes"]).exit_code == 0
        first = runner.invoke(app, ["hooks", "fence", "s-fence-3", "--yes"])
        assert first.exit_code == 0, first.output

        second = runner.invoke(app, ["hooks", "fence", "s-fence-3", "--yes"])
        assert second.exit_code == 0, second.output
        assert "already fenced" in second.output


class TestFileEditRewind:
    """E18.3: native Edit/Write calls are captured for rewind, end-to-end
    through the real CLI, two separate `belay hooks run` invocations (pre
    then post) exactly like a real host would call this."""

    def _run_pre_edit(self, target: Path, event_id: str) -> None:
        payload = json.dumps(
            {
                "session_id": "s1",
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target), "content": "new content"},
                "tool_use_id": event_id,
            }
        )
        result = runner.invoke(
            app, ["hooks", "run", "PreToolUse", "--db", "test.db"], input=payload
        )
        assert result.exit_code == 0, result.output
        out = json.loads(result.stdout)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def _run_post_edit(self, target: Path, event_id: str) -> None:
        payload = json.dumps(
            {
                "session_id": "s1",
                "hook_event_name": "PostToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target), "content": "new content"},
                "tool_use_id": event_id,
            }
        )
        result = runner.invoke(
            app, ["hooks", "run", "PostToolUse", "--db", "test.db"], input=payload
        )
        assert result.exit_code == 0, result.output

    def test_edit_of_existing_file_can_be_rewound(self, tmp_path: Path) -> None:
        target = tmp_path / "existing.txt"
        target.write_text("original content", encoding="utf-8")

        self._run_pre_edit(target, "toolu_edit_1")
        target.write_text("new content", encoding="utf-8")  # simulates the tool actually running
        self._run_post_edit(target, "toolu_edit_1")

        result = runner.invoke(app, ["hooks", "rewind", "toolu_edit_1", "--db", "test.db", "--yes"])
        assert result.exit_code == 0, result.output
        assert target.read_text(encoding="utf-8") == "original content"

    def test_edit_of_new_file_rewind_deletes_it(self, tmp_path: Path) -> None:
        target = tmp_path / "brand-new.txt"
        assert not target.exists()

        self._run_pre_edit(target, "toolu_edit_2")
        target.write_text("new content", encoding="utf-8")
        self._run_post_edit(target, "toolu_edit_2")

        result = runner.invoke(app, ["hooks", "rewind", "toolu_edit_2", "--db", "test.db", "--yes"])
        assert result.exit_code == 0, result.output
        assert not target.exists()

    def test_rewind_refuses_when_file_changed_again_since(self, tmp_path: Path) -> None:
        target = tmp_path / "existing.txt"
        target.write_text("original", encoding="utf-8")

        self._run_pre_edit(target, "toolu_edit_3")
        target.write_text("new content", encoding="utf-8")
        self._run_post_edit(target, "toolu_edit_3")

        target.write_text("touched again by something else", encoding="utf-8")

        result = runner.invoke(app, ["hooks", "rewind", "toolu_edit_3", "--db", "test.db", "--yes"])
        assert result.exit_code == 1
        assert "conflict" in result.output
        assert target.read_text(encoding="utf-8") == "touched again by something else"

    def test_list_edits_shows_captured_events(self, tmp_path: Path) -> None:
        target = tmp_path / "f.txt"
        target.write_text("v1", encoding="utf-8")
        self._run_pre_edit(target, "toolu_edit_list")
        target.write_text("v2", encoding="utf-8")
        self._run_post_edit(target, "toolu_edit_list")

        result = runner.invoke(app, ["hooks", "list-edits", "--db", "test.db"])
        assert result.exit_code == 0, result.output
        assert "toolu_edit_list" in result.output
        assert "recorded" in result.output

    def test_rewind_of_unknown_event_id_fails_cleanly(self) -> None:
        result = runner.invoke(
            app, ["hooks", "rewind", "no-such-event", "--db", "test.db", "--yes"]
        )
        assert result.exit_code == 1
        assert "no captured edit" in result.output

    def test_oversized_file_edit_is_paused_not_silently_allowed(self, tmp_path: Path) -> None:
        from belay.hooks.file_snapshot import MAX_SNAPSHOT_BYTES

        target = tmp_path / "huge.bin"
        target.write_bytes(b"x" * (MAX_SNAPSHOT_BYTES + 1))

        payload = json.dumps(
            {
                "session_id": "s1",
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
                "tool_use_id": "toolu_huge",
            }
        )
        result = runner.invoke(
            app, ["hooks", "run", "PreToolUse", "--db", "test.db"], input=payload
        )
        assert result.exit_code == 0, result.output
        out = json.loads(result.stdout)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "capture size cap" in out["hookSpecificOutput"]["permissionDecisionReason"]


class TestNativeMcpCallGate:
    """E18.4: native `mcp__<server>__<tool>` calls, end-to-end through the
    real CLI -- `belay hooks run` for the gate decision, `belay hooks
    approvals` (not the top-level `belay approvals`, which opens --db as a
    literal file rather than resolving the project-identity anchor) to
    review/approve/reject."""

    def _run_mcp_pre(self, tool_name: str, args: dict, event_id: str) -> dict:
        payload = json.dumps(
            {
                "session_id": "s1",
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": args,
                "tool_use_id": event_id,
            }
        )
        result = runner.invoke(
            app, ["hooks", "run", "PreToolUse", "--db", "test.db"], input=payload
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)["hookSpecificOutput"]

    def test_native_mcp_call_pauses_and_is_reviewable_via_hooks_approvals(self) -> None:
        out = self._run_mcp_pre(
            "mcp__github__create_issue", {"title": "x"}, "toolu_mcp_1"
        )
        assert out["permissionDecision"] == "deny"

        listed = runner.invoke(app, ["hooks", "approvals", "list", "--db", "test.db"])
        assert listed.exit_code == 0, listed.output
        assert "mcp__github__create_issue" in listed.output
        assert "pending" in listed.output

    def test_top_level_approvals_list_does_not_see_hook_queued_items(self) -> None:
        """Regression: `belay approvals list --db test.db` opens `test.db`
        literally -- hook-queued items live in the private belay-home data
        file instead, so the top-level command must NOT see them (silently
        finding them there would mean two commands both claim to be the
        source of truth for the same queue, from two different files)."""
        self._run_mcp_pre("mcp__github__create_issue", {"title": "x"}, "toolu_mcp_2")

        result = runner.invoke(app, ["approvals", "list", "--db", "test.db"])
        assert result.exit_code == 0, result.output
        assert "no approval items" in result.output

    def test_approving_via_hooks_approvals_allows_the_retry(self) -> None:
        self._run_mcp_pre("mcp__github__create_issue", {"title": "x"}, "toolu_mcp_3")
        listed = runner.invoke(app, ["hooks", "approvals", "list", "--db", "test.db"])
        approval_id = listed.output.split()[0]

        approve = runner.invoke(
            app, ["hooks", "approvals", "approve", approval_id, "--db", "test.db"]
        )
        assert approve.exit_code == 0, approve.output
        assert "approved" in approve.output

        out = self._run_mcp_pre("mcp__github__create_issue", {"title": "x"}, "toolu_mcp_3b")
        assert out["permissionDecision"] == "allow"

    def test_rejecting_via_hooks_approvals_stays_denied(self) -> None:
        self._run_mcp_pre("mcp__github__create_issue", {"title": "x"}, "toolu_mcp_4")
        listed = runner.invoke(app, ["hooks", "approvals", "list", "--db", "test.db"])
        approval_id = listed.output.split()[0]

        reject = runner.invoke(
            app, ["hooks", "approvals", "reject", approval_id, "--db", "test.db"]
        )
        assert reject.exit_code == 0, reject.output
        assert "rejected" in reject.output

        out = self._run_mcp_pre("mcp__github__create_issue", {"title": "x"}, "toolu_mcp_4b")
        assert out["permissionDecision"] == "deny"
        assert "rejected" in out["permissionDecisionReason"]

    def test_a_server_named_belay_still_pauses(self) -> None:
        """No exception for the name "belay" -- this layer can't confirm a
        native call actually reached belay's own proxy, so it doesn't try."""
        out = self._run_mcp_pre("mcp__belay__run_step", {"tool": "x"}, "toolu_mcp_5")
        assert out["permissionDecision"] == "deny"
