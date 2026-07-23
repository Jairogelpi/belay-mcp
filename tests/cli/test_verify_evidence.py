"""`belay keygen`/`verify-export`/`verify-evidence` (plan-v2 E13).

The round-trip is proved with a real CLI invocation against a real SQLite
ledger, then verified in a directory with no `belay.db` anywhere near it --
the "no Belay installation needed" claim, tested literally.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from belay.cli.main import app
from belay.ledger.store import LedgerStore
from typer.testing import CliRunner

runner = CliRunner()


def _seed(db_path: Path, session_id: str = "s1") -> None:
    store = LedgerStore(f"sqlite:///{db_path.as_posix()}")
    store.append(session_id, "step_journaled", {"tool": "crm.delete"}, step_seq=1)
    store.append(session_id, "result_recorded", {"ok": True}, step_seq=1)
    store.append(session_id, "compensation_registered", {}, step_seq=1)
    store.append(session_id, "step_committed", {}, step_seq=1)


def test_keygen_writes_a_private_key_and_a_separate_pub_file(tmp_path: Path) -> None:
    key_path = tmp_path / "signing.key"
    result = runner.invoke(app, ["keygen", str(key_path)])

    assert result.exit_code == 0
    assert key_path.exists()
    assert (tmp_path / "signing.key.pub").exists()
    assert b"PRIVATE KEY" in key_path.read_bytes()


def test_verify_export_then_verify_evidence_roundtrip_in_a_db_free_directory(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "belay.db"
    _seed(db_path)

    key_path = tmp_path / "signing.key"
    runner.invoke(app, ["keygen", str(key_path)])

    evidence_path = tmp_path / "evidence.json"
    export_result = runner.invoke(
        app,
        [
            "verify-export",
            "s1",
            "--key",
            str(key_path),
            "--db",
            str(db_path),
            "-o",
            str(evidence_path),
        ],
    )
    assert export_result.exit_code == 0, export_result.output
    assert evidence_path.exists()

    # Move the evidence + pubkey into a fresh directory with no belay.db at all.
    clean_dir = tmp_path / "clean_no_db"
    clean_dir.mkdir()
    shutil.copy(evidence_path, clean_dir / "evidence.json")
    shutil.copy(tmp_path / "signing.key.pub", clean_dir / "signing.key.pub")

    assert not (clean_dir / "belay.db").exists()
    assert list(clean_dir.glob("*.db")) == []

    verify_result = runner.invoke(
        app,
        [
            "verify-evidence",
            str(clean_dir / "evidence.json"),
            "--pubkey",
            str(clean_dir / "signing.key.pub"),
        ],
    )

    assert verify_result.exit_code == 0
    assert "VALID" in verify_result.stdout


def test_verify_evidence_reports_precise_tamper_stage(tmp_path: Path) -> None:
    db_path = tmp_path / "belay.db"
    _seed(db_path)
    key_path = tmp_path / "signing.key"
    runner.invoke(app, ["keygen", str(key_path)])
    evidence_path = tmp_path / "evidence.json"
    runner.invoke(
        app,
        [
            "verify-export",
            "s1",
            "--key",
            str(key_path),
            "--db",
            str(db_path),
            "-o",
            str(evidence_path),
        ],
    )

    raw = evidence_path.read_text(encoding="utf-8")
    tampered = raw.replace('"tool": "crm.delete"', '"tool": "crm.DELETE"')
    assert tampered != raw
    evidence_path.write_text(tampered, encoding="utf-8")

    verify_result = runner.invoke(app, ["verify-evidence", str(evidence_path)])

    assert verify_result.exit_code == 1
    assert "INVALID" in verify_result.stdout
    assert "chain" in verify_result.stdout


def test_verify_export_with_no_events_for_session_fails_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "belay.db"
    _seed(db_path)
    key_path = tmp_path / "signing.key"
    runner.invoke(app, ["keygen", str(key_path)])

    result = runner.invoke(
        app,
        [
            "verify-export",
            "no-such-session",
            "--key",
            str(key_path),
            "--db",
            str(db_path),
            "-o",
            str(tmp_path / "out.json"),
        ],
    )

    assert result.exit_code == 1
