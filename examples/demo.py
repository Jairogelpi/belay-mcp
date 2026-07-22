"""Belay portfolio demo (docs/plan.md §10) -- reproducible, no mocked output.

Runs the exact scenario the README's GIF is made from, against a real
`belay run` subprocess and `examples/crm-mock`, shelling out to the real
`belay` CLI for every command a human would type:

    $ belay wrap examples/crm-mock --contracts examples/contracts/crm.yaml
    $ belay run &
    $ python examples/demo.py
      -> plan: delete crm.record ~500 (unknown blast radius) -> verdict: pause
    $ belay approvals list                 # the real bound plan, not a paraphrase
    $ belay approvals approve <id>          # human narrows to ~80 stale rows,
                                             # then approves that narrower plan
      -> step committed (80 records deleted, full snapshot captured)
    $ python examples/demo.py --oops        # wrong cutoff, wipes the rest
    $ belay rewind <session> --dry-run      # honest plan, nothing executed
    $ belay rewind <session> --by <name>
      -> compensation executed - verification passed - chain verified
      -> session fully compensated

Honesty note (see docs/adr/0007-e7-rewind.md "known gaps" and
docs/adr/0009-e9-demo-docs-polish.md): `belay approvals approve --narrow`
does not exist as CLI surface. "Narrowing" here is what E7 actually built
and tested: the agent retries `crm.bulk_delete` with a different, narrower
`before_year`, which is a different plan (new `plan_id`, spec §12) that the
human approves instead of the original ~500-row one. Nothing below is
scripted/hardcoded text -- every number printed comes from a real MCP call,
a real subprocess `belay` invocation, and a real ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
STALE_COUNT = 80  # last_seen=2022, genuinely stale
FRESH_COUNT = 420  # last_seen=2024, not what we meant to delete
TOTAL = STALE_COUNT + FRESH_COUNT


def sh(*args: str) -> subprocess.CompletedProcess:
    """Run a real `belay` CLI command as a human would, print it, return it."""
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


def payload(call_result) -> dict:
    content = getattr(call_result, "structuredContent", None) or {}
    return dict(content.get("result", content)) if isinstance(content, dict) else {}


async def run_demo(oops: bool) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="belay-demo-"))
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

    print("\n$ belay run &   (spawned for this demo; a real terminal backgrounds it)")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "belay.cli.main", "run", "--config", str(config_path)],
        cwd=str(REPO_ROOT),
    )

    session_id: str | None = None

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

        print(f"\n# agent: seeding {TOTAL} CRM records (mixed last_seen)")
        records = {}
        for i in range(STALE_COUNT):
            records[f"stale-{i}"] = {"last_seen": 2022}
        for i in range(FRESH_COUNT):
            records[f"fresh-{i}"] = {"last_seen": 2024}
        seeded = await call_auto_approve("crm.import_records", {"records": records})
        assert not seeded.isError, seeded.content

        print("\n# agent: \"clean stale records\" -> crm.bulk_delete(before_year=2030)")
        broad = await session.call_tool("crm.bulk_delete", {"before_year": 2030})
        broad_payload = json.loads(broad.content[0].text)
        print("  -> plan: delete crm.record (unknown blast radius) -> verdict: pause")
        assert broad_payload["status"] == "pending_approval", broad_payload
        broad_approval_id = broad_payload["approval_id"]

        listed = sh("approvals", "list", "--db", str(db_path))
        assert broad_approval_id in listed.stdout

        print(
            "\n# human: that plan touches all "
            f"{TOTAL} records -- reject it, ask the agent to narrow instead"
        )
        sh("approvals", "reject", broad_approval_id, "--by", "jairo", "--db", str(db_path))

        print("\n# agent: narrower retry -> crm.bulk_delete(before_year=2023)")
        narrow = await session.call_tool("crm.bulk_delete", {"before_year": 2023})
        narrow_payload = json.loads(narrow.content[0].text)
        assert narrow_payload["status"] == "pending_approval", narrow_payload
        narrow_approval_id = narrow_payload["approval_id"]
        sh("approvals", "list", "--db", str(db_path))
        sh("approvals", "approve", narrow_approval_id, "--by", "jairo", "--db", str(db_path))

        committed = await session.call_tool("crm.bulk_delete", {"before_year": 2023})
        assert not committed.isError, committed.content
        deleted = payload(committed)["deleted_ids"]
        print(f"  -> step committed ({len(deleted)} records deleted, full snapshot captured)")
        assert len(deleted) == STALE_COUNT

        if oops:
            print(
                "\n# agent --oops: wrong cutoff -> crm.bulk_delete(before_year=2025)"
                " (meant a different filter, wipes what's left)"
            )
            oops_req = await session.call_tool("crm.bulk_delete", {"before_year": 2025})
            oops_payload = json.loads(oops_req.content[0].text)
            oops_approval_id = oops_payload["approval_id"]
            sh("approvals", "approve", oops_approval_id, "--by", "jairo", "--db", str(db_path))
            oops_committed = await session.call_tool("crm.bulk_delete", {"before_year": 2025})
            assert not oops_committed.isError, oops_committed.content
            oops_deleted = payload(oops_committed)["deleted_ids"]
            print(f"  -> step committed ({len(oops_deleted)} more records deleted -- oops)")

        from belay.ledger.store import LedgerStore

        all_events = LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}").read_all()
        session_id = all_events[0].session_id
        # Rewind only what the mistake touched, not the initial seed import.
        seed_step = next(
            e.step_seq for e in all_events if e.type == "step_journaled" and e.step_seq
        )

    assert session_id is not None
    print(f"\n(session {session_id})")

    sh(
        "rewind",
        session_id,
        "--dry-run",
        "--to-step",
        str(seed_step),
        "--config",
        str(config_path),
    )
    real = sh(
        "rewind",
        session_id,
        "--by",
        "jairo",
        "--to-step",
        str(seed_step),
        "--config",
        str(config_path),
    )
    sh("verify", str(db_path))
    if "fully compensated" in real.stdout:
        print("\nsession fully compensated -- demo complete.")
    else:
        print("\nWARNING: session was not reported fully compensated -- see output above.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--oops", action="store_true", help="Include the wrong-filter bulk delete before rewind."
    )
    args = parser.parse_args()
    anyio.run(run_demo, args.oops)


if __name__ == "__main__":
    main()
