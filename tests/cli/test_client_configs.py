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
