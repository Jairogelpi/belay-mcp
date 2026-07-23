"""Fast in-memory tests for `BelayProxyServer` (agent-facing MCP surface).

Uses the MCP SDK's in-memory transport
(`mcp.shared.memory.create_connected_server_and_client_session`) to talk a
real `ClientSession` to a real `BelayProxyServer.mcp_server`, backed by a
fake `UpstreamClient` -- this covers the agent-facing half of the proxy
without spawning subprocesses (the subprocess-based, full two-hop stdio
path is covered by `tests/proxy/test_stdio_integration.py`).
"""

from __future__ import annotations

from typing import Any

import pytest
from belay.contracts.model import ContractSet
from belay.ledger.store import LedgerStore
from belay.proxy.server import BelayProxyServer
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeUpstream:
    """A stand-in `UpstreamClient` with two tools, one read-only, one not."""

    def __init__(self) -> None:
        self._tools = {
            "fs.list_files": Tool(
                name="fs.list_files",
                inputSchema={"type": "object", "properties": {}},
                annotations=ToolAnnotations(readOnlyHint=True),
            ),
            "fs.write_file": Tool(
                name="fs.write_file",
                inputSchema={"type": "object", "properties": {}},
                annotations=None,
            ),
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def annotations_for(self, tool: str) -> ToolAnnotations | None:
        cached = self._tools.get(tool)
        return cached.annotations if cached is not None else None

    async def call_tool(self, tool: str, args: dict[str, Any]) -> CallToolResult:
        self.calls.append((tool, args))
        return CallToolResult(
            content=[TextContent(type="text", text="ok")], structuredContent=None, isError=False
        )


async def test_list_tools_proxies_upstream_tools() -> None:
    upstream = FakeUpstream()
    proxy = BelayProxyServer(
        upstream,  # type: ignore[arg-type]
        ContractSet(contracts={}, set_hash="sha256:empty"),
        LedgerStore(),
        "s_test",
    )
    proxy.lifecycle.start_session("test-fixture")

    async with create_connected_server_and_client_session(proxy.mcp_server) as client:
        result = await client.list_tools()
        assert {t.name for t in result.tools} == {"fs.list_files", "fs.write_file"}


async def test_call_tool_read_only_hint_passes_through_with_no_contract() -> None:
    upstream = FakeUpstream()
    proxy = BelayProxyServer(
        upstream,  # type: ignore[arg-type]
        ContractSet(contracts={}, set_hash="sha256:empty"),
        LedgerStore(),
        "s_test",
    )
    proxy.lifecycle.start_session("test-fixture")

    async with create_connected_server_and_client_session(proxy.mcp_server) as client:
        result = await client.call_tool("fs.list_files", {})
        assert not result.isError
        assert upstream.calls == [("fs.list_files", {})]


async def test_call_tool_without_contract_or_hint_is_refused_as_mcp_error() -> None:
    upstream = FakeUpstream()
    proxy = BelayProxyServer(
        upstream,  # type: ignore[arg-type]
        ContractSet(contracts={}, set_hash="sha256:empty"),
        LedgerStore(),
        "s_test",
    )
    proxy.lifecycle.start_session("test-fixture")

    async with create_connected_server_and_client_session(proxy.mcp_server) as client:
        result = await client.call_tool("fs.write_file", {"path": "a", "content": "b"})
        assert result.isError
        assert "contract_missing" in result.content[0].text  # type: ignore[union-attr]
        assert upstream.calls == []

