"""Detects which supported MCP clients are actually present on this
machine (plan-v2 E19.1) -- so `belay init --client auto` registers Belay
only where there's a real client to register with, instead of blindly
writing a config file for a tool that was never installed (the previous
behavior of `--client all`, which is unchanged and kept for anyone who
wants that -- e.g. preparing a repo's committed config files for
teammates who may have a client this machine doesn't).

Detection is binary-presence-based for CLI-driven clients (`shutil.which`,
the same PATH-search Python itself would use to exec them) plus a
best-effort version read; Claude Desktop has no CLI to probe, so its
signal is whether its app-data config directory exists at all (created by
the real desktop app on first run, not something `belay init` itself
would ever create ahead of detection).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Binary name to check via shutil.which(), or None for clients detected
#: some other way.
_BINARY_BY_CLIENT: dict[str, str | None] = {
    "claude-code": "claude",
    "cursor": "cursor",
    "codex": "codex",
    "opencode": "opencode",
    "claude-desktop": None,
}


@dataclass(frozen=True)
class DetectedClient:
    name: str
    installed: bool
    binary_path: str | None
    version: str | None


def _binary_version(binary_path: str) -> str | None:
    """Best-effort `<binary> --version`, first line only. Never raises --
    an unrecognized/hanging/failing version flag just means an unknown
    version, not a detection failure (the binary was still found on PATH).
    `shell=True`: on Windows several of these (`claude`, `codex`,
    `opencode`) install as `.cmd` shims, which Python's subprocess cannot
    exec directly via CreateProcess without going through a shell --
    confirmed the hard way earlier this project (see
    `tests/hooks/test_live_conformance.py`'s own `_run` helper for the
    same fix, same reasoning: safe here specifically because every
    argument is a fixed literal, never attacker-controlled text)."""
    try:
        result = subprocess.run(
            [binary_path, "--version"],
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    stripped = result.stdout.strip()
    return stripped.splitlines()[0] if stripped else None


def detect_client(name: str, *, claude_desktop_config_dir: Path | None = None) -> DetectedClient:
    binary_name = _BINARY_BY_CLIENT.get(name)
    if binary_name is not None:
        found = shutil.which(binary_name)
        version = _binary_version(found) if found else None
        return DetectedClient(
            name=name, installed=found is not None, binary_path=found, version=version
        )

    installed = claude_desktop_config_dir is not None and claude_desktop_config_dir.is_dir()
    return DetectedClient(name=name, installed=installed, binary_path=None, version=None)


def detect_all_clients(
    *, claude_desktop_config_dir: Path | None = None
) -> dict[str, DetectedClient]:
    return {
        name: detect_client(name, claude_desktop_config_dir=claude_desktop_config_dir)
        for name in _BINARY_BY_CLIENT
    }
