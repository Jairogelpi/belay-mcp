"""`belay counterfactual` against a real SQLite fixture from a prior `belay run`
session (plan-v2 E12, mirrors the E3/E7 stdio-subprocess fixture pattern)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest
from belay.cli.main import app
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from typer.testing import CliRunner

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _wrap(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "belay.db"
    config_path = tmp_path / "belay.wrap.json"
    result = runner.invoke(
        app,
        [
            "wrap",
            str(REPO_ROOT / "examples" / "crm-mock"),
            "--contracts",
            str(REPO_ROOT / "examples" / "contracts" / "crm.yaml"),
            "--db",
            str(db_path),
            "--out",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    return config_path, db_path


def _pause_bulk_delete_policy(tmp_path: Path) -> Path:
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "belay_policy: '0.1'\ntools:\n  - match: 'crm.bulk_delete'\n    verdict: pause\n",
        encoding="utf-8",
    )
    return policy_path


@pytest.mark.slow
async def test_counterfactual_against_a_real_belay_run_session_leaves_ledger_untouched(
    tmp_path: Path,
) -> None:
    config_path, db_path = _wrap(tmp_path)
    policy_path = _pause_bulk_delete_policy(tmp_path)

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "belay.cli.main",
            "run",
            "--config",
            str(config_path),
            "--policy",
            str(policy_path),
        ],
        cwd=str(REPO_ROOT),
    )

    session_id: str | None = None

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("crm.create", {"id": "a", "fields": {"last_seen": 2020}})

        first = await session.call_tool("crm.bulk_delete", {"before_year": 2024})
        payload = json.loads(first.content[0].text)  # type: ignore[union-attr]
        assert payload["status"] == "pending_approval"
        approval_id = payload["approval_id"]

        approved = runner.invoke(
            app, ["approvals", "approve", approval_id, "--by", "jairo", "--db", str(db_path)]
        )
        assert approved.exit_code == 0, approved.stdout

        second = await session.call_tool("crm.bulk_delete", {"before_year": 2024})
        assert not second.isError

        from belay.ledger.store import LedgerStore

        all_events = LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}").read_all()
        session_id = all_events[0].session_id

    assert session_id is not None
    before = len(LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}").read_all())  # type: ignore[name-defined]

    fork_step = next(
        e.step_seq
        for e in LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}").read(session_id)  # type: ignore[name-defined]
        if e.type == "policy_evaluated" and e.step_seq is not None
    )

    args = [
        "counterfactual",
        session_id,
        "--at-step",
        str(fork_step),
        "--override",
        '{"verdict": "deny"}',
        "--db",
        str(db_path),
        "--json",
    ]
    result = await anyio.to_thread.run_sync(lambda: runner.invoke(app, args))
    assert result.exit_code == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["session_id"] == session_id
    assert not report["is_noop"]
    assert any(s["outcome"] == "diverged" for s in report["steps"])

    after = len(LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}").read_all())  # type: ignore[name-defined]
    assert before == after

    verify_result = runner.invoke(app, ["verify", str(db_path)])
    assert verify_result.exit_code == 0
    assert "chain: OK" in verify_result.stdout
