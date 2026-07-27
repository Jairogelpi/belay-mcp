"""belay/cli/client_configs.py: Codex (TOML) and OpenCode (JSON) config rendering.

Pure-function tests only -- no real filesystem paths, no touching a real
home directory (a prior manual test of this accidentally wrote to the
developer's actual ~/.codex/config.toml; these tests never call anything
that resolves a real path).
"""

from __future__ import annotations

import json
import tomllib

from belay.cli.client_configs import render_codex_toml, render_opencode_json


def test_codex_toml_empty_file_produces_valid_table() -> None:
    text = render_codex_toml("", "belay", "python", ["-m", "belay.cli.main", "run"])
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["belay"]["command"] == "python"
    assert parsed["mcp_servers"]["belay"]["args"] == ["-m", "belay.cli.main", "run"]


def test_codex_toml_preserves_other_tables() -> None:
    existing = (
        'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "foo"\nargs = ["bar"]\n'
    )
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


def test_codex_toml_does_not_touch_unrelated_table_after_belay_section() -> None:
    existing = (
        "[mcp_servers.belay]\ncommand = \"old\"\nargs = [\"x\"]\n\n"
        "[projects.something]\ntrust_level = \"trusted\"\n"
    )
    text = render_codex_toml(existing, "belay", "new_python", ["-m", "y"])
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["belay"]["command"] == "new_python"
    assert parsed["projects"]["something"]["trust_level"] == "trusted"


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
