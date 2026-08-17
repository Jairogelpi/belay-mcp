#!/usr/bin/env python3
"""E22 Task 8: standalone, isolated smoke driver for `belay connect`/
`disconnect` against a real, wheel-installed `belay` -- proves the exact
zero-config, one-command experience end to end, without ever touching a
real user's `~/.codex`, `~/.claude.json`, or Claude Desktop config.

Self-contained on purpose: CI's `wheel-smoke` job installs ONLY the built
wheel into a throwaway venv (no dev extras, no pytest, no repo checkout
importable as a package) -- this script has no dependency beyond what the
wheel itself ships (`belay`, `mcp`, `anyio`, `typer`, ...), so it can run
unmodified in that environment. `tests/tools/test_smoke_connect.py` drives
the same functions from inside the normal dev test suite.

What it actually proves, in order:

1. `belay connect` (against a FAKE `codex` CLI on PATH, isolated home)
   succeeds, using only the pinned Filesystem pack the wheel bundles.
2. A second `belay connect` is a healthy no-op (idempotent).
3. The fake CLI's OWN recorded registration -- read back through its
   `mcp get`, never assumed -- is spawned directly and completes a real
   MCP `initialize`/`list_tools` THROUGH Belay (never by connecting to the
   Filesystem upstream directly): proves the registered command is
   correct, not just that `connect` claimed success.
   E23 Task 3: with `--expect-frozen` (CI's `build-binaries` matrix jobs,
   against a real PyInstaller `dist-bin/belay(.exe)`), that recorded
   command is additionally asserted to be the absolute binary itself
   plus `run --config ...` -- never a `python`/`py` interpreter, never
   `belay.cli.main` spelled out as a module argument.
4. Through that same Belay-proxied session: an inside-the-project path
   works, an outside path is confined/rejected.
5. `belay disconnect` removes only Belay's own entry -- a pre-existing,
   unrelated entry in the same config file survives untouched.

Any of these failing (including the pinned pack being missing from the
wheel, wrong official-CLI argv, a wrong recorded launch command, failure
to list tools through Belay, failed confinement, or -- with
`--expect-frozen` -- a frozen binary that quietly registered a
python-dependent launch command) exits nonzero with a clear message
identifying which stage failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class SmokeFailure(RuntimeError):
    """One clearly-named smoke stage failed."""


# --------------------------------------------------------------------------
# Fake codex/claude CLI (standalone copy of tests/cli/conftest.py's fake --
# duplicated, not imported, because this script must run with NO test
# dependencies in a bare wheel-only venv).
# --------------------------------------------------------------------------

_FAKE_CLI_BODY = textwrap.dedent(
    """
    def main() -> int:
        argv = sys.argv[1:]
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(argv) + "\\n")
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
            rest = argv[2:]
            sep = rest.index("--")
            name = rest[sep - 1]
            command = rest[sep + 1]
            cmd_args = rest[sep + 2:]
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


def write_fake_cli(
    bin_dir: Path, exe_name: str, *, fake_home: Path, config_path: Path, log_path: Path
) -> Path:
    prefix = (
        "import json\nimport sys\nfrom pathlib import Path\n\n"
        f"FAKE_HOME = Path({json.dumps(str(fake_home))})\n"
        f"CONFIG_PATH = Path({json.dumps(str(config_path))})\n"
        f"LOG_PATH = Path({json.dumps(str(log_path))})\n\n"
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


def run_fake_cli(exe: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(exe), *args], capture_output=True, text=True, timeout=30, shell=False
    )


# --------------------------------------------------------------------------
# E23 Task 3: a fake FROZEN `belay` -- exercises the exact registration
# decision `belay/cli/connection.py::belay_launch_argv` makes for a real
# PyInstaller `dist-bin/belay(.exe)`, without an actual multi-minute
# PyInstaller build in the fast dev suite.
#
# Deliberately does NOT set the real `sys.frozen`/`sys.executable`
# attributes `belay_launch_argv` itself reads -- confirmed the hard way
# on Windows: `sys.frozen = True` inside an ordinary (non-frozen)
# interpreter also flips `pywin32`'s own frozen-app import-path detection,
# which then fails to locate `pywintypes` and crashes belay's own
# `mcp`/`anyio` import chain -- a real PyInstaller binary doesn't hit
# this because ITS bundled pywin32 (if any) is laid out for exactly that
# frozen case; a plain script pretending to be frozen isn't. Instead this
# shim monkeypatches `belay_launch_argv` itself (a plain, undecorated
# module-level function `belay/cli/connection.py`'s `connect` command
# calls by name at call time) to return frozen-shaped output directly --
# same observable effect (the registered command IS this shim's own
# absolute path plus `run --config ...`), none of the real interpreter
# state that confuses an unrelated library. The genuinely frozen
# `sys.frozen`/`sys.executable` code path itself is only exercised for
# real by CI's `build-binaries` matrix jobs, against the actual binary.
# --------------------------------------------------------------------------

_FAKE_FROZEN_BELAY_BODY = textwrap.dedent(
    """
    import sys
    # This shim's own script lives outside the repo (a throwaway tmp bin
    # dir), so plain `python this_script.py` puts the SCRIPT's directory
    # -- not the repo -- at sys.path[0]. Left alone, that can silently
    # resolve `import belay` to an unrelated `belay` on the rest of
    # sys.path (e.g. a stale editable install pointing at a different
    # checkout) instead of this exact repo's own code -- confirmed the
    # hard way: `connect`/`disconnect` quietly vanished from the CLI when
    # that happened. Forcing the real repo root to the very front of
    # sys.path makes this shim behave like `python -m belay.cli.main` run
    # from the repo root (which puts cwd, not a script dir, at
    # sys.path[0]) -- the same resolution smoke_connect.py's own dev-mode
    # invocation already relies on.
    sys.path.insert(0, REPO_ROOT)

    import belay.cli.connection as _connection

    def _frozen_launch_argv(wrap_path):
        return (SELF_PATH, "run", "--config", str(wrap_path))

    _connection.belay_launch_argv = _frozen_launch_argv

    from belay.cli.main import main
    sys.exit(main())
    """
)


def write_fake_frozen_belay(bin_dir: Path) -> Path:
    """Real, directly-executable -- CI's `build-binaries` matrix jobs
    (E23 Task 3 Step 3) instead point `--expect-frozen` smoke runs at the
    genuine `dist-bin/belay(.exe)` PyInstaller output; this shim exists so
    the SAME `assert_frozen_launch_command` check has fast, real coverage
    without needing that build here."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe_name = "fake-frozen-belay"
    exe_path = bin_dir / (f"{exe_name}.cmd" if sys.platform == "win32" else exe_name)
    script_path = bin_dir / f"_{exe_name}_impl.py"
    body = (
        f"SELF_PATH = {json.dumps(str(exe_path))}\n"
        f"REPO_ROOT = {json.dumps(str(REPO_ROOT))}\n"
    ) + _FAKE_FROZEN_BELAY_BODY
    script_path.write_text(body, encoding="utf-8")

    if sys.platform == "win32":
        exe_path.write_text(
            f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n', encoding="utf-8"
        )
        return exe_path

    exe_path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    exe_path.chmod(exe_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return exe_path


def assert_frozen_launch_command(recorded_argv: list[str]) -> None:
    """E23 Task 3: a frozen `belay`/`belay.exe` binary must register
    itself directly -- the absolute path to the binary itself, then
    `run --config ...` -- never delegate through a `python`/`py`
    interpreter, and never spell out `belay.cli.main` as a module
    argument. Either of those would mean the compiled, no-Python-required
    binary quietly needs Python installed after all to actually run,
    defeating the entire point of shipping one (plan-v2 E19.5)."""
    if not recorded_argv:
        raise SmokeFailure("frozen belay's recorded registration has an empty command")
    command = recorded_argv[0]
    stem = Path(command).stem.lower()
    if stem in ("python", "python3", "py"):
        raise SmokeFailure(
            f"frozen belay registered a python interpreter as its launch command "
            f"({command!r}, argv={recorded_argv!r}) -- expected the absolute belay/belay.exe "
            f"binary itself"
        )
    if "belay.cli.main" in recorded_argv:
        raise SmokeFailure(
            f"frozen belay's registration still spells out the 'belay.cli.main' module "
            f"invocation: {recorded_argv!r}"
        )
    if "run" not in recorded_argv or "--config" not in recorded_argv:
        raise SmokeFailure(
            f"frozen belay's registration is missing 'run'/'--config': {recorded_argv!r}"
        )


# --------------------------------------------------------------------------
# The smoke driver itself
# --------------------------------------------------------------------------


def _isolated_env(*, home: Path, bin_dir: Path) -> dict[str, str]:
    """`belay connect`'s real CLI has no `--codex-home`/`--claude-home`
    flags (those are Python-API-only isolation knobs, see `belay/cli/
    connection.py::_Isolation`) -- the only way to isolate a REAL `belay`
    subprocess invocation from this machine's real `~/.codex`/
    `~/.claude.json`/Claude Desktop config is to override the OS-level
    home env vars the whole process resolves `Path.home()` from.
    Deliberately does NOT touch `APPDATA`/`LOCALAPPDATA` directly --
    `belay/cli/client_registration.py::claude_desktop_config_path` builds
    that path itself by joining `AppData/Roaming` onto `Path.home()`, so
    only `HOME`/`USERPROFILE` need overriding; overriding `APPDATA` too
    would (confirmed the hard way) also relocate this *interpreter's own*
    user-site-packages lookup on Windows, breaking `import typer` in the
    child before it even gets to `belay`'s own code."""
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    # Never let this process's own real belay state (BELAY_HOME) leak in.
    env["BELAY_HOME"] = str(home / ".belay-home")
    return env


async def _list_tools_through_belay(argv: list[str]) -> list[str]:
    from belay.proxy.upstream import connect_stdio

    async with connect_stdio(argv[0], argv[1:]) as client:
        tools = await client.list_tools()
    return [t.name for t in tools]


async def _call_tool_through_belay(argv: list[str], tool: str, args: dict[str, object]) -> bool:
    """Returns True if the call succeeded (no MCP-level error)."""
    from belay.proxy.upstream import connect_stdio

    async with connect_stdio(argv[0], argv[1:]) as client:
        result = await client.call_tool(tool, args)
    return result.isError is not True


def run_smoke(
    *, belay_cmd: list[str], bin_dir: Path, home: Path, project: Path, expect_frozen: bool = False
) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)
    outside = project.parent / f"{project.name}-outside-confinement-check"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "secret.txt").write_text("do not read me", encoding="utf-8")

    from belay.cli.client_registration import codex_config_path
    from belay.cli.connection_models import default_project_name

    # The real `belay connect` subprocess below has no `--codex-home` flag
    # (that's a Python-API-only isolation knob) -- with HOME/USERPROFILE
    # overridden to `home` (see `_isolated_env`), it independently computes
    # its own expected codex config path as `codex_config_path(Path.home())`
    # == `codex_config_path(home)` directly. The fake CLI's own CONFIG_PATH
    # below must match that exactly, or belay's own before/after byte
    # snapshots silently point at a file the fake CLI never actually wrote.
    codex_config = codex_config_path(home)
    log_path = home / "cli.log"
    codex_bin = write_fake_cli(
        bin_dir, "codex", fake_home=home / "codex_fake_home", config_path=codex_config,
        log_path=log_path,
    )

    # A pre-existing, unrelated MCP server -- must survive `belay connect`
    # AND `belay disconnect` (the fake CLI's own state/config file is its
    # own business, like a real client's config writer -- belay must only
    # ever touch the one entry it owns).
    unrelated_result = run_fake_cli(codex_bin, ["mcp", "add", "unrelated-server", "--", "cmd"])
    if unrelated_result.returncode != 0:
        raise SmokeFailure(f"setup: could not seed an unrelated codex entry: {unrelated_result}")

    env = _isolated_env(home=home, bin_dir=bin_dir)
    name = default_project_name(project)

    def _belay(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*belay_cmd, *args], env=env, capture_output=True, text=True, timeout=240,
            shell=False,
        )

    first = _belay("connect", "--project", str(project), "--client", "codex")
    if first.returncode != 0:
        raise SmokeFailure(
            f"belay connect (first) failed rc={first.returncode}\n"
            f"stdout={first.stdout}\nstderr={first.stderr}"
        )

    second = _belay("connect", "--project", str(project), "--client", "codex")
    if second.returncode != 0:
        raise SmokeFailure(
            f"belay connect (second, idempotency check) failed rc={second.returncode}\n"
            f"stdout={second.stdout}\nstderr={second.stderr}"
        )

    # The exact command Codex's OWN fake CLI recorded -- never assumed.
    get_result = run_fake_cli(codex_bin, ["mcp", "get", name])
    if get_result.returncode != 0:
        raise SmokeFailure(f"codex mcp get '{name}' failed: {get_result}")
    try:
        payload = json.loads(get_result.stdout)
        recorded_argv = [str(payload["command"]), *[str(a) for a in payload["args"]]]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SmokeFailure(f"could not parse codex's recorded registration: {exc}") from exc

    if expect_frozen:
        assert_frozen_launch_command(recorded_argv)

    tool_names = asyncio.run(_list_tools_through_belay(recorded_argv))
    if "write_file" not in tool_names or "read_file" not in tool_names:
        raise SmokeFailure(
            f"belay (via codex's own recorded launch command) did not advertise the pinned "
            f"Filesystem upstream's tools -- got {sorted(tool_names)}"
        )

    inside_ok = asyncio.run(
        _call_tool_through_belay(
            recorded_argv, "write_file",
            {"path": str(project / "smoke.txt"), "content": "hello from smoke_connect"},
        )
    )
    if not inside_ok or not (project / "smoke.txt").is_file():
        raise SmokeFailure("write inside the connected project directory failed through Belay")

    outside_ok = asyncio.run(
        _call_tool_through_belay(
            recorded_argv, "read_file", {"path": str(outside / "secret.txt")}
        )
    )
    if outside_ok:
        raise SmokeFailure(
            "directory confinement failed -- Belay's proxy allowed reading a path outside "
            "the connected project directory"
        )

    disconnect_result = _belay("disconnect", "--project", str(project))
    if disconnect_result.returncode != 0:
        raise SmokeFailure(
            f"belay disconnect failed rc={disconnect_result.returncode}\n"
            f"stdout={disconnect_result.stdout}\nstderr={disconnect_result.stderr}"
        )

    final_doc = json.loads(codex_config.read_text(encoding="utf-8"))
    if name in final_doc:
        raise SmokeFailure(f"belay disconnect did not remove its own entry '{name}'")
    if "unrelated-server" not in final_doc:
        raise SmokeFailure("belay disconnect removed an unrelated MCP server entry -- must not")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--belay", required=True,
        help="The belay command prefix to invoke, shlex-split, e.g. --belay belay, or "
        '--belay "python -m belay.cli.main".',
    )
    parser.add_argument("--bin-dir", required=True, help="Directory to place the fake codex CLI.")
    parser.add_argument("--home", required=True, help="Isolated HOME/USERPROFILE root.")
    parser.add_argument("--project", required=True, help="Project directory to connect.")
    parser.add_argument(
        "--expect-frozen", action="store_true",
        help="Require the recorded registration to be a bare, absolute belay/belay.exe launch "
        "(no python/py/belay.cli.main) -- E23 Task 3, use against a real PyInstaller build.",
    )
    args = parser.parse_args(argv)

    # posix=False on Windows: POSIX-mode shlex treats backslash as an escape
    # character, silently mangling a literal Windows path like
    # `C:\Python313\python.exe` into `C:Python313python.exe` -- confirmed
    # the hard way running this script's own tests on Windows.
    belay_cmd = shlex.split(args.belay, posix=(sys.platform != "win32"))

    try:
        run_smoke(
            belay_cmd=belay_cmd, bin_dir=Path(args.bin_dir), home=Path(args.home),
            project=Path(args.project), expect_frozen=args.expect_frozen,
        )
    except SmokeFailure as exc:
        print(f"smoke_connect: FAILED -- {exc}", file=sys.stderr)
        return 1

    print("smoke_connect: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
