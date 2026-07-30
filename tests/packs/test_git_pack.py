"""PACK-002 Git (plan-v2 E20.2) exit criterion: at least one real mutation,
an injected partial failure, compensation, and verification against a
disposable real service -- not mocked.

Runs the real, officially-published Python `mcp-server-git` MCP server as
a stdio subprocess against a real, disposable local git repository (a
fresh `tmp_path`, real `git init`), loads `packs/git/contracts.yaml`
through the same `belay/contracts/loader.py` a real `belay wrap`/`belay
run` session uses, and drives it through the real `SagaExecutor` -- same
pattern as tests/packs/test_filesystem_pack.py and, before that,
tests/executor/test_crm_mock_acceptance.py.

Requires `mcp-server-git` importable/runnable (installed via `pip install
mcp-server-git`) and a real `git` binary on PATH -- skips cleanly if
either is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from belay.contracts.loader import load_contract_set
from belay.executor.saga import SagaExecutor, SagaStep
from belay.ledger.store import LedgerStore
from belay.proxy.upstream import connect_stdio

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_PATH = REPO_ROOT / "packs" / "git" / "contracts.yaml"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _require_git_server() -> None:
    if shutil.which("git") is None:
        pytest.skip("git not found on PATH")
    try:
        subprocess.run(
            [sys.executable, "-c", "import mcp_server_git"],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("mcp-server-git not installed (pip install mcp-server-git)")


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _connect(repo: Path):
    return connect_stdio(sys.executable, ["-m", "mcp_server_git", "-r", str(repo)])


@pytest.mark.slow
async def test_staging_saga_fails_at_step_3_auto_compensates_via_reset(tmp_path: Path) -> None:
    _require_git_server()
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.txt").write_text("file a\n", encoding="utf-8")
    (repo / "b.txt").write_text("file b\n", encoding="utf-8")

    contract_set = load_contract_set([str(CONTRACTS_PATH)])
    add_contract = contract_set.resolve("git_add")
    assert add_contract is not None
    assert add_contract.reversibility == "reversible"

    async with _connect(repo) as up:

        async def executor(tool: str, args: dict) -> dict:
            result = await up.call_tool(tool, args)
            if result.isError:
                raise RuntimeError(str(result.content))
            return {"text": result.content[0].text if result.content else ""}

        ledger = LedgerStore()
        saga = SagaExecutor(ledger=ledger)

        steps = [
            SagaStep("git_add", {"repo_path": str(repo), "files": ["a.txt"]}, add_contract),
            SagaStep("git_add", {"repo_path": str(repo), "files": ["b.txt"]}, add_contract),
            SagaStep(
                "git_add",
                {"repo_path": str(repo), "files": ["does-not-exist-at-all.txt"]},
                add_contract,
            ),
            SagaStep("git_add", {"repo_path": str(repo), "files": ["a.txt"]}, add_contract),
        ]

        report = await saga.run_saga("s_git_pack_demo", steps, executor, auto_compensate=True)

        assert report.failed is not None
        assert len(report.committed) == 2
        assert report.compensated == [2, 1]  # strict reverse order

        # Real, on-disk verification via a fresh git_status call -- not
        # just the saga's own bookkeeping.
        status = await up.call_tool("git_status", {"repo_path": str(repo)})
        status_text = status.content[0].text
        assert "nothing to commit" in status_text or "Untracked" in status_text
        assert "Changes to be committed" not in status_text


@pytest.mark.slow
async def test_pack_contracts_load_cleanly_and_cover_every_real_upstream_tool(
    tmp_path: Path,
) -> None:
    """Regression against silent drift: every tool the real server actually
    advertises must have a contract in this pack, and vice versa."""
    _require_git_server()
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    contract_set = load_contract_set([str(CONTRACTS_PATH)])

    async with _connect(repo) as up:
        tools = await up.list_tools()

    real_tool_names = {t.name for t in tools}
    pack_tool_names = set(contract_set.contracts)
    assert real_tool_names == pack_tool_names, (
        f"pack/upstream drift -- upstream only: {real_tool_names - pack_tool_names}, "
        f"pack only: {pack_tool_names - real_tool_names}"
    )


@pytest.mark.slow
async def test_git_commit_is_honestly_irreversible(tmp_path: Path) -> None:
    """This server exposes no way to undo a commit (no revert, no
    reset-to-specific-commit) -- the contract must say so, not pretend."""
    _require_git_server()
    contract_set = load_contract_set([str(CONTRACTS_PATH)])
    commit_contract = contract_set.resolve("git_commit")
    assert commit_contract is not None
    assert commit_contract.reversibility == "irreversible"
    assert commit_contract.undo is None
