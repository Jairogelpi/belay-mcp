"""MCP client toward the real tool servers (spec §3, Appendix C).

Belay is an MCP client to the wrapped tool server: it launches it as a
stdio subprocess (E3 minimum transport; HTTP streamable is a documented
follow-up, see docs/adr/0003-e3-proxy-l1.md) and speaks MCP to it via the
official SDK's `ClientSession`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, Tool, ToolAnnotations


class UpstreamClient:
    """A live MCP client session to one wrapped tool server."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session
        self._tools_by_name: dict[str, Tool] = {}

    async def list_tools(self) -> list[Tool]:
        """List (and cache) the upstream's tools, for `list_tools` passthrough."""
        result = await self._session.list_tools()
        self._tools_by_name = {tool.name: tool for tool in result.tools}
        return result.tools

    def annotations_for(self, tool: str) -> ToolAnnotations | None:
        """MCP annotations of a previously-listed tool (spec §4.6, Appendix C)."""
        cached = self._tools_by_name.get(tool)
        return cached.annotations if cached is not None else None

    async def call_tool(self, tool: str, args: dict[str, Any]) -> CallToolResult:
        return await self._session.call_tool(tool, args)


@asynccontextmanager
async def connect_stdio(
    command: str, args: list[str], env: dict[str, str] | None = None
) -> AsyncIterator[UpstreamClient]:
    """Launch `command args...` as a stdio MCP server and connect a client to it.

    `env` is merged over the SDK's safe-subset default environment (it does
    not, by itself, inherit the full parent environment) — Belay passes its
    own `os.environ` through here so the upstream subprocess sees the same
    configuration (e.g. `examples/fs-server`'s `BELAY_FS_ROOT`) as `belay run`.
    """
    params = StdioServerParameters(command=command, args=args, env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        client = UpstreamClient(session)
        await client.list_tools()
        yield client
