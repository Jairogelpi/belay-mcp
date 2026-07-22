"""`belay plan <tool> --args '<json>'` (plan.md E4 (d))."""

from __future__ import annotations

import json
from pathlib import Path

from belay.cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[2]


def _wrap_config(tmp_path: Path) -> Path:
    out = tmp_path / "belay.wrap.json"
    result = runner.invoke(
        app,
        [
            "wrap",
            str(REPO_ROOT / "examples" / "fs-server"),
            "--contracts",
            str(REPO_ROOT / "examples" / "contracts" / "fs.yaml"),
            "--db",
            str(tmp_path / "belay.db"),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.stdout
    return out


def test_plan_command_prints_the_full_plan_object(tmp_path: Path) -> None:
    config = _wrap_config(tmp_path)
    result = runner.invoke(
        app,
        [
            "plan",
            "fs.delete_file",
            "--args",
            json.dumps({"path": "a.txt"}),
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 0, result.stdout
    plan = json.loads(result.stdout)
    # Every field of the Plan model (spec §5.1) must be present.
    for field in (
        "plan_id",
        "session_id",
        "tool",
        "args",
        "effects",
        "reversibility",
        "policy_verdict",
        "policy_reasons",
        "requires_approval",
        "confidence",
        "unknown",
        "created_at",
        "expires_at",
    ):
        assert field in plan
    assert plan["tool"] == "fs.delete_file"
    assert plan["args"] == {"path": "a.txt"}
    assert plan["reversibility"] == "conditional"


def test_plan_command_applies_a_supplied_policy(tmp_path: Path) -> None:
    config = _wrap_config(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "belay_policy: '0.1'\ntools:\n  - match: 'fs.delete_*'\n    verdict: deny\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "plan",
            "fs.delete_file",
            "--args",
            json.dumps({"path": "a.txt"}),
            "--config",
            str(config),
            "--policy",
            str(policy_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    plan = json.loads(result.stdout)
    assert plan["policy_verdict"] == "deny"
    assert plan["policy_reasons"] == ["tools[0]"]
