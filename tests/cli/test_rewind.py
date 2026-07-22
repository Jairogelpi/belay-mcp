"""`belay rewind` end-to-end (spec §10; plan.md E7 (d), the plan.md §10 demo).

Exercises the full CLI-driven flow against a real `belay run` subprocess and
`examples/crm-mock`: an agent's `crm.bulk_delete` is paused (blast radius),
a human approves it via `belay approvals approve`, a second ("oops") bulk
delete happens the same way, then `belay rewind --dry-run` shows the honest
plan without touching anything, and `belay rewind --by <name>` compensates
both steps in reverse order and reports the session fully rewound.
"""

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


def _payload(call_result: object) -> dict:
    """A real (non-error, non-pending) tool result's payload dict.

    Mirrors `tests/executor/test_crm_mock_acceptance.py`'s defensive
    unwrapping: FastMCP may or may not nest a dict-returning tool's output
    under a `"result"` key in `structuredContent`.
    """
    content = getattr(call_result, "structuredContent", None) or {}
    return dict(content.get("result", content))


def _pause_bulk_delete_policy(tmp_path: Path) -> Path:
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "belay_policy: '0.1'\ntools:\n  - match: 'crm.bulk_delete'\n    verdict: pause\n",
        encoding="utf-8",
    )
    return policy_path


@pytest.mark.slow
async def test_demo_scenario_pause_approve_oops_dry_run_and_rewind(tmp_path: Path) -> None:
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
        await session.call_tool("crm.create", {"id": "b", "fields": {"last_seen": 2020}})
        await session.call_tool("crm.create", {"id": "c", "fields": {"last_seen": 2025}})

        # 1. Agent asks to clean up stale records -> paused (blast radius).
        first = await session.call_tool("crm.bulk_delete", {"before_year": 2024})
        payload = json.loads(first.content[0].text)  # type: ignore[union-attr]
        assert payload["status"] == "pending_approval"
        approval_id = payload["approval_id"]

        # 2. `belay approvals list` shows the real bound plan; human approves it.
        listed = runner.invoke(app, ["approvals", "list", "--db", str(db_path)])
        assert approval_id in listed.stdout

        approved = runner.invoke(
            app, ["approvals", "approve", approval_id, "--by", "jairo", "--db", str(db_path)]
        )
        assert approved.exit_code == 0, approved.stdout

        # 3. The agent retries -> now committed: a, b deleted (last_seen < 2024).
        # (Deliberately not spending an extra governed `crm.export_records`
        # call here to "check" the result -- every governed call becomes its
        # own committed step, and `crm.export_records` is contractually
        # irreversible, which would pollute this session's rewind scope with
        # an irreversible step unrelated to the demo. `deleted_ids` from the
        # bulk_delete result itself is evidence enough.)
        second = await session.call_tool("crm.bulk_delete", {"before_year": 2024})
        assert not second.isError
        result_payload = _payload(second)
        assert sorted(result_payload["deleted_ids"]) == ["a", "b"]

        # 4. "--oops": another bulk delete, wrong cutoff, wipes everything left.
        oops_pending = await session.call_tool("crm.bulk_delete", {"before_year": 2030})
        oops_payload = json.loads(oops_pending.content[0].text)  # type: ignore[union-attr]
        oops_approval_id = oops_payload["approval_id"]
        runner.invoke(
            app,
            ["approvals", "approve", oops_approval_id, "--by", "jairo", "--db", str(db_path)],
        )
        oops = await session.call_tool("crm.bulk_delete", {"before_year": 2030})
        assert not oops.isError
        oops_result = _payload(oops)
        assert oops_result["deleted_ids"] == ["c"]

        # Recover the session id belay assigned (fixed per `belay run` process).
        # `belay verify` reads the whole ledger; find the session from the DB directly.
        from belay.ledger.store import LedgerStore

        all_events = LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}").read_all()
        session_id = all_events[0].session_id
        for ev in all_events:
            assert ev.session_id == session_id  # one `belay run` process => one session

    assert session_id is not None

    # 5. `belay rewind --dry-run`: honest plan, nothing executed.
    # `rewind`'s implementation calls `anyio.run()` itself, so it must run off
    # this test's own event loop thread (CliRunner.invoke is otherwise sync).
    dry_args = ["rewind", session_id, "--dry-run", "--config", str(config_path)]
    dry = await anyio.to_thread.run_sync(lambda: runner.invoke(app, dry_args))
    assert dry.exit_code == 0, dry.stdout
    assert "reversible" in dry.stdout
    assert "0 irreversible" in dry.stdout or "irreversible/indeterminate" in dry.stdout

    # `belay verify` after the dry run must still be clean -- nothing appended.
    verify_after_dry = runner.invoke(app, ["verify", str(db_path)])
    assert verify_after_dry.exit_code == 0

    # 6. `belay rewind --by jairo`: real rewind, both bulk_delete steps undone
    # in reverse order -- final state must be the original 3-record snapshot.
    real_args = ["rewind", session_id, "--by", "jairo", "--config", str(config_path)]
    real = await anyio.to_thread.run_sync(lambda: runner.invoke(app, real_args))
    assert real.exit_code == 0, real.stdout
    assert "fully compensated" in real.stdout

    verify_result = runner.invoke(app, ["verify", str(db_path)])
    assert verify_result.exit_code == 0, verify_result.stdout
    assert "chain: OK" in verify_result.stdout
    assert "coherence: OK" in verify_result.stdout


@pytest.mark.slow
async def test_rewind_fencing_blocks_the_governed_session_from_new_steps(
    tmp_path: Path,
) -> None:
    config_path, db_path = _wrap(tmp_path)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "belay.cli.main", "run", "--config", str(config_path)],
        cwd=str(REPO_ROOT),
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("crm.create", {"id": "x", "fields": {"last_seen": 2020}})

        from belay.ledger.store import LedgerStore

        events = LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}").read_all()
        session_id = events[0].session_id

        rewound = await anyio.to_thread.run_sync(
            lambda: runner.invoke(
                app, ["rewind", session_id, "--by", "jairo", "--config", str(config_path)]
            )
        )
        assert rewound.exit_code == 0, rewound.stdout

        # The session was fenced by the rewind above (a ledger fact this
        # still-live proxy process observes on its next call).
        blocked = await session.call_tool("crm.create", {"id": "y", "fields": {}})
        assert blocked.isError
        body = json.loads(blocked.content[0].text)  # type: ignore[union-attr]
        assert body["code"] == "session_fenced"
