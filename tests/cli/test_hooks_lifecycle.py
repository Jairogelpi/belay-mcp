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
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def _settings_path(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.json"


def test_install_creates_settings_with_pretooluse_hook(tmp_path: Path) -> None:
    result = runner.invoke(app, ["hooks", "install", "--yes"])
    assert result.exit_code == 0, result.output
    target = _settings_path(tmp_path)
    assert target.is_file()
    doc = json.loads(target.read_text(encoding="utf-8"))
    entry = doc["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    assert "belay.cli.main hooks run" in entry["hooks"][0]["command"]


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


def test_hooks_run_pretooluse_allows_safe_command_end_to_end(tmp_path: Path) -> None:
    payload = json.dumps(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "git status"}}
    )
    result = runner.invoke(app, ["hooks", "run", "PreToolUse", "--db", "test.db"], input=payload)
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_hooks_run_pretooluse_denies_and_queues_unsafe_command(tmp_path: Path) -> None:
    payload = json.dumps(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
    )
    result = runner.invoke(app, ["hooks", "run", "PreToolUse", "--db", "test.db"], input=payload)
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    list_result = runner.invoke(app, ["approvals", "list", "--db", "test.db"])
    assert "pending" in list_result.output
    assert "rm -rf /tmp/x" not in list_result.output  # plan dict isn't dumped raw, just tool/id


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
