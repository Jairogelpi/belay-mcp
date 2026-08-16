"""E22 Task 3: official-CLI client registration adapters.

Every test here runs against a FAKE `codex`/`claude` executable (a small
real, standalone Python script this test writes to an isolated `tmp_path`
and invokes via `sys.executable`) -- never the real user's `~/.codex` or
`~/.claude.json`. The fake owns a tiny made-up on-disk format (JSON lines)
that only these tests understand; the real adapter code never parses it,
it only snapshots the file's raw bytes -- which is the actual point:
`CodexAdapter`/`ClaudeAdapter` must work by delegating registration to the
official CLI, not by understanding its config format.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest
from belay.cli.client_registration import (
    ClaudeAdapter,
    ClientRegistrationError,
    CodexAdapter,
    claude_desktop_config_path,
    claude_user_config_path,
    codex_config_path,
    register_claude_desktop,
    remove_claude_desktop,
)

#: A fake `mcp add/get/list/remove` CLI shared by both codex/claude fakes
#: (the two official CLIs' real syntax differs only in the extra
#: `--scope user --transport stdio` claude inserts before `<name>`, which
#: this fake doesn't need to care about -- it only cares about the trailing
#: `-- <command> <args...>` shape both share). Config state lives as one
#: JSON object per line in `$FAKE_HOME/config.state` (this fake's own
#: format, deliberately NOT a real config.toml/`.claude.json` shape, to
#: keep the assertion "the adapter never parses this format itself" true
#: by construction).
_FAKE_CLI_BODY = textwrap.dedent(
    """
    import json
    import os
    import sys
    import time
    from pathlib import Path

    def _home() -> Path:
        return Path(os.environ["FAKE_HOME"])

    def _log(argv):
        log_path = Path(os.environ["FAKE_LOG"])
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(argv) + "\\n")

    def _state_path() -> Path:
        return _home() / "state.json"

    def _load_state() -> dict:
        p = _state_path()
        if not p.is_file():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    def _save_state(state: dict) -> None:
        _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
        # Also write the "real" config file this fake pretends to own, so
        # adapter-side byte snapshots actually change on add/remove.
        config_path = Path(os.environ["FAKE_CONFIG_PATH"])
        config_path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(state, indent=2, sort_keys=True) + "\\n"
        config_path.write_text(text, encoding="utf-8")

    def main() -> int:
        argv = sys.argv[1:]
        _log(argv)
        if argv == ["--version"]:
            print("fake-cli 0.0.0")
            return 0
        if len(argv) < 2 or argv[0] != "mcp":
            print("unrecognized invocation", file=sys.stderr)
            return 2

        op = argv[1]
        if op == "add":
            # add [--scope user --transport stdio] <name> -- <command> <args...>
            # (codex has no leading flags; claude does -- the name is always
            # whatever immediately precedes the "--" separator, not
            # positionally fixed.)
            rest = argv[2:]
            sep = rest.index("--")
            name = rest[sep - 1]
            command = rest[sep + 1]
            cmd_args = rest[sep + 2:]
            if name == "TRIGGER_HANG":
                time.sleep(5)
                return 0
            if name == "TRIGGER_FAIL":
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


def _write_fake_cli(bin_dir: Path, exe_name: str) -> Path:
    """Write a real, standalone, directly-executable fake CLI named
    `exe_name` into `bin_dir`. On Windows: a `.cmd` shim (proven, in this
    session's own preflight, to run fine under `subprocess.run(...,
    shell=False)` when given its FULL resolved path -- exactly how
    `CodexAdapter`/`ClaudeAdapter` are used, never a bare PATH-relative
    name). On POSIX: a `#!/usr/bin/env python3` script with the execute
    bit set."""
    script_path = bin_dir / f"_{exe_name}_impl.py"
    script_path.write_text(_FAKE_CLI_BODY, encoding="utf-8")

    if sys.platform == "win32":
        exe_path = bin_dir / f"{exe_name}.cmd"
        exe_path.write_text(
            f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n', encoding="utf-8"
        )
        return exe_path

    exe_path = bin_dir / exe_name
    exe_path.write_text(f"#!{sys.executable}\n" + _FAKE_CLI_BODY, encoding="utf-8")
    exe_path.chmod(exe_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return exe_path


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fake_home"
    home.mkdir()
    return home


@pytest.fixture
def fake_codex_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    return _write_fake_cli(bin_dir, "fake-codex")


@pytest.fixture
def fake_claude_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    return _write_fake_cli(bin_dir, "fake-claude")


def _env_for(fake_home: Path, config_path: Path, log_path: Path) -> dict[str, str]:
    # A deliberately MINIMAL, fully isolated environment -- no inherited
    # os.environ, so a bug that ever made the adapter fall back to the
    # real environment/home would show up as a fake CLI crash (missing
    # FAKE_HOME), not a silent write into this developer's real machine.
    env = {
        "FAKE_HOME": str(fake_home),
        "FAKE_CONFIG_PATH": str(config_path),
        "FAKE_LOG": str(log_path),
    }
    if sys.platform == "win32":
        # Needed for the Python interpreter itself (and the .cmd shim) to
        # start at all on Windows.
        for key in ("SYSTEMROOT", "PATH", "PATHEXT", "TEMP", "TMP"):
            if key in os.environ:
                env[key] = os.environ[key]
    return env


def _read_log(log_path: Path) -> list[list[str]]:
    if not log_path.is_file():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------


def test_codex_add_uses_exact_official_syntax(
    fake_codex_bin: Path, fake_home: Path, tmp_path: Path
) -> None:
    log_path = tmp_path / "codex.log"
    config_path = codex_config_path(fake_home)
    env = _env_for(fake_home, config_path, log_path)
    adapter = CodexAdapter(codex_bin=str(fake_codex_bin), codex_home=fake_home, env=env)

    target = adapter.add("myproj-abc12345", sys.executable, ["-m", "belay.cli.main", "run"])

    assert target.client == "codex"
    assert target.name == "myproj-abc12345"
    assert target.after_sha256 is not None
    assert target.before.existed is False

    calls = _read_log(log_path)
    assert calls[-1] == [
        "mcp", "add", "myproj-abc12345", "--", sys.executable,
        "-m", "belay.cli.main", "run",
    ]
    assert config_path.is_file()


def test_codex_get_list_remove(fake_codex_bin: Path, fake_home: Path, tmp_path: Path) -> None:
    log_path = tmp_path / "codex.log"
    config_path = codex_config_path(fake_home)
    env = _env_for(fake_home, config_path, log_path)
    adapter = CodexAdapter(codex_bin=str(fake_codex_bin), codex_home=fake_home, env=env)

    adapter.add("proj-1", "cmd", ["a"])
    get_result = adapter.get("proj-1")
    assert get_result.returncode == 0
    assert "cmd" in get_result.stdout

    list_result = adapter.list()
    assert list_result.returncode == 0
    assert "proj-1" in list_result.stdout

    before_remove = config_path.read_bytes()
    target = adapter.remove("proj-1")
    assert target.after_sha256 is not None
    assert config_path.read_bytes() != before_remove

    missing_get = adapter.get("proj-1")
    assert missing_get.returncode != 0


def test_codex_add_collision_raises_with_official_message(
    fake_codex_bin: Path, fake_home: Path, tmp_path: Path
) -> None:
    log_path = tmp_path / "codex.log"
    config_path = codex_config_path(fake_home)
    env = _env_for(fake_home, config_path, log_path)
    adapter = CodexAdapter(codex_bin=str(fake_codex_bin), codex_home=fake_home, env=env)

    adapter.add("dup", "cmd", [])
    with pytest.raises(ClientRegistrationError, match="already exists"):
        adapter.add("dup", "cmd", [])


def test_codex_nonzero_exit_raises(fake_codex_bin: Path, fake_home: Path, tmp_path: Path) -> None:
    log_path = tmp_path / "codex.log"
    config_path = codex_config_path(fake_home)
    env = _env_for(fake_home, config_path, log_path)
    adapter = CodexAdapter(codex_bin=str(fake_codex_bin), codex_home=fake_home, env=env)

    with pytest.raises(ClientRegistrationError) as excinfo:
        adapter.add("TRIGGER_FAIL", "cmd", [])
    assert excinfo.value.result.returncode == 1


def test_codex_timeout_raises(fake_codex_bin: Path, fake_home: Path, tmp_path: Path) -> None:
    log_path = tmp_path / "codex.log"
    config_path = codex_config_path(fake_home)
    env = _env_for(fake_home, config_path, log_path)
    adapter = CodexAdapter(
        codex_bin=str(fake_codex_bin), codex_home=fake_home, env=env, timeout=0.5
    )

    with pytest.raises(ClientRegistrationError) as excinfo:
        adapter.add("TRIGGER_HANG", "cmd", [])
    assert excinfo.value.result.timed_out is True


def test_codex_missing_executable_raises(fake_home: Path, tmp_path: Path) -> None:
    log_path = tmp_path / "codex.log"
    config_path = codex_config_path(fake_home)
    env = _env_for(fake_home, config_path, log_path)
    missing_bin = str(tmp_path / "does-not-exist-codex")
    adapter = CodexAdapter(codex_bin=missing_bin, codex_home=fake_home, env=env)

    with pytest.raises(ClientRegistrationError) as excinfo:
        adapter.add("proj", "cmd", [])
    assert excinfo.value.result.missing_executable is True


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------


def test_claude_add_uses_exact_official_syntax(
    fake_claude_bin: Path, fake_home: Path, tmp_path: Path
) -> None:
    log_path = tmp_path / "claude.log"
    config_path = claude_user_config_path(fake_home)
    env = _env_for(fake_home, config_path, log_path)
    adapter = ClaudeAdapter(claude_bin=str(fake_claude_bin), claude_home=fake_home, env=env)

    target = adapter.add("myproj-abc12345", sys.executable, ["-m", "belay.cli.main", "run"])
    assert target.client == "claude"
    assert target.after_sha256 is not None

    calls = _read_log(log_path)
    assert calls[-1] == [
        "mcp", "add", "--scope", "user", "--transport", "stdio", "myproj-abc12345",
        "--", sys.executable, "-m", "belay.cli.main", "run",
    ]


def test_claude_get_list_remove(fake_claude_bin: Path, fake_home: Path, tmp_path: Path) -> None:
    log_path = tmp_path / "claude.log"
    config_path = claude_user_config_path(fake_home)
    env = _env_for(fake_home, config_path, log_path)
    adapter = ClaudeAdapter(claude_bin=str(fake_claude_bin), claude_home=fake_home, env=env)

    adapter.add("proj-1", "cmd", ["a"])
    assert adapter.get("proj-1").returncode == 0
    assert "proj-1" in adapter.list().stdout
    adapter.remove("proj-1")
    assert adapter.get("proj-1").returncode != 0


def test_claude_collision_raises(fake_claude_bin: Path, fake_home: Path, tmp_path: Path) -> None:
    log_path = tmp_path / "claude.log"
    config_path = claude_user_config_path(fake_home)
    env = _env_for(fake_home, config_path, log_path)
    adapter = ClaudeAdapter(claude_bin=str(fake_claude_bin), claude_home=fake_home, env=env)

    adapter.add("dup", "cmd", [])
    with pytest.raises(ClientRegistrationError, match="already exists"):
        adapter.add("dup", "cmd", [])


def test_claude_timeout_and_missing_executable(
    fake_claude_bin: Path, fake_home: Path, tmp_path: Path
) -> None:
    log_path = tmp_path / "claude.log"
    config_path = claude_user_config_path(fake_home)
    env = _env_for(fake_home, config_path, log_path)

    hung = ClaudeAdapter(
        claude_bin=str(fake_claude_bin), claude_home=fake_home, env=env, timeout=0.5
    )
    with pytest.raises(ClientRegistrationError) as excinfo:
        hung.add("TRIGGER_HANG", "cmd", [])
    assert excinfo.value.result.timed_out is True

    missing = ClaudeAdapter(
        claude_bin=str(tmp_path / "nope-claude"), claude_home=fake_home, env=env
    )
    with pytest.raises(ClientRegistrationError) as excinfo2:
        missing.add("proj", "cmd", [])
    assert excinfo2.value.result.missing_executable is True


# --------------------------------------------------------------------------
# Claude Desktop fallback
# --------------------------------------------------------------------------


def test_claude_desktop_config_path_by_platform(tmp_path: Path) -> None:
    assert claude_desktop_config_path(tmp_path, platform="darwin") == (
        tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    )
    assert claude_desktop_config_path(tmp_path, platform="win32") == (
        tmp_path / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    )
    assert claude_desktop_config_path(tmp_path, platform="linux") == (
        tmp_path / ".config" / "Claude" / "claude_desktop_config.json"
    )


def test_claude_desktop_fallback_merges_only_its_own_entry(tmp_path: Path) -> None:
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "x", "args": []}}, "unrelated": True}),
        encoding="utf-8",
    )

    target = register_claude_desktop(
        "belay-proj", sys.executable, ["-m", "belay.cli.main", "run"], config_path=config_path
    )
    assert target.client == "claude-desktop"
    doc = json.loads(config_path.read_text(encoding="utf-8"))
    assert doc["unrelated"] is True
    assert doc["mcpServers"]["other"] == {"command": "x", "args": []}
    assert doc["mcpServers"]["belay-proj"]["command"] == sys.executable

    removed = remove_claude_desktop("belay-proj", config_path=config_path)
    assert removed.after_sha256 is not None
    doc2 = json.loads(config_path.read_text(encoding="utf-8"))
    assert "belay-proj" not in doc2["mcpServers"]
    assert doc2["mcpServers"]["other"] == {"command": "x", "args": []}
    assert doc2["unrelated"] is True


def test_claude_desktop_fallback_creates_config_when_absent(tmp_path: Path) -> None:
    config_path = tmp_path / "nested" / "claude_desktop_config.json"
    target = register_claude_desktop("belay-proj", "cmd", [], config_path=config_path)
    assert target.before.existed is False
    assert config_path.is_file()


def test_claude_desktop_remove_on_missing_config_is_a_noop(tmp_path: Path) -> None:
    config_path = tmp_path / "nope.json"
    target = remove_claude_desktop("belay-proj", config_path=config_path)
    assert target.after_sha256 is None
    assert not config_path.exists()
