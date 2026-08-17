"""Shared fake `codex`/`claude` CLI fixtures for E22 tests
(`test_connection.py`, `test_connect_cli.py`).

The fake CLI is a real, standalone, directly-executable script (never the
real user's `codex`/`claude`) that understands just enough of the real
`mcp add/get/list/remove` syntax to exercise `belay/cli/
client_registration.py`'s adapters honestly -- including the `--scope user
--transport stdio` flags Claude's real CLI inserts before `<name>` that
Codex's doesn't. Each generated fake has its OWN fixed state/config/log
paths baked directly into its script (not read from the caller's
environment) -- necessary so a single `connect()` call juggling both a
codex fake and a claude fake at once never has them fight over shared
env-var names; it also means these fakes work correctly even when
`connect()`/`disconnect()` pass a deliberately minimal, isolated `env`
that doesn't happen to carry any fake-specific variable through at all.
"""

from __future__ import annotations

import json as jsonlib
import stat
import sys
import textwrap
from pathlib import Path

import pytest

_FAKE_CLI_BODY = textwrap.dedent(
    """
    def main() -> int:
        argv = sys.argv[1:]
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(argv) + "\\n")
        if argv == ["--version"]:
            print("fake-cli 0.0.0")
            return 0
        if len(argv) < 2 or argv[0] != "mcp":
            print("unrecognized invocation", file=sys.stderr)
            return 2

        def _load_state() -> dict:
            if not FAKE_HOME.joinpath("state.json").is_file():
                return {}
            return json.loads(FAKE_HOME.joinpath("state.json").read_text(encoding="utf-8"))

        def _save_state(state: dict) -> None:
            FAKE_HOME.mkdir(parents=True, exist_ok=True)
            FAKE_HOME.joinpath("state.json").write_text(
                json.dumps(state, indent=2), encoding="utf-8"
            )
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(state, indent=2, sort_keys=True) + "\\n"
            CONFIG_PATH.write_text(text, encoding="utf-8")

        op = argv[1]
        if op == "add":
            # add [--scope user --transport stdio] <name> -- <command> <args...>
            rest = argv[2:]
            sep = rest.index("--")
            name = rest[sep - 1]
            command = rest[sep + 1]
            cmd_args = rest[sep + 2:]
            if name in ("TRIGGER_HANG", "trigger-hang"):
                time.sleep(5)
                return 0
            if name in ("TRIGGER_FAIL", "trigger-fail"):
                print("simulated failure", file=sys.stderr)
                return 1
            state = _load_state()
            if name in state:
                print(f"error: server '{name}' already exists", file=sys.stderr)
                return 1
            state[name] = {"command": command, "args": cmd_args}
            _save_state(state)
            return 0
        if op == "get":
            name = argv[2]
            state = _load_state()
            if name not in state:
                print(f"error: no server named '{name}'", file=sys.stderr)
                return 1
            print(json.dumps(state[name]))
            return 0
        if op == "list":
            state = _load_state()
            for name in sorted(state):
                print(name)
            return 0
        if op == "remove":
            name = argv[2]
            state = _load_state()
            if name not in state:
                print(f"error: no server named '{name}'", file=sys.stderr)
                return 1
            del state[name]
            _save_state(state)
            return 0
        print("unrecognized op", file=sys.stderr)
        return 2

    sys.exit(main())
    """
)


def _write_fake_cli(
    bin_dir: Path, exe_name: str, *, fake_home: Path, config_path: Path, log_path: Path
) -> Path:
    """Write a real, standalone, directly-executable fake CLI named
    `exe_name` into `bin_dir`, with `fake_home`/`config_path`/`log_path`
    baked into the script itself (via `json.dumps` -- safe against
    backslashes/quotes in a Windows path used as a Python string literal).
    On Windows: a `.cmd` shim invoked by its FULL resolved path (proven to
    run fine under `subprocess.run(..., shell=False)`). On POSIX: a
    `#!/usr/bin/env python3` script with the execute bit set."""
    prefix = (
        "import json\nimport sys\nimport time\nfrom pathlib import Path\n\n"
        f"FAKE_HOME = Path({jsonlib.dumps(str(fake_home))})\n"
        f"CONFIG_PATH = Path({jsonlib.dumps(str(config_path))})\n"
        f"LOG_PATH = Path({jsonlib.dumps(str(log_path))})\n\n"
    )
    body = prefix + _FAKE_CLI_BODY

    script_path = bin_dir / f"_{exe_name}_impl.py"
    script_path.write_text(body, encoding="utf-8")

    if sys.platform == "win32":
        exe_path = bin_dir / f"{exe_name}.cmd"
        exe_path.write_text(
            f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n', encoding="utf-8"
        )
        return exe_path

    exe_path = bin_dir / exe_name
    exe_path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    exe_path.chmod(exe_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return exe_path


@pytest.fixture
def make_fake_codex(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    def _make(fake_home: Path, config_path: Path, log_path: Path) -> Path:
        return _write_fake_cli(
            bin_dir, "fake-codex", fake_home=fake_home, config_path=config_path,
            log_path=log_path,
        )

    return _make


@pytest.fixture
def make_fake_claude(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    def _make(fake_home: Path, config_path: Path, log_path: Path) -> Path:
        return _write_fake_cli(
            bin_dir, "fake-claude", fake_home=fake_home, config_path=config_path,
            log_path=log_path,
        )

    return _make


def read_log(log_path: Path) -> list[list[str]]:
    if not log_path.is_file():
        return []
    return [jsonlib.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
