"""`belay wrap <server-dir> --contracts <path>` (plan.md E3 (a))."""

from __future__ import annotations

import sys
from pathlib import Path

from belay.cli.main import app
from belay.proxy.config import WrapConfig
from typer.testing import CliRunner

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_wrap_writes_a_valid_config(tmp_path: Path) -> None:
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
    config = WrapConfig.load(out)
    assert config.upstream.command == sys.executable
    assert config.upstream.args == [str(REPO_ROOT / "examples" / "fs-server" / "server.py")]
    assert config.unsafe_passthrough == []


def test_wrap_records_unsafe_passthrough_tools(tmp_path: Path) -> None:
    out = tmp_path / "belay.wrap.json"
    result = runner.invoke(
        app,
        [
            "wrap",
            str(REPO_ROOT / "examples" / "fs-server"),
            "--contracts",
            str(REPO_ROOT / "examples" / "contracts" / "fs.yaml"),
            "--unsafe-passthrough",
            "fs.list_files, fs.some_tool",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.stdout
    config = WrapConfig.load(out)
    assert config.unsafe_passthrough == ["fs.list_files", "fs.some_tool"]


def test_wrap_fails_fast_on_missing_server_entry_point(tmp_path: Path) -> None:
    empty_dir = tmp_path / "no-server"
    empty_dir.mkdir()
    result = runner.invoke(
        app,
        [
            "wrap",
            str(empty_dir),
            "--contracts",
            str(REPO_ROOT / "examples" / "contracts" / "fs.yaml"),
        ],
    )
    assert result.exit_code == 1


def test_wrap_fails_fast_on_invalid_contract(tmp_path: Path) -> None:
    bad_contract = tmp_path / "bad.yaml"
    bad_contract.write_text(
        "belay_contract: '0.1'\ntool: x\nreversibility: reversible\neffects: []\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "wrap",
            str(REPO_ROOT / "examples" / "fs-server"),
            "--contracts",
            str(bad_contract),
        ],
    )
    assert result.exit_code != 0
