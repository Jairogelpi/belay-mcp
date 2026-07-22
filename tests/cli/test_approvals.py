"""`belay approvals list/approve/reject` end-to-end (spec §7; plan.md E5 (d)).

Exercises the full CLI-driven flow against a real `belay run` subprocess
over stdio: action paused -> `belay approvals list` shows it -> `approve`
-> the same session's retried call proceeds -> the ledger links everything
(`verify_chain`/`verify_coherence`, spec §9.2/E2).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
            str(REPO_ROOT / "examples" / "fs-server"),
            "--contracts",
            str(REPO_ROOT / "examples" / "contracts" / "fs.yaml"),
            "--db",
            str(db_path),
            "--out",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    return config_path, db_path


def _pause_write_policy(tmp_path: Path) -> Path:
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "belay_policy: '0.1'\ntools:\n  - match: 'fs.write_file'\n    verdict: pause\n",
        encoding="utf-8",
    )
    return policy_path


async def test_paused_action_lists_via_cli_then_approve_lets_it_proceed(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "hello.txt").write_text("hi", encoding="utf-8")

    config_path, db_path = _wrap(tmp_path)
    policy_path = _pause_write_policy(tmp_path)

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
        env={"BELAY_FS_ROOT": str(sandbox)},
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        # 1. The agent's write is paused (spec §7.3): structured, not an error.
        first = await session.call_tool(
            "fs.write_file", {"path": "hello.txt", "content": "updated"}
        )
        assert not first.isError
        payload = json.loads(first.content[0].text)  # type: ignore[union-attr]
        assert payload["status"] == "pending_approval"
        approval_id = payload["approval_id"]
        assert "poll_after_ms" in payload

        # File must be untouched -- nothing executed yet.
        assert (sandbox / "hello.txt").read_text(encoding="utf-8") == "hi"

        # 2. `belay approvals list` shows the real bound plan (spec §12).
        listed = runner.invoke(app, ["approvals", "list", "--db", str(db_path)])
        assert listed.exit_code == 0, listed.stdout
        assert approval_id in listed.stdout
        assert "fs.write_file" in listed.stdout

        # 3. `belay approvals approve <id>` transitions it (spec §7.1).
        approved = runner.invoke(
            app, ["approvals", "approve", approval_id, "--by", "jairo", "--db", str(db_path)]
        )
        assert approved.exit_code == 0, approved.stdout
        assert "approved" in approved.stdout

        # 4. The agent retries the identical call, within the same session ->
        # same plan_id -> now proceeds (spec §7, plan.md E5 (d)).
        second = await session.call_tool(
            "fs.write_file", {"path": "hello.txt", "content": "updated"}
        )
        assert not second.isError
        assert (sandbox / "hello.txt").read_text(encoding="utf-8") == "updated"

    # 5. The ledger links everything (spec §9.2 coherence + chain).
    verify_result = runner.invoke(app, ["verify", str(db_path)])
    assert verify_result.exit_code == 0, verify_result.stdout
    assert "chain: OK" in verify_result.stdout
    assert "coherence: OK" in verify_result.stdout


async def test_rejected_action_never_proceeds_and_reports_reason(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "hello.txt").write_text("hi", encoding="utf-8")

    config_path, db_path = _wrap(tmp_path)
    policy_path = _pause_write_policy(tmp_path)

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
        env={"BELAY_FS_ROOT": str(sandbox)},
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        first = await session.call_tool(
            "fs.write_file", {"path": "hello.txt", "content": "updated"}
        )
        approval_id = json.loads(first.content[0].text)["approval_id"]  # type: ignore[union-attr]

        rejected = runner.invoke(
            app,
            [
                "approvals",
                "reject",
                approval_id,
                "--by",
                "jairo",
                "--reason",
                "not now",
                "--db",
                str(db_path),
            ],
        )
        assert rejected.exit_code == 0, rejected.stdout

        second = await session.call_tool(
            "fs.write_file", {"path": "hello.txt", "content": "updated"}
        )
        assert second.isError
        body = json.loads(second.content[0].text)  # type: ignore[union-attr]
        assert body["code"] == "approval_rejected"
        assert body["detail"]["reason"] == "not now"

    assert (sandbox / "hello.txt").read_text(encoding="utf-8") == "hi"


def test_approvals_list_is_empty_message_on_a_fresh_db(tmp_path: Path) -> None:
    result = runner.invoke(app, ["approvals", "list", "--db", str(tmp_path / "belay.db")])
    assert result.exit_code == 0
    assert "no approval items" in result.stdout
