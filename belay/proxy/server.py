"""MCP-facing server surface presented to the agent (spec §3, Appendix C).

Belay is an MCP server toward the agent. `BelayProxyServer` lists the
upstream's tools verbatim and, on every `call_tool`, runs the request
through `belay.proxy.lifecycle.Lifecycle` (resolve -> plan -> policy ->
approval -> execute) before delegating the actual call to the upstream
client. Every call emits its ledger events; `contract_missing` and other
spec §11 errors surface as structured MCP tool errors rather than a raw
traceback, and a `pause` verdict (spec §7) surfaces as a structured,
non-error `pending_approval` result instead of either.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from belay.contracts.model import ContractSet
from belay.errors import BelayError
from belay.ledger.store import LedgerStore
from belay.policy.model import PolicyDoc, default_policy
from belay.proxy.lifecycle import Lifecycle
from belay.proxy.upstream import UpstreamClient


class BelayProxyServer:
    """The Belay MCP server: agent-facing, backed by one upstream + one contract set."""

    def __init__(
        self,
        upstream: UpstreamClient,
        contract_set: ContractSet,
        ledger: LedgerStore,
        session_id: str,
        unsafe_passthrough_tools: frozenset[str] = frozenset(),
        policy: PolicyDoc | None = None,
    ) -> None:
        self._upstream = upstream
        self.lifecycle = Lifecycle(
            contract_set=contract_set,
            unsafe_passthrough_tools=unsafe_passthrough_tools,
            ledger=ledger,
            session_id=session_id,
            policy=policy if policy is not None else default_policy(),
        )
        self._server: Server[Any, Any] = Server("belay")
        self._register_handlers()

    @property
    def mcp_server(self) -> Server[Any, Any]:
        return self._server

    def _register_handlers(self) -> None:
        @self._server.list_tools()  # type: ignore[untyped-decorator, no-untyped-call]
        async def _list_tools() -> list[Tool]:
            return await self._upstream.list_tools()

        @self._server.call_tool()  # type: ignore[untyped-decorator]
        async def _call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
            annotations = self._upstream.annotations_for(name)
            read_only_hint = bool(annotations and annotations.readOnlyHint)

            async def executor(tool: str, args: dict[str, Any]) -> CallToolResult:
                return await self._upstream.call_tool(tool, args)

            try:
                result = await self.lifecycle.govern_and_execute(
                    name, arguments, read_only_hint=read_only_hint, executor=executor
                )
            except BelayError as exc:
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(exc.to_dict()))],
                    isError=True,
                )
            if isinstance(result, CallToolResult):
                return result
            # A structured, non-error status payload (spec §7.3
            # `pending_approval`) -- not a raw error, not the upstream's
            # result shape either. `structuredContent` is set too so MCP
            # clients that validate against the tool's declared
            # `outputSchema` (an open object schema for every example tool)
            # still accept it.
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result))],
                structuredContent=result,
                isError=False,
            )

    async def run_stdio(self) -> None:
        """Serve over stdio (E3's minimum required transport, spec Appendix C)."""
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(
                read_stream, write_stream, self._server.create_initialization_options()
            )
