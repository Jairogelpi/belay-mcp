"""belay/cli/dashboard.py: static HTML snapshot rendering."""

from __future__ import annotations

from belay.cli.dashboard import build_dashboard_data, render_dashboard
from belay.ledger.store import LedgerStore


def test_build_dashboard_data_summarizes_sessions(tmp_path) -> None:
    db_path = tmp_path / "belay.db"
    ledger = LedgerStore(f"sqlite:///{db_path.as_posix()}")
    ledger.append("s_1", "session_started", {}, initiated_by="agent-bot")
    ledger.append("s_1", "plan_created", {"tool": "fs.write_file"}, step_seq=1)

    data = build_dashboard_data(str(db_path))
    assert "s_1" in data["sessions"]
    assert data["sessions"]["s_1"]["initiated_by"] == "agent-bot"
    assert data["sessions"]["s_1"]["event_count"] == 2


def test_render_dashboard_produces_self_contained_html(tmp_path) -> None:
    db_path = tmp_path / "belay.db"
    ledger = LedgerStore(f"sqlite:///{db_path.as_posix()}")
    ledger.append("s_1", "session_started", {}, initiated_by="agent-bot")

    html = render_dashboard(str(db_path))
    assert "<!doctype html>" in html.lower()
    assert "s_1" in html
    assert "belay/explanation" not in html  # sanity: not leaking unrelated internals
