"""belay/cli/host_detection.py: which supported MCP clients are actually
installed on this machine (E19.1). No real subprocess/PATH dependency in
these tests -- shutil.which and subprocess.run are monkeypatched so the
suite doesn't depend on what's actually installed on the machine running it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from belay.cli.host_detection import detect_all_clients, detect_client


def test_binary_found_and_version_probe_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/claude" if name == "claude" else None
    )

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[0] == "/usr/bin/claude"
        return subprocess.CompletedProcess(args, 0, stdout="2.1.219 (Claude Code)\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = detect_client("claude-code")
    assert result.installed is True
    assert result.binary_path == "/usr/bin/claude"
    assert result.version == "2.1.219 (Claude Code)"


def test_binary_not_on_path_reports_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = detect_client("cursor")
    assert result.installed is False
    assert result.binary_path is None
    assert result.version is None


def test_version_probe_failure_still_reports_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The binary IS on PATH (that's what "installed" means here) even if
    its --version flag hangs, errors, or isn't recognized -- a version
    probe failure is not a detection failure."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex")

    def raising_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="codex", timeout=10)

    monkeypatch.setattr("subprocess.run", raising_run)

    result = detect_client("codex")
    assert result.installed is True
    assert result.binary_path == "/usr/bin/codex"
    assert result.version is None


def test_version_probe_nonzero_exit_is_treated_as_unknown_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/opencode")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="boom"),
    )
    result = detect_client("opencode")
    assert result.installed is True
    assert result.version is None


def test_claude_desktop_detected_via_config_dir_presence(tmp_path: Path) -> None:
    config_dir = tmp_path / "Claude"
    config_dir.mkdir()
    result = detect_client("claude-desktop", claude_desktop_config_dir=config_dir)
    assert result.installed is True
    assert result.binary_path is None
    assert result.version is None


def test_claude_desktop_not_detected_when_dir_absent(tmp_path: Path) -> None:
    result = detect_client("claude-desktop", claude_desktop_config_dir=tmp_path / "nonexistent")
    assert result.installed is False


def test_detect_all_clients_returns_every_supported_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = detect_all_clients(claude_desktop_config_dir=None)
    assert set(result) == {"claude-code", "cursor", "codex", "opencode", "claude-desktop"}
    assert all(not d.installed for d in result.values())
