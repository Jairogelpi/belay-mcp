"""belay/cli/client_configs.py: Codex (TOML) and OpenCode (JSON) config rendering,
plus atomic-write-with-backup.

Pure-function tests for rendering (existing text in, new text out) and
tmp_path-scoped tests for atomic_write_with_backup -- nothing here ever
resolves a real home directory path. (A prior manual verification of the
regex-based predecessor of this module accidentally wrote to the
developer's actual ~/.codex/config.toml; these tests are structured so
that mistake is impossible here.)

Covers the two real bugs review found in the regex-based predecessor:
a comment on the [mcp_servers.<name>] heading line, and an indented table
immediately following it, both corrupted or dropped unrelated content.
tomlkit-based rendering fixes both.
"""

from __future__ import annotations

import json
import tomllib

import pytest
from belay.cli.client_configs import (
    atomic_write_with_backup,
    render_codex_toml,
    render_opencode_json,
)


def test_codex_toml_empty_file_produces_valid_table() -> None:
    text = render_codex_toml("", "belay", "python", ["-m", "belay.cli.main", "run"])
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["belay"]["command"] == "python"
    assert parsed["mcp_servers"]["belay"]["args"] == ["-m", "belay.cli.main", "run"]


def test_codex_toml_preserves_other_tables() -> None:
    existing = 'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "foo"\nargs = ["bar"]\n'
    text = render_codex_toml(existing, "belay", "python", ["-m", "x"])
    parsed = tomllib.loads(text)
    assert parsed["model"] == "gpt-5"
    assert parsed["mcp_servers"]["other"]["command"] == "foo"
    assert parsed["mcp_servers"]["belay"]["command"] == "python"


def test_codex_toml_reapply_replaces_not_duplicates() -> None:
    once = render_codex_toml("", "belay", "python", ["-m", "old"])
    twice = render_codex_toml(once, "belay", "python", ["-m", "new"])
    assert twice.count("[mcp_servers.belay]") == 1
    parsed = tomllib.loads(twice)
    assert parsed["mcp_servers"]["belay"]["args"] == ["-m", "new"]


def test_codex_toml_comment_on_heading_line_does_not_corrupt() -> None:
    """The real bug: a comment on the [mcp_servers.<name>] line itself broke the
    old regex-based merge (produced invalid TOML / a duplicate table)."""
    existing = (
        '[mcp_servers.belay] # managed by belay, do not edit\ncommand = "old"\nargs = ["x"]\n'
    )
    text = render_codex_toml(existing, "belay", "new", ["-m", "y"])
    parsed = tomllib.loads(text)  # must still be valid TOML
    assert parsed["mcp_servers"]["belay"]["command"] == "new"


def test_codex_toml_indented_next_table_is_preserved() -> None:
    """The real bug: an indented table immediately after [mcp_servers.<name>]
    could be deleted entirely by the old regex-based merge."""
    existing = (
        '[mcp_servers.belay]\ncommand = "old"\nargs = ["x"]\n\n'
        '  [mcp_servers.other]\n  command = "foo"\n'
    )
    text = render_codex_toml(existing, "belay", "new", ["-m", "y"])
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["other"]["command"] == "foo"
    assert parsed["mcp_servers"]["belay"]["command"] == "new"


def test_codex_toml_handles_crlf_line_endings() -> None:
    existing = '[mcp_servers.other]\r\ncommand = "foo"\r\n'
    text = render_codex_toml(existing, "belay", "python", ["-m", "y"])
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["other"]["command"] == "foo"
    assert parsed["mcp_servers"]["belay"]["command"] == "python"


def test_codex_toml_handles_leading_bom() -> None:
    existing = '﻿[mcp_servers.other]\ncommand = "foo"\n'
    text = render_codex_toml(existing, "belay", "python", ["-m", "y"])
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["other"]["command"] == "foo"


def test_codex_toml_handles_quoted_table_names() -> None:
    existing = '[mcp_servers."my-server"]\ncommand = "foo"\n'
    text = render_codex_toml(existing, "belay", "python", ["-m", "y"])
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["my-server"]["command"] == "foo"
    assert parsed["mcp_servers"]["belay"]["command"] == "python"


def test_codex_toml_invalid_config_aborts_without_modifying() -> None:
    """Invalid TOML must fail loudly, not silently produce something else."""
    with pytest.raises(Exception, match=r"."):
        render_codex_toml("not valid [[[ toml {{{", "belay", "python", ["-m", "y"])


def test_opencode_json_empty_file() -> None:
    text = render_opencode_json("", "belay", ["python", "-m", "belay.cli.main", "run"])
    doc = json.loads(text)
    assert doc["mcp"]["belay"]["type"] == "local"
    assert doc["mcp"]["belay"]["command"] == ["python", "-m", "belay.cli.main", "run"]


def test_opencode_json_preserves_other_servers() -> None:
    existing = json.dumps({"mcp": {"other": {"type": "local", "command": ["x"]}}})
    text = render_opencode_json(existing, "belay", ["python", "-m", "y"])
    doc = json.loads(text)
    assert doc["mcp"]["other"]["command"] == ["x"]
    assert doc["mcp"]["belay"]["command"] == ["python", "-m", "y"]


def test_atomic_write_creates_backup_of_existing_content(tmp_path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("original\n", encoding="utf-8")
    backup = atomic_write_with_backup(target, "new\n")
    assert backup is not None
    assert backup.read_text(encoding="utf-8") == "original\n"
    assert target.read_text(encoding="utf-8") == "new\n"


def test_atomic_write_no_backup_when_target_did_not_exist(tmp_path) -> None:
    target = tmp_path / "new-config.toml"
    backup = atomic_write_with_backup(target, "content\n")
    assert backup is None
    assert target.read_text(encoding="utf-8") == "content\n"


def test_atomic_write_leaves_original_intact_on_write_failure(tmp_path, monkeypatch) -> None:
    target = tmp_path / "config.toml"
    target.write_text("original\n", encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("simulated disk failure")

    import os as os_module

    monkeypatch.setattr(os_module, "replace", _boom)
    with pytest.raises(OSError, match="simulated disk failure"):
        atomic_write_with_backup(target, "new\n")
    assert target.read_text(encoding="utf-8") == "original\n"
    leftover_temps = list(tmp_path.glob(".config.toml.*"))
    assert leftover_temps == []


def test_render_claude_hooks_settings_empty_file() -> None:
    from belay.cli.client_configs import render_claude_hooks_settings

    text = render_claude_hooks_settings("", "belay hooks run PreToolUse")
    doc = json.loads(text)
    entry = doc["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    assert entry["hooks"][0]["command"] == "belay hooks run PreToolUse"


def test_render_claude_hooks_settings_preserves_other_hooks_and_keys() -> None:
    from belay.cli.client_configs import render_claude_hooks_settings

    existing = json.dumps(
        {
            "someOtherSetting": True,
            "hooks": {
                "PreToolUse": [{"matcher": "Write", "hooks": [{"command": "my-own-hook.sh"}]}],
                "PostToolUse": [{"matcher": "*", "hooks": [{"command": "other.sh"}]}],
            },
        }
    )
    text = render_claude_hooks_settings(existing, "belay hooks run PreToolUse")
    doc = json.loads(text)
    assert doc["someOtherSetting"] is True
    assert doc["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "other.sh"
    pre = doc["hooks"]["PreToolUse"]
    assert any(e["hooks"][0]["command"] == "my-own-hook.sh" for e in pre)
    assert any(e["hooks"][0]["command"] == "belay hooks run PreToolUse" for e in pre)
    assert len(pre) == 2


def test_render_claude_hooks_settings_reinstall_replaces_not_duplicates() -> None:
    from belay.cli.client_configs import render_claude_hooks_settings

    once = render_claude_hooks_settings("", "belay hooks run PreToolUse --db old.db")
    twice = render_claude_hooks_settings(once, "belay hooks run PreToolUse --db new.db")
    doc = json.loads(twice)
    pre = doc["hooks"]["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["hooks"][0]["command"] == "belay hooks run PreToolUse --db new.db"


def test_remove_claude_hooks_entry_removes_only_belays() -> None:
    from belay.cli.client_configs import remove_claude_hooks_entry, render_claude_hooks_settings

    existing = json.dumps(
        {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [{"command": "mine.sh"}]}]}}
    )
    with_belay = render_claude_hooks_settings(existing, "belay hooks run PreToolUse")
    removed = remove_claude_hooks_entry(with_belay)
    doc = json.loads(removed)
    pre = doc["hooks"]["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["hooks"][0]["command"] == "mine.sh"


def test_remove_claude_hooks_entry_on_file_with_no_hooks_key_is_a_noop() -> None:
    from belay.cli.client_configs import remove_claude_hooks_entry

    existing = json.dumps({"foo": "bar"})
    result = remove_claude_hooks_entry(existing)
    assert json.loads(result) == {"foo": "bar"}


def test_claude_hooks_entry_present() -> None:
    from belay.cli.client_configs import claude_hooks_entry_present, render_claude_hooks_settings

    assert claude_hooks_entry_present("") is False
    assert claude_hooks_entry_present(json.dumps({"hooks": {}})) is False
    with_belay = render_claude_hooks_settings("", "belay hooks run PreToolUse")
    assert claude_hooks_entry_present(with_belay) is True


def test_atomic_restore_is_byte_exact(tmp_path) -> None:
    from belay.cli.client_configs import atomic_restore

    target = tmp_path / "config.json"
    backup = tmp_path / "config.json.belay-backup"
    original_bytes = b'{"a": 1}\r\n\xef\xbb\xbf'  # CRLF + trailing BOM bytes, on purpose
    backup.write_bytes(original_bytes)
    target.write_bytes(b'{"a": 1, "belay": {}}\n')

    atomic_restore(target, backup)

    assert target.read_bytes() == original_bytes


def test_atomic_restore_leaves_target_intact_on_failure(tmp_path, monkeypatch) -> None:
    from belay.cli.client_configs import atomic_restore

    target = tmp_path / "config.json"
    backup = tmp_path / "config.json.belay-backup"
    target.write_bytes(b"current\n")
    backup.write_bytes(b"backup\n")

    def _boom(*a, **k):
        raise OSError("simulated disk failure")

    import os as os_module

    monkeypatch.setattr(os_module, "replace", _boom)
    with pytest.raises(OSError, match="simulated disk failure"):
        atomic_restore(target, backup)
    assert target.read_bytes() == b"current\n"
    leftover_temps = list(tmp_path.glob(".config.json.*"))
    assert leftover_temps == []


def test_entry_present_json_true_and_false() -> None:
    from belay.cli.client_configs import entry_present

    existing = json.dumps({"mcpServers": {"belay": {"command": "python"}}})
    assert entry_present("claude-code", existing, "belay") is True
    assert entry_present("claude-code", existing, "other") is False
    assert entry_present("claude-code", "", "belay") is False


def test_entry_present_codex_toml() -> None:
    from belay.cli.client_configs import entry_present

    existing = '[mcp_servers.belay]\ncommand = "python"\n'
    assert entry_present("codex", existing, "belay") is True
    assert entry_present("codex", existing, "other") is False


def test_entry_present_opencode() -> None:
    from belay.cli.client_configs import entry_present

    existing = json.dumps({"mcp": {"belay": {"type": "local"}}})
    assert entry_present("opencode", existing, "belay") is True
    assert entry_present("opencode", existing, "other") is False


def test_manifest_round_trips(tmp_path) -> None:
    from belay.cli.client_configs import Manifest, load_manifest, sha256_of, write_manifest

    target = tmp_path / "config.json"
    target.write_text("after content", encoding="utf-8")
    before_hash = sha256_of("before content")
    path = write_manifest("claude-code", target, "belay", before_hash, "after content", None)
    assert path.is_file()
    loaded = load_manifest(target)
    assert isinstance(loaded, Manifest)
    assert loaded.client == "claude-code"
    assert loaded.name == "belay"
    assert loaded.before_hash == before_hash
    assert loaded.backup_path is None


def test_load_manifest_returns_none_when_absent(tmp_path) -> None:
    from belay.cli.client_configs import load_manifest

    assert load_manifest(tmp_path / "nonexistent.json") is None


def test_remove_codex_entry_removes_only_named_table() -> None:
    from belay.cli.client_configs import remove_codex_entry

    existing = (
        '[mcp_servers.belay]\ncommand = "python"\nargs = ["x"]\n\n'
        '[mcp_servers.other]\ncommand = "foo"\n'
    )
    result = remove_codex_entry(existing, "belay")
    parsed = tomllib.loads(result)
    assert "belay" not in parsed.get("mcp_servers", {})
    assert parsed["mcp_servers"]["other"]["command"] == "foo"


def test_remove_json_mcp_entry_removes_only_named_key() -> None:
    from belay.cli.client_configs import remove_json_mcp_entry

    existing = json.dumps(
        {"mcpServers": {"belay": {"command": "python"}, "other": {"command": "foo"}}}
    )
    result = remove_json_mcp_entry(existing, "belay")
    doc = json.loads(result)
    assert "belay" not in doc["mcpServers"]
    assert doc["mcpServers"]["other"]["command"] == "foo"


def test_remove_json_mcp_entry_supports_opencode_key() -> None:
    from belay.cli.client_configs import remove_json_mcp_entry

    existing = json.dumps({"mcp": {"belay": {"type": "local"}, "other": {"type": "local"}}})
    result = remove_json_mcp_entry(existing, "belay", key="mcp")
    doc = json.loads(result)
    assert "belay" not in doc["mcp"]
    assert "other" in doc["mcp"]
