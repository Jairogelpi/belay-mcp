"""End-to-end integration: a real MCP SDK client talks to Belay, which talks
to `examples/fs-server`, entirely over stdio (plan.md E3 required test).

Two real stdio hops: `stdio_client` -> `belay run` subprocess (agent-facing
MCP server) -> its own `connect_stdio` -> `examples/fs-server` subprocess
(upstream). This is the slowest test in the suite (two Python subprocess
starts); see docs/adr/0003-e3-proxy-l1.md for the tradeoff note.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_real_client_belay_fs_server_over_stdio(tmp_path: Path) -> None:
    from belay.contracts.loader import load_contract_set
    from belay.proxy.config import UpstreamCommand, WrapConfig

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "hello.txt").write_text("hi", encoding="utf-8")

    contracts_path = REPO_ROOT / "examples" / "contracts" / "fs.yaml"
    load_contract_set([str(contracts_path)])  # fail fast if the fixture contract is broken

    db_path = tmp_path / "belay.db"
    config = WrapConfig(
        upstream=UpstreamCommand(
            command=sys.executable,
            args=[str(REPO_ROOT / "examples" / "fs-server" / "server.py")],
        ),
        contracts=[str(contracts_path)],
        unsafe_passthrough=[],
        db=str(db_path),
    )
    config_path = tmp_path / "belay.wrap.json"
    config.save(config_path)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "belay.cli.main", "run", "--config", str(config_path)],
        cwd=str(REPO_ROOT),
        env={"BELAY_FS_ROOT": str(sandbox)},
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert {"fs.list_files", "fs.read_file", "fs.write_file", "fs.delete_file"} <= names

        listed = await session.call_tool("fs.list_files", {})
        assert not listed.isError
        assert "hello.txt" in listed.structuredContent["result"]

        read = await session.call_tool("fs.read_file", {"path": "hello.txt"})
        assert not read.isError
        assert read.structuredContent["result"] == "hi"

        # fs.write_file has a contract in examples/contracts/fs.yaml -> allowed.
        written = await session.call_tool(
            "fs.write_file", {"path": "hello.txt", "content": "updated"}
        )
        assert not written.isError

        reread = await session.call_tool("fs.read_file", {"path": "hello.txt"})
        assert reread.structuredContent["result"] == "updated"

    events_ok = load_contract_set([str(contracts_path)])
    assert events_ok.set_hash  # sanity: the contract set used still loads post-hoc


async def test_undeclared_destructive_tool_is_refused_over_real_stdio(tmp_path: Path) -> None:
    """A tool with `destructiveHint` and no contract stays refused end-to-end."""
    import yaml
    from belay.proxy.config import UpstreamCommand, WrapConfig

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "victim.txt").write_text("keep me", encoding="utf-8")

    # A contract set that deliberately omits fs.delete_file.
    minimal_contracts = tmp_path / "fs-minimal.yaml"
    minimal_contracts.write_text(
        yaml.safe_dump(
            {
                "belay_contract": "0.1",
                "tool": "fs.read_file",
                "reversibility": "irreversible",
                "effects": [{"type": "read", "resource": "fs.file"}],
            }
        ),
        encoding="utf-8",
    )

    config = WrapConfig(
        upstream=UpstreamCommand(
            command=sys.executable,
            args=[str(REPO_ROOT / "examples" / "fs-server" / "server.py")],
        ),
        contracts=[str(minimal_contracts)],
        unsafe_passthrough=[],
        db=str(tmp_path / "belay.db"),
    )
    config_path = tmp_path / "belay.wrap.json"
    config.save(config_path)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "belay.cli.main", "run", "--config", str(config_path)],
        cwd=str(REPO_ROOT),
        env={"BELAY_FS_ROOT": str(sandbox)},
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("fs.delete_file", {"path": "victim.txt"})
        assert result.isError
        assert "contract_missing" in result.content[0].text

    assert (sandbox / "victim.txt").exists()
