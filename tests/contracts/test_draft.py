"""belay/contracts/draft.py: heuristic contract drafting from MCP tool schemas."""

from __future__ import annotations

from belay.contracts.draft import draft_contract, draft_contracts
from belay.contracts.loader import load_contract_set
from mcp.types import Tool, ToolAnnotations


def _tool(name: str, *, read_only: bool = False, destructive: bool = False) -> Tool:
    return Tool(
        name=name,
        inputSchema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        annotations=ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive),
    )


def test_read_only_tool_drafted_as_reversible_noop() -> None:
    tool = _tool("read_file", read_only=True)
    result = draft_contract(tool, {"read_file"})
    assert result.document["reversibility"] == "reversible"
    assert result.document["undo"]["tool"] == "read_file"


def test_write_tool_paired_with_read_counterpart() -> None:
    write_tool = _tool("write_file")
    result = draft_contract(write_tool, {"write_file", "read_file"})
    assert result.document["reversibility"] == "reversible"
    assert result.document["capture"]["tool"] == "read_file"
    assert "paired with read tool" in result.note


def test_delete_tool_paired_with_read_counterpart_is_conditional() -> None:
    delete_tool = _tool("delete_file", destructive=True)
    result = draft_contract(delete_tool, {"delete_file", "read_file"})
    assert result.document["reversibility"] == "conditional"
    assert result.document["conditions"]


def test_unpaired_write_tool_defaults_to_irreversible() -> None:
    tool = _tool("edit_file")
    result = draft_contract(tool, {"edit_file"})
    assert result.document["reversibility"] == "irreversible"


def test_all_drafted_contracts_are_unverified() -> None:
    tools = [_tool("read_file", read_only=True), _tool("edit_file")]
    results = draft_contracts(tools)
    for r in results:
        assert r.document["provenance"]["verified"] is False


def test_drafted_contracts_validate_against_real_schema(tmp_path) -> None:
    """The draft output must load through the same strict loader real contracts do."""
    import yaml

    tools = [_tool("read_file", read_only=True), _tool("write_file"), _tool("edit_file")]
    results = draft_contracts(tools)
    path = tmp_path / "draft.yaml"
    path.write_text(
        yaml.safe_dump_all([r.document for r in results], sort_keys=False), encoding="utf-8"
    )
    contract_set = load_contract_set([str(path)])
    assert len(contract_set.contracts) == 3
