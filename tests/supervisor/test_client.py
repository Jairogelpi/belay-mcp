"""belay/supervisor/client.py: fail-closed behavior (spec 8.1) when the
supervisor can't be reached at all -- the one path that must never leave a
PreToolUse call unanswered.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from belay.supervisor import client as client_module
from belay.supervisor.addressing import supervisor_identity


def test_fails_closed_when_supervisor_cannot_be_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "ensure_running", lambda identity, db_path: False)
    identity = supervisor_identity(tmp_path / "belay-hooks.db", belay_home=tmp_path / "home")

    result = client_module.send_hook_event(identity, "unused", "claude-code", {})

    out = result["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "supervisor unavailable" in out["permissionDecisionReason"]
    assert "spec 8.1" in out["permissionDecisionReason"]


def test_shutdown_on_a_never_started_supervisor_is_a_harmless_no_op(tmp_path: Path) -> None:
    identity = supervisor_identity(tmp_path / "belay-hooks.db", belay_home=tmp_path / "home")
    assert client_module.send_shutdown(identity) is False
