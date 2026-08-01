"""R1.6: `packs/claude-code-native/contracts.yaml` is the pack `belay hooks
install --contracts <file>` actually needs -- keyed by Claude Code's own
native tool names (`Write`/`Edit`/`NotebookEdit`), not a downstream MCP
server's tool names like `packs/filesystem/contracts.yaml`. Loads the real
pack through the same `belay/contracts/loader.py` a real install uses and
asserts every tool the Native Agent Gate can resolve a file edit against
(`belay/hooks/gate.py::evaluate_file_edit`) actually resolves here.
"""

from __future__ import annotations

from pathlib import Path

from belay.contracts.loader import load_contract_set

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_PATH = REPO_ROOT / "packs" / "claude-code-native" / "contracts.yaml"


def test_all_three_native_file_edit_tools_resolve() -> None:
    contract_set = load_contract_set([str(CONTRACTS_PATH)])
    for tool_name in ("Write", "Edit", "NotebookEdit"):
        contract = contract_set.resolve(tool_name)
        assert contract is not None, f"{tool_name} must resolve in the native pack"
        assert contract.tool == tool_name


def test_downstream_mcp_server_tool_names_do_not_resolve() -> None:
    """Guards against accidentally re-declaring this pack with proxy-style
    tool names (the exact mistake the README used to make with
    packs/filesystem/contracts.yaml)."""
    contract_set = load_contract_set([str(CONTRACTS_PATH)])
    for proxy_tool_name in ("write_file", "edit_file", "read_file"):
        assert contract_set.resolve(proxy_tool_name) is None
