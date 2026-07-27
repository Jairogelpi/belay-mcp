"""belay/cli/agent_instructions.py: idempotent AGENTS.md/CLAUDE.md standing-instruction block."""

from __future__ import annotations

from belay.cli.agent_instructions import upsert


def test_upsert_appends_to_existing_content() -> None:
    result = upsert("# My Project\n\nSome existing notes.")
    assert "# My Project" in result
    assert "Some existing notes." in result
    assert "belay:standing-instruction:start" in result


def test_upsert_on_empty_file_still_produces_block() -> None:
    result = upsert("")
    assert "belay:standing-instruction:start" in result


def test_upsert_is_idempotent_no_duplicate_block() -> None:
    once = upsert("# Project")
    twice = upsert(once)
    assert twice.count("belay:standing-instruction:start") == 1


def test_upsert_uses_custom_server_name() -> None:
    result = upsert("", server_name="my-belay")
    assert "`my-belay` MCP server" in result
    assert "`my-belay` MCP tools" in result
