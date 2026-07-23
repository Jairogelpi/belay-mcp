"""Counterfactual replay demo (plan-v2 E12): "what if the human had denied instead?"

Runs the exact real bulk-delete-then-rewind scenario from `examples/demo.py`
(seed -> broad delete paused -> human narrows and approves -> committed ->
rewind), then, entirely offline from the same real ledger, asks what would
have happened if the human had *denied* the narrower delete instead of
approving it -- via `belay counterfactual`, never touching the real upstream
or the real session's ledger again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import anyio
from belay.ledger.store import LedgerStore
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent


def sh(*args: str) -> subprocess.CompletedProcess:
    printed = "$ belay " + " ".join(args)
    print(printed)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "belay.cli.main", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    out = (result.stdout + result.stderr).rstrip()
    if out:
        print(out)
    return result


async def run_demo() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="belay-demo-cf-"))
    db_path = tmp / "belay.db"
    config_path = tmp / "belay.wrap.json"

    wrapped = sh(
        "wrap",
        str(REPO_ROOT / "examples" / "crm-mock"),
        "--contracts",
        str(REPO_ROOT / "examples" / "contracts" / "crm.yaml"),
        "--db",
        str(db_path),
        "--out",
        str(config_path),
    )
    assert wrapped.returncode == 0, "belay wrap failed"

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "belay.cli.main", "run", "--config", str(config_path)],
        cwd=str(REPO_ROOT),
    )

    session_id: str | None = None
    fork_step: int | None = None

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        async def call_auto_approve(tool: str, tool_args: dict) -> object:
            """Call a tool; if it pauses, approve it (unattended setup step) and retry."""
            result = await session.call_tool(tool, tool_args)
            if not result.isError:
                body = json.loads(result.content[0].text) if result.content else {}
                if isinstance(body, dict) and body.get("status") == "pending_approval":
                    sh(
                        "approvals",
                        "approve",
                        body["approval_id"],
                        "--by",
                        "jairo",
                        "--db",
                        str(db_path),
                    )
                    result = await session.call_tool(tool, tool_args)
            return result

        print("\n# agent: seeding 5 CRM records")
        records = {f"r{i}": {"last_seen": 2022 if i < 3 else 2024} for i in range(5)}
        seeded = await call_auto_approve("crm.import_records", {"records": records})
        assert not seeded.isError, seeded.content

        print("\n# agent: crm.bulk_delete(before_year=2023) -> paused (irreversible)")
        req = await session.call_tool("crm.bulk_delete", {"before_year": 2023})
        payload = json.loads(req.content[0].text)
        assert payload["status"] == "pending_approval", payload
        approval_id = payload["approval_id"]

        print("\n# human: approves the narrow, correct delete")
        sh("approvals", "approve", approval_id, "--by", "jairo", "--db", str(db_path))

        committed = await session.call_tool("crm.bulk_delete", {"before_year": 2023})
        assert not committed.isError, committed.content

        ledger = LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}")
        all_events = ledger.read_all()
        session_id = all_events[0].session_id
        bulk_delete_steps = {
            e.step_seq
            for e in all_events
            if e.type == "plan_created" and e.payload.get("tool") == "crm.bulk_delete"
        }
        fork_step = next(
            e.step_seq
            for e in all_events
            if e.type == "policy_evaluated"
            and e.payload.get("verdict") == "pause"
            and e.step_seq in bulk_delete_steps
        )

    assert session_id is not None and fork_step is not None
    print(f"\n(session {session_id}, forking at step {fork_step})")

    before_row_count = len(LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}").read_all())

    print("\n# offline, no upstream, no ledger writes: what if the human had denied instead?")
    result = sh(
        "counterfactual",
        session_id,
        "--at-step",
        str(fork_step),
        "--override",
        '{"verdict": "deny"}',
        "--db",
        str(db_path),
        "--json",
    )
    assert result.returncode == 0, result.stdout
    report = json.loads(result.stdout)

    print("\nbranch report:")
    for step in report["steps"]:
        print(f"  step {step['step_seq']}: {step['tool']} -> {step['outcome']} ({step['basis']})")
    print(f"  is_noop={report['is_noop']}")

    after_row_count = len(LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}").read_all())
    print(
        f"\nledger row count before={before_row_count} after={after_row_count} "
        f"(unchanged={before_row_count == after_row_count})"
    )
    assert before_row_count == after_row_count, "counterfactual must never write to the real ledger"
    assert not report["is_noop"], "the override genuinely diverges from what really happened"

    verify_result = sh("verify", str(db_path))
    assert verify_result.returncode == 0
    print("\ncounterfactual demo complete: real session untouched, branch reported honestly.")


def main() -> None:
    anyio.run(run_demo)


if __name__ == "__main__":
    main()
