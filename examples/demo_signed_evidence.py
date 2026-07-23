"""Signed, offline-verifiable evidence demo (plan-v2 E13).

Runs a real session against `examples/crm-mock`, exports signed evidence via
`belay verify-export`, then verifies it in a clean subdirectory with NO
access to the original `belay.db` -- proving the "no Belay installation
needed" claim for real -- and finally demonstrates a tamper attempt (one
byte flipped in a copy) being caught with a precise error.
"""

from __future__ import annotations

import os
import shutil
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


async def run_demo() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="belay-demo-signed-"))
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

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        print("\n# agent: seeding 3 CRM records")
        records = {f"r{i}": {"last_seen": 2024} for i in range(3)}
        seeded = await session.call_tool("crm.import_records", {"records": records})
        assert not seeded.isError, seeded.content

    from belay.ledger.store import LedgerStore

    ledger = LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}")
    all_events = ledger.read_all()
    session_id = all_events[0].session_id
    print(f"\n(session {session_id}, {len(all_events)} events)")

    print("\n# operator: generate an Ed25519 signing key")
    key_path = tmp / "signing.key"
    keygen_result = sh("keygen", str(key_path))
    assert keygen_result.returncode == 0

    print("\n# operator: export signed evidence for this session")
    evidence_path = tmp / "evidence.json"
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

    print("\n# third party: verify in a CLEAN directory with NO belay.db present")
    clean_dir = tmp / "no_db_here"
    clean_dir.mkdir()
    shutil.copy(evidence_path, clean_dir / "evidence.json")
    shutil.copy(f"{key_path}.pub", clean_dir / "signing.key.pub")
    assert not (clean_dir / "belay.db").exists()
    assert list(clean_dir.glob("*.db")) == [], "must be verifiable with zero DB access"

    clean_verify = sh(
        "verify-evidence",
        str(clean_dir / "evidence.json"),
        "--pubkey",
        str(clean_dir / "signing.key.pub"),
    )
    assert clean_verify.returncode == 0
    assert "VALID" in clean_verify.stdout

    print("\n# tamper attempt: flip one byte in a copy of the exported evidence")
    tampered_path = clean_dir / "evidence.tampered.json"
    raw = (clean_dir / "evidence.json").read_text(encoding="utf-8")
    events_start = raw.index('"events"')
    pos = raw.index('"payload"', events_start)
    tampered = raw[:pos] + raw[pos:].replace("crm.import_records", "crm.IMPORT_RECORDS", 1)
    assert tampered != raw, "tamper must actually change bytes"
    tampered_path.write_text(tampered, encoding="utf-8")

    tampered_verify = sh(
        "verify-evidence",
        str(tampered_path),
        "--pubkey",
        str(clean_dir / "signing.key.pub"),
    )
    assert tampered_verify.returncode == 1, "tampered evidence must be rejected"
    assert "INVALID" in tampered_verify.stdout
    assert "chain" in tampered_verify.stdout, "tamper must be reported precisely, as a chain break"

    print("\ndemo complete: signed evidence verified offline with zero DB access,")
    print("tamper attempt caught with a precise error (chain break, not opaque failure).")


def main() -> None:
    anyio.run(run_demo)


if __name__ == "__main__":
    main()
