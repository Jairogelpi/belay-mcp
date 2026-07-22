"""`belay verify <db>` against a real SQLite database (plan.md E2 (d))."""

from __future__ import annotations

from pathlib import Path

from belay.cli.main import app
from belay.ledger.store import LedgerStore
from typer.testing import CliRunner

runner = CliRunner()


def test_verify_ok_on_a_healthy_ledger(tmp_path: Path) -> None:
    db_path = tmp_path / "belay.db"
    store = LedgerStore(f"sqlite:///{db_path.as_posix()}")
    store.append("s1", "step_journaled", {}, step_seq=1)
    store.append("s1", "result_recorded", {}, step_seq=1)
    store.append("s1", "compensation_registered", {}, step_seq=1)
    store.append("s1", "step_committed", {}, step_seq=1)

    result = runner.invoke(app, ["verify", str(db_path)])

    assert result.exit_code == 0
    assert "chain: OK" in result.stdout
    assert "coherence: OK" in result.stdout


def test_verify_fails_on_a_tampered_ledger(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "belay.db"
    store = LedgerStore(f"sqlite:///{db_path.as_posix()}")
    store.append("s1", "step_journaled", {"a": 1}, step_seq=1)
    store.append("s1", "step_committed", {}, step_seq=1)

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE events SET hash = 'deadbeef' WHERE id = 1")
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["verify", str(db_path)])

    assert result.exit_code == 1
    assert "FAILED" in result.stdout
