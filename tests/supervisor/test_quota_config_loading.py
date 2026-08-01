"""belay/supervisor/server.py::Supervisor._load_quota_config -- R1 fourth
slice (ADR 0023): `belay hooks install --quota-max` writes
`identity.quota_config_path`; the supervisor best-effort loads it at
construction. `None` (no pointer file, or a broken one) must always be
the safe fallback -- opt-in extra strictness, never a reason for the
supervisor to fail to start.
"""

from __future__ import annotations

import json
from pathlib import Path

from belay.supervisor.addressing import SupervisorIdentity, supervisor_identity
from belay.supervisor.server import Supervisor


def _identity(tmp_path: Path) -> SupervisorIdentity:
    project_anchor = (tmp_path / "belay-hooks.db").resolve()
    return supervisor_identity(project_anchor, belay_home=tmp_path / "home")


def test_no_pointer_file_is_none(tmp_path: Path) -> None:
    supervisor = Supervisor(_identity(tmp_path))
    assert supervisor._quota is None


def test_valid_pointer_file_loads_a_real_config(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    identity.quota_config_path.parent.mkdir(parents=True, exist_ok=True)
    identity.quota_config_path.write_text(
        json.dumps({"max_actions": 5, "window": "12h"}), encoding="utf-8"
    )

    supervisor = Supervisor(identity)
    assert supervisor._quota is not None
    assert supervisor._quota.max_actions == 5
    from datetime import timedelta

    assert supervisor._quota.window == timedelta(hours=12)


def test_missing_max_actions_key_falls_back_to_none(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    identity.quota_config_path.parent.mkdir(parents=True, exist_ok=True)
    identity.quota_config_path.write_text(json.dumps({"window": "1d"}), encoding="utf-8")

    supervisor = Supervisor(identity)
    assert supervisor._quota is None


def test_invalid_window_string_falls_back_to_none(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    identity.quota_config_path.parent.mkdir(parents=True, exist_ok=True)
    identity.quota_config_path.write_text(
        json.dumps({"max_actions": 5, "window": "not-a-window"}), encoding="utf-8"
    )

    supervisor = Supervisor(identity)
    assert supervisor._quota is None


def test_malformed_json_falls_back_to_none(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    identity.quota_config_path.parent.mkdir(parents=True, exist_ok=True)
    identity.quota_config_path.write_text("not json at all", encoding="utf-8")

    supervisor = Supervisor(identity)
    assert supervisor._quota is None
