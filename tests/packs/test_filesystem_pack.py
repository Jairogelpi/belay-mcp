"""PACK-001 Filesystem (plan-v2 E20.1) exit criterion: at least one real
mutation, an injected partial failure, compensation, and verification
against a disposable real service -- not mocked.

Runs the real, officially-published `@modelcontextprotocol/server-filesystem`
MCP server as a stdio subprocess (`npx -y @modelcontextprotocol/server-
filesystem <sandbox>`), loads `packs/filesystem/contracts.yaml` through the
same `belay/contracts/loader.py` a real `belay wrap`/`belay run` session
uses, and drives it through the real `SagaExecutor` -- the same pattern
`tests/executor/test_crm_mock_acceptance.py` established for the CRM mock,
applied here to a real, officially-published third-party server instead of
one belay wrote for its own tests.

Requires `npx` on PATH (Node.js) -- skips cleanly if unavailable rather
than failing, matching this project's own established pattern of not
letting an environment gap masquerade as a code bug (compare
tests/hooks/test_live_conformance.py's PINNED_CLAUDE_VERSION skip).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from belay.contracts.loader import load_contract_set
from belay.executor.saga import SagaExecutor, SagaStep
from belay.ledger.store import LedgerStore
from belay.proxy.upstream import connect_stdio

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_PATH = REPO_ROOT / "packs" / "filesystem" / "contracts.yaml"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _require_npx() -> None:
    if shutil.which("npx") is None:
        pytest.skip("npx (Node.js) not found on PATH -- needed to run the real filesystem server")


def _connect(sandbox: Path):
    return connect_stdio("npx", ["-y", "@modelcontextprotocol/server-filesystem", str(sandbox)])


@pytest.mark.slow
async def test_write_edit_move_saga_fails_at_step_4_auto_compensates(tmp_path: Path) -> None:
    _require_npx()
    contract_set = load_contract_set([str(CONTRACTS_PATH)])

    async with _connect(tmp_path) as up:

        async def executor(tool: str, args: dict) -> dict:
            result = await up.call_tool(tool, args)
            if result.isError:
                raise RuntimeError(str(result.content))
            content = result.structuredContent or {}
            return dict(content)

        # Seed two real files with known original content.
        file_a = str(tmp_path / "a.txt")
        file_b = str(tmp_path / "b.txt")
        await executor("write_file", {"path": file_a, "content": "original a\n"})
        await executor("write_file", {"path": file_b, "content": "line1\nline2\nline3\n"})

        ledger = LedgerStore()
        saga = SagaExecutor(ledger=ledger)

        write_contract = contract_set.resolve("write_file")
        edit_contract = contract_set.resolve("edit_file")
        move_contract = contract_set.resolve("move_file")
        assert write_contract is not None
        assert edit_contract is not None
        assert move_contract is not None

        moved_path = str(tmp_path / "a-moved.txt")
        steps = [
            SagaStep("write_file", {"path": file_a, "content": "overwritten a"}, write_contract),
            SagaStep(
                "edit_file",
                {"path": file_b, "edits": [{"oldText": "line2", "newText": "EDITED"}]},
                edit_contract,
            ),
            SagaStep("move_file", {"source": file_a, "destination": moved_path}, move_contract),
            SagaStep(
                "write_file", {"path": "/nope/does-not-exist.txt", "content": "x"}, write_contract
            ),
            SagaStep("write_file", {"path": file_b, "content": "never reached"}, write_contract),
        ]

        async def flaky_executor(tool: str, args: dict) -> dict:
            if args.get("path") == "/nope/does-not-exist.txt":
                raise RuntimeError("simulated step-4 failure -- an unwritable path")
            return await executor(tool, args)

        report = await saga.run_saga("s_fs_pack_demo", steps, flaky_executor, auto_compensate=True)

        assert report.failed is not None
        assert len(report.committed) == 3
        assert report.compensated == [3, 2, 1]  # strict reverse order

        # Real, on-disk verification -- not just the saga's own bookkeeping.
        assert Path(file_a).read_text(encoding="utf-8") == "original a\n"
        assert Path(file_b).read_text(encoding="utf-8") == "line1\nline2\nline3\n"
        assert not Path(moved_path).exists()


@pytest.mark.slow
async def test_write_file_on_a_brand_new_file_is_not_reversible_but_still_succeeds(
    tmp_path: Path,
) -> None:
    """The conditional-reversibility edge case this pack's own contract
    documents: creating a file that didn't exist before must still be
    ALLOWED (the write itself succeeds) even though there is no upstream
    delete tool to undo it with."""
    _require_npx()
    contract_set = load_contract_set([str(CONTRACTS_PATH)])
    write_contract = contract_set.resolve("write_file")
    assert write_contract is not None
    assert write_contract.reversibility == "conditional"

    async with _connect(tmp_path) as up:

        async def executor(tool: str, args: dict):
            return await up.call_tool(tool, args)

        ledger = LedgerStore()
        saga = SagaExecutor(ledger=ledger)
        new_path = str(tmp_path / "brand-new.txt")

        # Must not raise -- a failed capture (the file doesn't exist yet,
        # so the read_file capture call itself errors) must never block
        # the actual write from happening.
        result = await saga.run_step(
            "s_fs_new_file",
            1,
            "write_file",
            {"path": new_path, "content": "hello"},
            write_contract,
            executor,
        )

        assert result.tool == "write_file"
        assert Path(new_path).read_text(encoding="utf-8") == "hello"


@pytest.mark.slow
async def test_pack_contracts_load_cleanly_and_cover_every_real_upstream_tool(
    tmp_path: Path,
) -> None:
    """Regression against silent drift: every tool the real server actually
    advertises must have a contract in this pack, and vice versa -- not
    verified by inspection alone, verified by asking the real server."""
    _require_npx()
    contract_set = load_contract_set([str(CONTRACTS_PATH)])

    async with _connect(tmp_path) as up:
        tools = await up.list_tools()

    real_tool_names = {t.name for t in tools}
    pack_tool_names = set(contract_set.contracts)
    assert real_tool_names == pack_tool_names, (
        f"pack/upstream drift -- upstream only: {real_tool_names - pack_tool_names}, "
        f"pack only: {pack_tool_names - real_tool_names}"
    )
