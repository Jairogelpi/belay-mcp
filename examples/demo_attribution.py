"""Identity attribution demo (plan-v2 E14).

Two different `--initiated-by` identities run sessions against the SAME
wrapped server; `belay verify-evidence` correctly distinguishes which
identity triggered which session, and E13's signature covers the identity
field so tampering with it is caught exactly like tampering with the chain.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import anyio
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


async def _run_one_session(config_path: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "belay.cli.main", "run", "--config", str(config_path)],
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(
            "crm.import_records", {"records": {"r1": {"last_seen": 2024}}}
        )
        assert not result.isError, result.content


async def run_demo() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="belay-demo-attribution-"))
    db_path = tmp / "belay.db"  # one shared ledger for both identities

    print("# operator: wrap the same CRM server once (shared ledger for both identities)")
    config_alice = tmp / "belay.wrap.alice.json"
    wrapped_alice = sh(
        "wrap",
        str(REPO_ROOT / "examples" / "crm-mock"),
        "--contracts",
        str(REPO_ROOT / "examples" / "contracts" / "crm.yaml"),
        "--db",
        str(db_path),
        "--out",
        str(config_alice),
        "--initiated-by",
        "alice@corp",
    )
    assert wrapped_alice.returncode == 0

    print("\n# alice@corp: run a real session (a real stdio call, real ledger events)")
    await _run_one_session(config_alice)

    print("\n# scheduler-bot on behalf of bob@corp: run a second real session, same server")
    config_bob = tmp / "belay.wrap.bob.json"
    wrapped_bob = sh(
        "wrap",
        str(REPO_ROOT / "examples" / "crm-mock"),
        "--contracts",
        str(REPO_ROOT / "examples" / "contracts" / "crm.yaml"),
        "--db",
        str(db_path),
        "--out",
        str(config_bob),
        "--initiated-by",
        "scheduler-bot",
        "--on-behalf-of",
        "bob@corp",
    )
    assert wrapped_bob.returncode == 0
    await _run_one_session(config_bob)

    from belay.ledger.store import LedgerStore

    ledger = LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}")
    all_events = ledger.read_all()
    session_ids = sorted({e.session_id for e in all_events})
    assert len(session_ids) == 2, "two sessions expected, one per identity"
    print(f"\n(ledger has {len(all_events)} events across 2 sessions: {session_ids})")

    print("\n# operator: sign + verify-evidence for EACH session, no cross-contamination")
    key_path = tmp / "signing.key"
    sh("keygen", str(key_path))

    seen_identities: dict[str, str] = {}
    for session_id in session_ids:
        evidence_path = tmp / f"evidence.{session_id}.json"
        export_result = sh(
            "verify-export",
            session_id,
            "--key",
            str(key_path),
            "--db",
            str(db_path),
            "-o",
            str(evidence_path),
        )
        assert export_result.returncode == 0

        verify_result = sh("verify-evidence", str(evidence_path))
        assert verify_result.returncode == 0
        assert "VALID" in verify_result.stdout

        for line in verify_result.stdout.splitlines():
            if line.startswith("initiated_by:"):
                seen_identities[session_id] = line.split(":", 1)[1].strip()

    print(f"\n(session -> initiator map from verify-evidence: {seen_identities})")
    identities = set(seen_identities.values())
    assert identities == {"alice@corp", "scheduler-bot"}, (
        f"expected exactly {{'alice@corp', 'scheduler-bot'}}, got {identities}"
    )

    print("\n# tamper attempt: flip the initiated_by claim in one session's exported evidence")
    tampered_session = session_ids[0]
    evidence_path = tmp / f"evidence.{tampered_session}.json"
    raw = evidence_path.read_text(encoding="utf-8")
    original_id = seen_identities[tampered_session]
    # Tamper only the bundle-level `initiated_by` claim (the LAST occurrence
    # in the file -- the embedded `session_started` event's own copy comes
    # first), so the signature is what's expected to break, not the chain.
    needle = f'"initiated_by": "{original_id}"'
    last_pos = raw.rindex(needle)
    tampered = raw[:last_pos] + '"initiated_by": "mallory@evil.example"' + raw[
        last_pos + len(needle) :
    ]
    assert tampered != raw, "tamper must actually change bytes"
    tampered_path = tmp / "evidence.tampered.json"
    tampered_path.write_text(tampered, encoding="utf-8")

    tampered_verify = sh("verify-evidence", str(tampered_path))
    assert tampered_verify.returncode == 1, "tampered identity must be rejected"
    assert "INVALID" in tampered_verify.stdout
    assert "signature" in tampered_verify.stdout

    print("\ndemo complete: two identities, two sessions, one shared server -- no")
    print("cross-contamination, and E13 signing catches identity tampering exactly")
    print("like it catches chain tampering.")


def main() -> None:
    anyio.run(run_demo)


if __name__ == "__main__":
    main()
