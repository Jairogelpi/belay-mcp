"""belay/supervisor/server.py::Supervisor._load_anomaly_config -- R1.8.x
(ADR 0026): `belay hooks install --anomaly` writes
`identity.anomaly_pointer_path`; the supervisor best-effort loads it at
construction. `None` (no pointer file at all) is the unchanged,
permissive default. Once that pointer file exists, R1.6's fail-closed
posture applies here too: an unreadable target returns
`ConfigUnavailable` rather than silently falling back to `None` (no
anomaly check). Unlike quota, there is no JSON content to be invalid --
the pointer file's mere presence and readability is the whole check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from belay.supervisor.addressing import SupervisorIdentity, supervisor_identity
from belay.supervisor.server import ConfigUnavailable, Supervisor


def _identity(tmp_path: Path) -> SupervisorIdentity:
    project_anchor = (tmp_path / "belay-hooks.db").resolve()
    return supervisor_identity(project_anchor, belay_home=tmp_path / "home")


def test_no_pointer_file_is_none(tmp_path: Path) -> None:
    supervisor = Supervisor(_identity(tmp_path))
    assert supervisor._anomaly is None


def test_valid_pointer_file_loads_a_real_config(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    identity.anomaly_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    identity.anomaly_pointer_path.write_text("1", encoding="utf-8")

    supervisor = Supervisor(identity)
    assert supervisor._anomaly is not None
    from belay.hooks.anomaly import AnomalyConfig

    assert isinstance(supervisor._anomaly, AnomalyConfig)


def test_unreadable_pointer_file_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity(tmp_path)
    identity.anomaly_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    identity.anomaly_pointer_path.write_text("1", encoding="utf-8")

    original_read_text = Path.read_text

    def _flaky_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == identity.anomaly_pointer_path:
            raise OSError("permission denied (simulated)")
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _flaky_read_text)

    supervisor = Supervisor(identity)
    assert isinstance(supervisor._anomaly, ConfigUnavailable)


def test_broken_anomaly_config_denies_every_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same R1.6 posture as a broken contracts/quota/allowlist pointer:
    a configured-but-unreadable anomaly pointer must deny everything via
    `_decide_pre`'s `broken` check, not just return `ConfigUnavailable`
    from the loader and otherwise proceed as if unconfigured."""
    from belay.hooks.claude_code_adapter import normalize

    identity = _identity(tmp_path)
    identity.anomaly_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    identity.anomaly_pointer_path.write_text("1", encoding="utf-8")

    original_read_text = Path.read_text

    def _flaky_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == identity.anomaly_pointer_path:
            raise OSError("permission denied (simulated)")
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _flaky_read_text)

    supervisor = Supervisor(identity)
    raw = {
        "session_id": "s1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "toolu_broken_anomaly",
    }
    event = normalize(raw, installation_id=identity.install_id)
    decision = supervisor._decide_pre(event)
    assert decision.verdict == "deny"
    assert "configured_policy_unavailable" in decision.reason
    assert "anomaly" in decision.reason
