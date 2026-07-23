"""No-self-approval (spec Â§12): the agent has no approval-surface at all.

Belay MUST NOT expose any approval-related route to the protected agent.
This is enforced architecturally, not just by convention: `BelayProxyServer`
only ever registers `list_tools`/`call_tool` handlers that proxy the
*upstream's* tools (`belay/proxy/server.py`), and `ApprovalStage` (in
`belay/proxy/lifecycle.py`) has no `approve`/`reject` call sites -- those
live only in `belay/cli/main.py`'s `approvals` subcommands. So there is
no code path from an MCP `call_tool` to `ApprovalQueue.approve`/`.reject`.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from belay.contracts.model import ContractSet
from belay.ledger.store import LedgerStore
from belay.proxy import lifecycle as lifecycle_module
from belay.proxy.server import BelayProxyServer
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, Tool, ToolAnnotations

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeUpstream:
    """An upstream with no approval-shaped tools at all."""

    async def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="fs.list_files",
                inputSchema={"type": "object", "properties": {}},
                annotations=ToolAnnotations(readOnlyHint=True),
            )
        ]

    def annotations_for(self, tool: str) -> ToolAnnotations | None:
        return None

    async def call_tool(  # pragma: no cover
        self, tool: str, args: dict[str, Any]
    ) -> CallToolResult:
        raise AssertionError("upstream should never be asked to run an approval-shaped tool")


async def test_agent_facing_tool_list_never_advertises_an_approval_tool() -> None:
    """@spec("12.1") — approval surfaces MUST NOT be exposed as tools to the protected agent."""
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
        names = {t.name for t in result.tools}
        assert not any("approv" in n.lower() for n in names)


@pytest.mark.parametrize(
    "fake_tool_name",
    ["approvals.approve", "belay.approve", "approve", "approvals/approve"],
)
async def test_calling_any_approval_shaped_tool_name_is_refused_not_approved(
    fake_tool_name: str,
) -> None:
    """@spec("7.2.2") — an agent MUST NOT be able to approve its own actions through any tool."""
    upstream = FakeUpstream()
    proxy = BelayProxyServer(
        upstream,  # type: ignore[arg-type]
        ContractSet(contracts={}, set_hash="sha256:empty"),
        LedgerStore(),
        "s_test",
    )
    proxy.lifecycle.start_session("test-fixture")

    async with create_connected_server_and_client_session(proxy.mcp_server) as client:
        result = await client.call_tool(fake_tool_name, {"approval_id": "ap_whatever"})
        # There is no approval surface to call: an undeclared, non-read-only
        # tool is refused per the default rule (spec Â§4.6) -- it never
        # reaches anything that could resolve or approve anything.
        assert result.isError
        assert "contract_missing" in result.content[0].text  # type: ignore[union-attr]


def test_approval_stage_has_no_approve_or_reject_call_site() -> None:
    """Architectural check: `ApprovalStage` (the only approval-facing object
    reachable from the agent-facing lifecycle) never calls
    `ApprovalQueue.approve`/`.reject` -- those verbs are CLI-only."""
    source = inspect.getsource(lifecycle_module.ApprovalStage)
    assert ".approve(" not in source
    assert ".reject(" not in source

