"""Official-CLI client registration adapters (E22 Task 3).

`belay connect` registers Belay's protected proxy with Codex and Claude
Code through their OWN official `mcp add`/`get`/`list`/`remove` commands
-- never by hand-editing `~/.codex/config.toml` or `~/.claude.json`
ourselves (that reverse-engineered-format approach is what
`belay/cli/client_configs.py`/`belay/cli/main.py`'s pre-E22 `init`/`wrap`
flow does; E22 deliberately does not reuse it for MCP registration, only
for its byte/hash compare-and-swap helpers and the Claude Code project
hooks merge). Using the official CLI means whatever internal format each
tool uses is that tool's own business to get right, not belay's to guess.

Claude Desktop has no CLI at all, so it is the one exception: `register_*`
functions for it merge `mcpServers.<name>` into its JSON config directly
(`render_json_mcp_entry`/`remove_json_mcp_entry` in `client_configs.py`).

Every subprocess call here uses an explicit argument array with
`shell=False` -- confirmed on this project's own Windows dev machine that
passing a *fully resolved* executable path (e.g. what `shutil.which`
returns, `.cmd` extension and all) works fine without a shell; adapters
never search PATH themselves, callers must resolve the binary first (same
division of responsibility as `belay/cli/host_detection.py`).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from belay.cli.client_configs import (
    atomic_write,
    remove_json_mcp_entry,
    render_json_mcp_entry,
)
from belay.cli.connection_models import ClientTarget, FileSnapshot

#: Default timeout for any single official-CLI invocation (`mcp add/get/
#: list/remove`) -- generous enough for a cold CLI start, bounded enough
#: that a hung/misbehaving binary cannot block `belay connect` forever
#: (Task 4/5's preflight and transaction both need a hard ceiling on every
#: external process they spawn).
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class CommandResult:
    """The outcome of one subprocess invocation -- never raises for a
    nonzero exit, a missing executable, or a timeout; callers (adapter
    methods, then `connection.py`) decide what each of those means. This
    keeps "missing executable" and "timeout" ordinary, testable data
    instead of exception-handling branches scattered through callers."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    missing_executable: bool = False


class ClientRegistrationError(Exception):
    """A `codex`/`claude` `mcp <op>` invocation did not succeed. Carries the
    full `CommandResult` so callers can surface the real stdout/stderr
    (e.g. the official CLI's own collision message) rather than a generic
    failure."""

    def __init__(self, client: str, operation: str, result: CommandResult) -> None:
        self.client = client
        self.operation = operation
        self.result = result
        detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
        reason = (
            "timed out"
            if result.timed_out
            else "executable not found"
            if result.missing_executable
            else f"exit code {result.returncode}"
        )
        super().__init__(
            f"{client} mcp {operation} failed ({reason}): {detail} "
            f"[argv={' '.join(result.argv)!r}]"
        )


def _run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | None,
    timeout: float,
) -> CommandResult:
    argv_t = tuple(argv)
    try:
        proc = subprocess.run(
            list(argv_t),
            env=dict(env),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError:
        return CommandResult(
            argv=argv_t, returncode=127, stdout="", stderr="executable not found",
            missing_executable=True,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(
            "utf-8", "replace"
        )
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(
            "utf-8", "replace"
        )
        return CommandResult(
            argv=argv_t, returncode=-1, stdout=stdout, stderr=stderr, timed_out=True
        )
    return CommandResult(
        argv=argv_t, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------


def codex_config_path(codex_home: Path) -> Path:
    """The one file `codex mcp add/remove` is documented to touch --
    `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`, same path
    `belay/cli/main.py::_client_config_path`'s pre-E22 codex branch
    already uses for `--scope user`)."""
    return codex_home / "config.toml"


@dataclass(frozen=True)
class CodexAdapter:
    """Wraps the real `codex mcp <op>` CLI. `codex_bin` must already be a
    resolved path (e.g. `shutil.which("codex")`'s result) -- this adapter
    never searches PATH itself. `env` is the *complete* subprocess
    environment (including any `CODEX_HOME` override); callers isolate
    tests by pointing both `codex_home` and `env["CODEX_HOME"]` at the same
    throwaway directory, never the real user's `~/.codex`."""

    codex_bin: str
    codex_home: Path
    env: Mapping[str, str]
    timeout: float = DEFAULT_TIMEOUT

    @property
    def config_path(self) -> Path:
        return codex_config_path(self.codex_home)

    def add(self, name: str, command: str, args: Sequence[str]) -> ClientTarget:
        before = FileSnapshot.capture(self.config_path)
        argv = [self.codex_bin, "mcp", "add", name, "--", command, *args]
        result = _run(argv, env=self.env, cwd=None, timeout=self.timeout)
        if result.returncode != 0:
            raise ClientRegistrationError("codex", "add", result)
        after = FileSnapshot.capture(self.config_path)
        return ClientTarget(
            client="codex", name=name, config_path=str(self.config_path),
            before=before, after_sha256=after.sha256,
        )

    def get(self, name: str) -> CommandResult:
        return _run(
            [self.codex_bin, "mcp", "get", name], env=self.env, cwd=None, timeout=self.timeout
        )

    def list(self) -> CommandResult:
        return _run([self.codex_bin, "mcp", "list"], env=self.env, cwd=None, timeout=self.timeout)

    def remove(self, name: str) -> ClientTarget:
        before = FileSnapshot.capture(self.config_path)
        result = _run(
            [self.codex_bin, "mcp", "remove", name], env=self.env, cwd=None, timeout=self.timeout
        )
        if result.returncode != 0:
            raise ClientRegistrationError("codex", "remove", result)
        after = FileSnapshot.capture(self.config_path)
        return ClientTarget(
            client="codex", name=name, config_path=str(self.config_path),
            before=before, after_sha256=after.sha256,
        )


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------


def claude_user_config_path(claude_home: Path) -> Path:
    """The file `claude mcp add --scope user` is documented to touch:
    `~/.claude.json`'s top-level `mcpServers` object."""
    return claude_home / ".claude.json"


@dataclass(frozen=True)
class ClaudeAdapter:
    """Wraps the real `claude mcp <op>` CLI, always `--scope user
    --transport stdio` (E22's connect flow only ever registers Belay at
    user scope -- project scope would mean the *proxy's own registration*,
    not just the underlying hooks, lives in a project file another agent
    session could edit). Same `codex_bin`-resolution and env-isolation
    contract as `CodexAdapter`."""

    claude_bin: str
    claude_home: Path
    env: Mapping[str, str]
    timeout: float = DEFAULT_TIMEOUT

    @property
    def config_path(self) -> Path:
        return claude_user_config_path(self.claude_home)

    def add(self, name: str, command: str, args: Sequence[str]) -> ClientTarget:
        before = FileSnapshot.capture(self.config_path)
        argv = [
            self.claude_bin, "mcp", "add", "--scope", "user", "--transport", "stdio",
            name, "--", command, *args,
        ]
        result = _run(argv, env=self.env, cwd=None, timeout=self.timeout)
        if result.returncode != 0:
            raise ClientRegistrationError("claude", "add", result)
        after = FileSnapshot.capture(self.config_path)
        return ClientTarget(
            client="claude", name=name, config_path=str(self.config_path),
            before=before, after_sha256=after.sha256,
        )

    def get(self, name: str) -> CommandResult:
        return _run(
            [self.claude_bin, "mcp", "get", name], env=self.env, cwd=None, timeout=self.timeout
        )

    def list(self) -> CommandResult:
        return _run(
            [self.claude_bin, "mcp", "list"], env=self.env, cwd=None, timeout=self.timeout
        )

    def remove(self, name: str) -> ClientTarget:
        before = FileSnapshot.capture(self.config_path)
        result = _run(
            [self.claude_bin, "mcp", "remove", name], env=self.env, cwd=None,
            timeout=self.timeout,
        )
        if result.returncode != 0:
            raise ClientRegistrationError("claude", "remove", result)
        after = FileSnapshot.capture(self.config_path)
        return ClientTarget(
            client="claude", name=name, config_path=str(self.config_path),
            before=before, after_sha256=after.sha256,
        )


# --------------------------------------------------------------------------
# Claude Desktop (no CLI -- direct, surgical JSON merge fallback)
# --------------------------------------------------------------------------


def claude_desktop_config_path(home_dir: Path, *, platform: str = sys.platform) -> Path:
    if platform == "darwin":
        return (
            home_dir / "Library" / "Application Support" / "Claude"
            / "claude_desktop_config.json"
        )
    if platform == "win32":
        return home_dir / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    return home_dir / ".config" / "Claude" / "claude_desktop_config.json"


def register_claude_desktop(
    name: str, command: str, args: Sequence[str], *, config_path: Path
) -> ClientTarget:
    """Merge `mcpServers.<name>` into Claude Desktop's config, preserving
    every other key/entry byte-for-byte via `tomlkit`-equivalent surgical
    JSON editing (`render_json_mcp_entry`) -- never a wholesale rewrite."""
    before = FileSnapshot.capture(config_path)
    before_bytes = before.raw_bytes()
    existing = before_bytes.decode("utf-8") if before_bytes is not None else ""
    new_text = render_json_mcp_entry(existing, name, command, list(args))
    atomic_write(config_path, new_text)
    after = FileSnapshot.capture(config_path)
    return ClientTarget(
        client="claude-desktop", name=name, config_path=str(config_path),
        before=before, after_sha256=after.sha256,
    )


def remove_claude_desktop(name: str, *, config_path: Path) -> ClientTarget:
    before = FileSnapshot.capture(config_path)
    if not before.existed:
        return ClientTarget(
            client="claude-desktop", name=name, config_path=str(config_path),
            before=before, after_sha256=None,
        )
    before_bytes = before.raw_bytes()
    existing = before_bytes.decode("utf-8") if before_bytes is not None else ""
    new_text = remove_json_mcp_entry(existing, name)
    atomic_write(config_path, new_text)
    after = FileSnapshot.capture(config_path)
    return ClientTarget(
        client="claude-desktop", name=name, config_path=str(config_path),
        before=before, after_sha256=after.sha256,
    )
