"""Preflight, transaction, rollback, verification, and disconnect
orchestration for `belay connect`/`belay disconnect` (E22 Tasks 4-6).

Task 4's slice: generate the exact protected proxy runtime a `belay
connect` will register (`.belay/belay.wrap.json`, using the bundled,
pinned Filesystem pack from `belay/bundled_packs.py`), and independently
preflight it -- spawn the *exact* argv that will be handed to Codex/Claude,
speak real MCP `initialize`/`list_tools` through it (not just call Python
functions directly), and confirm the advertised tools really did come from
the pinned upstream -- before any client registration is ever attempted.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import anyio
from mcp.types import Tool

from belay.bundled_packs import filesystem_pack, stable_contracts_path
from belay.cli.client_configs import atomic_write, render_claude_hooks_settings
from belay.cli.client_registration import (
    ClaudeAdapter,
    ClientRegistrationError,
    CodexAdapter,
    claude_desktop_config_path,
    register_claude_desktop,
    remove_claude_desktop,
)
from belay.cli.connection_models import (
    ClientTarget,
    ConnectionInspection,
    ConnectionManifest,
    FileSnapshot,
    RuntimeInfo,
    TargetInspection,
    TargetState,
    classify_target_state,
    default_project_name,
    validate_name,
)
from belay.proxy.config import UpstreamCommand, WrapConfig
from belay.proxy.upstream import connect_stdio

#: Default ceiling on the real MCP `initialize`/`list_tools` preflight --
#: generous for a cold `npx` download-and-start, bounded so a hung/broken
#: upstream can never hang `belay connect` forever.
DEFAULT_PREFLIGHT_TIMEOUT = 120.0

#: The exact tool names the pinned Filesystem pack's contracts describe --
#: used to assert the preflight's advertised tools really came from that
#: upstream, not some other stdio server the launch argv happened to start.
EXPECTED_FILESYSTEM_TOOLS = frozenset(
    {
        "read_file",
        "read_text_file",
        "read_media_file",
        "read_multiple_files",
        "list_directory",
        "list_directory_with_sizes",
        "directory_tree",
        "search_files",
        "get_file_info",
        "list_allowed_directories",
        "write_file",
        "edit_file",
        "move_file",
        "create_directory",
    }
)


class ConnectionError_(Exception):
    """Base for every typed E22 connection error. Named with a trailing
    underscore to avoid shadowing the builtin `ConnectionError` -- callers
    (`belay/cli/main.py`'s thin command wrappers, Task 7) catch this to
    translate any subclass into a clean CLI exit 1."""


class DependencyError(ConnectionError_):
    """A required external dependency (e.g. `npx`/Node.js) is missing."""


class PreflightError(ConnectionError_):
    """The generated runtime failed to pass its own real-MCP preflight --
    Belay never registers a client with an upstream it hasn't itself
    proven works."""


class CollisionError(ConnectionError_):
    """A client already has an entry under the name `belay connect` was
    about to register/re-register, and Belay does not own it (no manifest,
    or the manifest doesn't already track it as healthy)."""


class RollbackIncompleteError(ConnectionError_):
    """A transaction failed AND its own rollback could not fully restore
    every target -- at least one config changed out from under Belay
    between its post-write snapshot and the rollback attempt (a real,
    concurrent external edit, not a bug in Belay's own bookkeeping). The
    manifest is left at `rollback_incomplete`; only `disconnect` (after a
    human reconciles the conflicting file by hand) can move it forward."""

    def __init__(self, manifest: ConnectionManifest, conflicts: list[str] | None = None) -> None:
        self.manifest = manifest
        self.conflicts = conflicts or []
        detail = f" -- conflicting targets: {self.conflicts}" if self.conflicts else ""
        super().__init__(
            f"connection '{manifest.name}' is in rollback_incomplete state{detail}; "
            "run `belay disconnect` to reconcile before connecting again"
        )


class TransactionFailedError(ConnectionError_):
    """The transaction failed and was fully rolled back (every target
    restored to its exact prior bytes/absence) -- the original failure is
    preserved as `__cause__` and restated in this message."""


# --------------------------------------------------------------------------
# The exact launch command Codex/Claude will be told to run
# --------------------------------------------------------------------------


def belay_launch_argv(wrap_path: Path) -> tuple[str, ...]:
    """The exact command E22 registers with every client, and the exact
    command Task 4's preflight independently spawns before any
    registration happens -- so what gets preflighted and what gets
    registered can never quietly diverge.

    A real PyInstaller-frozen `belay` binary (`sys.frozen`) IS the
    interpreter+entrypoint in one file, so its own argv drops the
    `-m belay.cli.main` module invocation an ordinary `python` launch
    needs.
    """
    if getattr(sys, "frozen", False):
        return (sys.executable, "run", "--config", str(wrap_path))
    return (sys.executable, "-m", "belay.cli.main", "run", "--config", str(wrap_path))


# --------------------------------------------------------------------------
# Runtime generation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposedRuntime:
    """The complete, not-yet-written desired state of a project's protected
    proxy runtime -- computed before touching disk so preflight/dependency
    checks can run against it first (spec-first, no partial writes)."""

    project_dir: Path
    wrap_path: Path
    db_path: Path
    contracts_path: Path
    upstream_argv: tuple[str, ...]
    launch_argv: tuple[str, ...]

    def to_runtime_info(self) -> RuntimeInfo:
        return RuntimeInfo(
            wrap_path=str(self.wrap_path),
            db_path=str(self.db_path),
            contracts_path=str(self.contracts_path),
            upstream_argv=self.upstream_argv,
        )


def build_proposed_runtime(project_dir: Path) -> ProposedRuntime:
    """Compute (never write) the desired runtime for `project_dir`. Uses the
    bundled, pinned Filesystem pack's own contracts file directly for an
    ordinary install -- a real, permanent path, stable across process
    launches. A frozen `belay.exe` gets `stable_contracts_path`'s
    `belay_home()`-anchored copy instead: the bundled resource's own path
    lives under that *specific* process's ephemeral `sys._MEIPASS` and is
    gone by the time a later `belay run` process (registered by this same
    call, but launched separately) would try to read it. Uses the
    *canonicalized* (`.resolve()`d) project directory as both the
    upstream's allowed-directory argument and the identity this runtime is
    forever tied to."""
    canonical_dir = project_dir.resolve()
    belay_dir = canonical_dir / ".belay"
    wrap_path = belay_dir / "belay.wrap.json"
    db_path = belay_dir / "belay.db"

    pack = filesystem_pack()
    upstream_argv = pack.upstream_launch_argv(canonical_dir)
    launch_argv = belay_launch_argv(wrap_path)

    return ProposedRuntime(
        project_dir=canonical_dir,
        wrap_path=wrap_path,
        db_path=db_path,
        contracts_path=stable_contracts_path(pack),
        upstream_argv=upstream_argv,
        launch_argv=launch_argv,
    )


def write_runtime(runtime: ProposedRuntime, *, initiated_by: str = "belay-connect") -> None:
    """Write `.belay/belay.wrap.json` for `runtime`. Does NOT create the
    ledger database file itself -- `belay run`'s own `LedgerStore` creates
    it (and its schema) lazily on first real use, same as every other
    belay entrypoint."""
    runtime.wrap_path.parent.mkdir(parents=True, exist_ok=True)
    config = WrapConfig(
        upstream=UpstreamCommand(
            command=runtime.upstream_argv[0], args=list(runtime.upstream_argv[1:])
        ),
        contracts=[str(runtime.contracts_path)],
        db=str(runtime.db_path),
        initiated_by=initiated_by,
    )
    config.save(runtime.wrap_path)


# --------------------------------------------------------------------------
# Real MCP-through-Belay preflight
# --------------------------------------------------------------------------


async def preflight_runtime(
    runtime: ProposedRuntime, *, timeout: float = DEFAULT_PREFLIGHT_TIMEOUT
) -> list[Tool]:
    """Independently spawn the EXACT argv that will be registered with
    Codex/Claude (`runtime.launch_argv` -- `<python> -m belay.cli.main run
    --config ...`, or the frozen binary form), complete a real MCP
    `initialize` + `list_tools` through it (i.e. through Belay's own proxy,
    which itself launches the pinned upstream), and confirm the advertised
    tools genuinely came from the pinned Filesystem upstream -- never
    connecting to that upstream directly, since the whole point is proving
    the *registered* command actually works end-to-end.

    Raises `PreflightError` (never a bare SDK/timeout exception) on any
    failure -- `belay connect`'s transaction (Task 5) must never register a
    client with a runtime that didn't pass this.
    """
    command, *args = runtime.launch_argv
    try:
        with anyio.fail_after(timeout):
            async with connect_stdio(command, args) as client:
                tools = await client.list_tools()
    except TimeoutError as exc:
        raise PreflightError(
            f"belay run preflight timed out after {timeout}s (argv={runtime.launch_argv!r})"
        ) from exc
    except Exception as exc:
        raise PreflightError(
            f"belay run preflight failed (argv={runtime.launch_argv!r}): {exc}"
        ) from exc

    tool_names = {t.name for t in tools}
    if not tool_names:
        raise PreflightError("belay run preflight succeeded but advertised zero tools")
    unexpected = tool_names - EXPECTED_FILESYSTEM_TOOLS
    if unexpected:
        raise PreflightError(
            f"belay run preflight advertised unexpected tools not from the pinned Filesystem "
            f"upstream: {sorted(unexpected)}"
        )
    return tools


# --------------------------------------------------------------------------
# Isolation / environment resolution (shared by connect/disconnect/inspect)
# --------------------------------------------------------------------------

_ALL_CLIENTS = frozenset({"codex", "claude", "claude-desktop"})

_HOOKS_EVENTS = ("PreToolUse", "PostToolUse")


def _hooks_command_for(db_anchor: str, event: str) -> str:
    """Mirrors `belay/cli/main.py::_hooks_command_for` exactly (same event
    argument, same `--db` anchor convention) -- E22's project-scoped hook
    install is a new *install site* (`<project>/.claude/settings.json`
    instead of a user-chosen scope), not a new hook mechanism."""
    return f'"{sys.executable}" -m belay.cli.main hooks run {event} --db "{db_anchor}"'


@dataclass(frozen=True)
class _Isolation:
    """Every knob a caller (a real CLI invocation, or a test) can use to
    isolate `connect`/`disconnect`/`inspect_connection` from this machine's
    real `~/.codex`, real `~/.claude.json`, real Claude Desktop config, and
    real environment. Production defaults (all `None`/unset) resolve to
    the real, unisolated locations -- exactly once, here, not scattered."""

    clients: frozenset[str] = _ALL_CLIENTS
    codex_bin: str | None = None
    claude_bin: str | None = None
    codex_home: Path | None = None
    claude_home: Path | None = None
    claude_desktop_home: Path | None = None
    project_hooks_path: Path | None = None
    env: Mapping[str, str] | None = None
    preflight_timeout: float = DEFAULT_PREFLIGHT_TIMEOUT
    registration_timeout: float = 30.0

    def resolved_env(self) -> dict[str, str]:
        return dict(self.env) if self.env is not None else dict(os.environ)

    def resolved_codex_home(self) -> Path:
        return self.codex_home if self.codex_home is not None else Path.home()

    def resolved_claude_home(self) -> Path:
        return self.claude_home if self.claude_home is not None else Path.home()

    def resolved_claude_desktop_home(self) -> Path:
        return self.claude_desktop_home if self.claude_desktop_home is not None else Path.home()

    def resolved_codex_bin(self) -> str | None:
        return self.codex_bin if self.codex_bin is not None else shutil.which("codex")

    def resolved_claude_bin(self) -> str | None:
        return self.claude_bin if self.claude_bin is not None else shutil.which("claude")

    def resolved_hooks_path(self, project_dir: Path) -> Path:
        return (
            self.project_hooks_path
            if self.project_hooks_path is not None
            else project_dir / ".claude" / "settings.json"
        )

    def codex_adapter(self, codex_bin: str) -> CodexAdapter:
        return CodexAdapter(
            codex_bin=codex_bin, codex_home=self.resolved_codex_home(),
            env=self.resolved_env(), timeout=self.registration_timeout,
        )

    def claude_adapter(self, claude_bin: str) -> ClaudeAdapter:
        return ClaudeAdapter(
            claude_bin=claude_bin, claude_home=self.resolved_claude_home(),
            env=self.resolved_env(), timeout=self.registration_timeout,
        )


def _detect_active_clients(iso: _Isolation) -> tuple[dict[str, bool], str | None, str | None]:
    codex_bin = iso.resolved_codex_bin()
    claude_bin = iso.resolved_claude_bin()
    desktop_path = claude_desktop_config_path(iso.resolved_claude_desktop_home())
    available = {
        "codex": codex_bin is not None,
        "claude": claude_bin is not None,
        "claude-desktop": desktop_path.parent.is_dir(),
    }
    return available, codex_bin, claude_bin


def _config_path_for(client: str, *, codex_bin: str | None, claude_bin: str | None,
                      iso: _Isolation) -> Path:
    if client == "codex":
        return iso.codex_adapter(codex_bin or "").config_path
    if client == "claude":
        return iso.claude_adapter(claude_bin or "").config_path
    if client == "claude-desktop":
        return claude_desktop_config_path(iso.resolved_claude_desktop_home())
    raise AssertionError(f"unknown client kind: {client!r}")


def _atomic_write_bytes(path: Path, raw: bytes) -> None:
    """Byte-exact atomic write (temp file + `os.replace`) -- unlike
    `client_configs.atomic_write` (text, always UTF-8-encoded), this never
    re-encodes: rollback must restore the *literal original bytes* a
    `FileSnapshot` captured, not a text round-trip of them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _restore_target(target: ClientTarget) -> bool:
    """Compare-and-swap restore: only touches `target.config_path` if its
    current bytes still match what we ourselves wrote (`after_sha256`).
    Returns `False` (a conflict -- never overwrites) if something else
    changed the file since; `True` if restored (or nothing needed
    restoring)."""
    path = Path(target.config_path)
    current = FileSnapshot.capture(path)
    if target.after_sha256 is not None and current.sha256 != target.after_sha256:
        return False
    if not target.before.existed:
        if path.is_file():
            path.unlink()
        return True
    raw = target.before.raw_bytes()
    assert raw is not None
    _atomic_write_bytes(path, raw)
    return True


def _register_target(
    client: str, name: str, command: str, args: list[str], *,
    codex_bin: str | None, claude_bin: str | None, iso: _Isolation,
) -> ClientTarget:
    if client == "codex":
        assert codex_bin is not None
        return iso.codex_adapter(codex_bin).add(name, command, args)
    if client == "claude":
        assert claude_bin is not None
        return iso.claude_adapter(claude_bin).add(name, command, args)
    if client == "claude-desktop":
        path = claude_desktop_config_path(iso.resolved_claude_desktop_home())
        return register_claude_desktop(name, command, args, config_path=path)
    raise AssertionError(f"unknown client kind: {client!r}")


def _get_target(
    client: str, name: str, *, codex_bin: str | None, claude_bin: str | None, iso: _Isolation,
) -> tuple[bool, str, list[str]] | None:
    """Best-effort read-back of a client's own record for `name`: `(present,
    command, args)` for codex/claude (via `mcp get`), or `None` for
    claude-desktop (no CLI -- callers fall back to reading its JSON
    directly). `command`/`args` are empty when not present or the
    official CLI's own output isn't the JSON shape our own adapters/fakes
    use -- callers must be prepared to fall back to the desired argv they
    already know in that case (a real, non-fake `codex`/`claude`'s `mcp
    get` text format has not been verified against this code)."""
    if client == "codex":
        assert codex_bin is not None
        result = iso.codex_adapter(codex_bin).get(name)
    elif client == "claude":
        assert claude_bin is not None
        result = iso.claude_adapter(claude_bin).get(name)
    else:
        return None
    if result.returncode != 0:
        return False, "", []
    try:
        payload = json.loads(result.stdout)
        return True, str(payload["command"]), [str(a) for a in payload["args"]]
    except (json.JSONDecodeError, KeyError, TypeError, IndexError):
        return True, "", []


def _install_project_hooks(
    project_dir: Path, runtime: ProposedRuntime, name: str, iso: _Isolation
) -> ClientTarget:
    hooks_path = iso.resolved_hooks_path(project_dir)
    before = FileSnapshot.capture(hooks_path)
    raw = before.raw_bytes()
    text = raw.decode("utf-8") if raw is not None else ""
    for event in _HOOKS_EVENTS:
        text = render_claude_hooks_settings(
            text, _hooks_command_for(str(runtime.db_path), event), event=event
        )
    atomic_write(hooks_path, text)
    after = FileSnapshot.capture(hooks_path)
    return ClientTarget(
        client="claude-code-hooks", name=name, config_path=str(hooks_path),
        before=before, after_sha256=after.sha256,
    )


# --------------------------------------------------------------------------
# inspect_connection -- read-only
# --------------------------------------------------------------------------


def inspect_connection(
    project_dir: Path,
    name: str | None = None,
    *,
    clients: frozenset[str] = _ALL_CLIENTS,
    codex_bin: str | None = None,
    claude_bin: str | None = None,
    codex_home: Path | None = None,
    claude_home: Path | None = None,
    claude_desktop_home: Path | None = None,
    project_hooks_path: Path | None = None,
) -> ConnectionInspection:
    """Read-only: never writes anything, never spawns `codex`/`claude`/
    `npx`. Reports every manifest/runtime/client/hook target's drift state
    from what the manifest last recorded, and whether the connection is
    stuck `rollback_incomplete` with real unresolved conflicts."""
    resolved = project_dir.resolve()
    manifest = ConnectionManifest.load(resolved)
    if manifest is None:
        return ConnectionInspection(
            name=name or default_project_name(resolved),
            project_dir=str(resolved),
            manifest_status=None,
            runtime_state="missing",
            targets=(),
        )

    runtime_snapshot = FileSnapshot.capture(Path(manifest.runtime.wrap_path))
    runtime_state: TargetState = "healthy" if runtime_snapshot.existed else "missing"

    target_inspections = []
    for t in manifest.targets:
        current = FileSnapshot.capture(Path(t.config_path))
        state = classify_target_state(recorded_after_sha256=t.after_sha256, current=current)
        if manifest.status == "rollback_incomplete" and state == "modified":
            state = "conflict"
        target_inspections.append(
            TargetInspection(client=t.client, name=t.name, config_path=t.config_path, state=state)
        )

    hook_state: TargetState | None = None
    if manifest.hook_target is not None:
        current = FileSnapshot.capture(Path(manifest.hook_target.config_path))
        hook_state = classify_target_state(
            recorded_after_sha256=manifest.hook_target.after_sha256, current=current
        )
        if manifest.status == "rollback_incomplete" and hook_state == "modified":
            hook_state = "conflict"

    return ConnectionInspection(
        name=manifest.name,
        project_dir=str(resolved),
        manifest_status=manifest.status,
        runtime_state=runtime_state,
        targets=tuple(target_inspections),
        hook_state=hook_state,
        failure=manifest.failure,
    )


# --------------------------------------------------------------------------
# connect
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectResult:
    manifest: ConnectionManifest
    already_connected: bool = False


def connect(
    project_dir: Path,
    name: str | None = None,
    *,
    clients: frozenset[str] = _ALL_CLIENTS,
    codex_bin: str | None = None,
    claude_bin: str | None = None,
    codex_home: Path | None = None,
    claude_home: Path | None = None,
    claude_desktop_home: Path | None = None,
    project_hooks_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    preflight_timeout: float = DEFAULT_PREFLIGHT_TIMEOUT,
    registration_timeout: float = 30.0,
) -> ConnectResult:
    """Connect every detected, requested MCP client in `project_dir` to a
    fresh, protected, Belay-wrapped instance of the pinned Filesystem MCP
    server. Fixed order (never reordered): resolve project -> validate
    dependencies -> build desired state -> upstream preflight -> detect
    collisions -> snapshot every target -> write atomic runtime/connecting
    manifest -> register all detected clients -> install Claude project
    hooks -> verify (real MCP through each client's own recorded
    registration) -> mark connected. Any failure past the manifest write
    triggers reverse-order compensation; a target that changed underneath
    Belay during that compensation is never overwritten -- the connection
    is left `rollback_incomplete` instead."""
    iso = _Isolation(
        clients=clients, codex_bin=codex_bin, claude_bin=claude_bin,
        codex_home=codex_home, claude_home=claude_home,
        claude_desktop_home=claude_desktop_home, project_hooks_path=project_hooks_path,
        env=env, preflight_timeout=preflight_timeout, registration_timeout=registration_timeout,
    )
    resolved_dir = project_dir.resolve()
    resolved_name = validate_name(name) if name else default_project_name(resolved_dir)

    existing = ConnectionManifest.load(resolved_dir)
    if existing is not None and existing.status in ("connecting", "rollback_incomplete"):
        raise RollbackIncompleteError(existing, [t.client for t in existing.targets])

    repairing = existing is not None and existing.status == "connected"
    if repairing:
        assert existing is not None
        inspection = inspect_connection(
            resolved_dir, existing.name, clients=clients, codex_bin=codex_bin,
            claude_bin=claude_bin, codex_home=codex_home, claude_home=claude_home,
            claude_desktop_home=claude_desktop_home, project_hooks_path=project_hooks_path,
        )
        if inspection.healthy:
            return ConnectResult(manifest=existing, already_connected=True)
        resolved_name = existing.name

    if shutil.which("npx") is None:
        raise DependencyError(
            "npx (Node.js) not found on PATH -- required to run the pinned Filesystem MCP server"
        )

    runtime = build_proposed_runtime(resolved_dir)
    available, codex_bin_resolved, claude_bin_resolved = _detect_active_clients(iso)
    active_clients = [c for c in ("codex", "claude", "claude-desktop")
                       if available[c] and c in clients]
    if not active_clients:
        raise DependencyError(
            "no supported MCP client (codex, claude, claude-desktop) detected on this machine"
        )

    # The preflight needs a real, on-disk `belay.wrap.json` to spawn `belay
    # run --config ...` against -- write it now (harmless, static JSON, not
    # a shared client config) so the exact argv that will be registered can
    # be proven end-to-end BEFORE any client is touched. A failed preflight
    # removes it again immediately; nothing downstream has happened yet.
    write_runtime(runtime)

    async def _preflight() -> list[Tool]:
        return await preflight_runtime(runtime, timeout=preflight_timeout)

    try:
        anyio.run(_preflight)
    except Exception:
        if runtime.wrap_path.is_file() and not repairing:
            with contextlib.suppress(OSError):
                runtime.wrap_path.unlink()
        raise

    command, *launch_args = runtime.launch_argv

    to_register = active_clients
    kept_targets: tuple[ClientTarget, ...] = ()
    kept_hook: ClientTarget | None = None
    if repairing:
        assert existing is not None
        healthy_clients = set()
        for t in existing.targets:
            current = FileSnapshot.capture(Path(t.config_path))
            if classify_target_state(
                recorded_after_sha256=t.after_sha256, current=current
            ) == "healthy":
                healthy_clients.add(t.client)
        to_register = [c for c in active_clients if c not in healthy_clients]
        kept_targets = tuple(t for t in existing.targets if t.client in healthy_clients)
        if existing.hook_target is not None:
            hook_current = FileSnapshot.capture(Path(existing.hook_target.config_path))
            if classify_target_state(
                recorded_after_sha256=existing.hook_target.after_sha256, current=hook_current
            ) == "healthy":
                kept_hook = existing.hook_target

    # Collision detection: refuse to silently overwrite an entry Belay
    # doesn't already know is its own healthy registration.
    for client in to_register:
        result = _get_target(
            client, resolved_name, codex_bin=codex_bin_resolved,
            claude_bin=claude_bin_resolved, iso=iso,
        )
        if result is not None and result[0]:
            raise CollisionError(
                f"{client} already has an MCP server named '{resolved_name}' registered, and "
                "it is not a healthy Belay-managed entry -- choose a different --name, or "
                "resolve the existing entry by hand"
            )
        if client == "claude-desktop":
            path = claude_desktop_config_path(iso.resolved_claude_desktop_home())
            before = FileSnapshot.capture(path)
            raw = before.raw_bytes()
            if raw is not None:
                try:
                    doc = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    doc = {}
                if resolved_name in doc.get("mcpServers", {}):
                    raise CollisionError(
                        f"claude-desktop already has an MCP server named '{resolved_name}' -- "
                        "choose a different --name"
                    )

    before_snapshots = {
        client: FileSnapshot.capture(
            _config_path_for(
                client, codex_bin=codex_bin_resolved, claude_bin=claude_bin_resolved, iso=iso
            )
        )
        for client in to_register
    }

    connecting_targets = tuple(
        ClientTarget(
            client=client, name=resolved_name,
            config_path=str(_config_path_for(
                client, codex_bin=codex_bin_resolved, claude_bin=claude_bin_resolved, iso=iso
            )),
            before=before_snapshots[client],
        )
        for client in to_register
    ) + kept_targets
    manifest = ConnectionManifest.new(
        name=resolved_name, project_dir=resolved_dir, runtime=runtime.to_runtime_info(),
        targets=connecting_targets, hook_target=kept_hook, status="connecting",
    )
    manifest.save(resolved_dir)

    registered: list[ClientTarget] = []
    newly_installed_hook: ClientTarget | None = None
    try:
        for client in to_register:
            target = _register_target(
                client, resolved_name, command, launch_args,
                codex_bin=codex_bin_resolved, claude_bin=claude_bin_resolved, iso=iso,
            )
            registered.append(target)

        hook_target = kept_hook
        if "claude" in active_clients and hook_target is None:
            hook_target = _install_project_hooks(resolved_dir, runtime, resolved_name, iso)
            newly_installed_hook = hook_target

        async def _verify() -> None:
            for target in registered:
                readback = _get_target(
                    target.client, resolved_name, codex_bin=codex_bin_resolved,
                    claude_bin=claude_bin_resolved, iso=iso,
                )
                verify_command, verify_args = command, launch_args
                if readback is not None and readback[0] and readback[1]:
                    verify_command, verify_args = readback[1], readback[2]
                with anyio.fail_after(preflight_timeout):
                    async with connect_stdio(verify_command, verify_args) as verify_client:
                        tools = await verify_client.list_tools()
                names = {t.name for t in tools}
                if not (names & EXPECTED_FILESYSTEM_TOOLS):
                    raise PreflightError(
                        f"post-registration verification failed for {target.client}: "
                        "advertised tools don't match the pinned Filesystem upstream"
                    )

        anyio.run(_verify)

        final_targets = tuple(registered) + kept_targets
        connected_manifest = manifest.evolve(
            status="connected", targets=final_targets, hook_target=hook_target, failure=None,
        )
        connected_manifest.save(resolved_dir)
        return ConnectResult(manifest=connected_manifest, already_connected=False)

    except Exception as exc:
        conflicts: list[str] = []
        if newly_installed_hook is not None and not _restore_target(newly_installed_hook):
            conflicts.append(f"{newly_installed_hook.client}:{newly_installed_hook.config_path}")
        for target in reversed(registered):
            if not _restore_target(target):
                conflicts.append(f"{target.client}:{target.config_path}")

        if runtime.wrap_path.is_file() and not repairing:
            try:
                runtime.wrap_path.unlink()
            except OSError:
                conflicts.append(f"runtime:{runtime.wrap_path}")

        if conflicts:
            failed_manifest = manifest.evolve(
                status="rollback_incomplete", targets=tuple(registered) + kept_targets,
                failure=str(exc),
            )
            failed_manifest.save(resolved_dir)
            raise RollbackIncompleteError(failed_manifest, conflicts) from exc

        if repairing:
            assert existing is not None
            existing.save(resolved_dir)
        else:
            manifest_path = ConnectionManifest.manifest_path(resolved_dir)
            if manifest_path.is_file():
                manifest_path.unlink()

        raise TransactionFailedError(
            f"belay connect failed and was fully rolled back: {exc}"
        ) from exc


# --------------------------------------------------------------------------
# disconnect
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DisconnectResult:
    manifest: ConnectionManifest | None
    purged: bool = False


def _remove_target_cas(
    client: str, name: str, target: ClientTarget, *,
    codex_bin: str | None, claude_bin: str | None, iso: _Isolation,
) -> bool:
    """Compare-and-swap removal: only removes if the target's current bytes
    still match what Belay itself last recorded. For codex/claude this
    still goes through the official CLI's own `mcp remove` (never a raw
    file edit) -- but only after confirming, via hash comparison, that
    nothing else touched the file since Belay's last write; `False` means
    a real conflict, never overwritten."""
    path = Path(target.config_path)
    current = FileSnapshot.capture(path)
    if target.after_sha256 is not None and current.sha256 != target.after_sha256:
        return False
    if not current.existed:
        return True  # already gone -- nothing to do, not a conflict
    try:
        if client == "codex":
            assert codex_bin is not None
            iso.codex_adapter(codex_bin).remove(name)
        elif client == "claude":
            assert claude_bin is not None
            iso.claude_adapter(claude_bin).remove(name)
        elif client == "claude-desktop":
            remove_claude_desktop(name, config_path=path)
        elif client == "claude-code-hooks":
            from belay.cli.client_configs import remove_claude_hooks_entry

            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            for event in _HOOKS_EVENTS:
                text = remove_claude_hooks_entry(text, event=event)
            atomic_write(path, text)
        else:
            raise AssertionError(f"unknown client kind: {client!r}")
    except ClientRegistrationError:
        return False
    return True


def disconnect(
    project_dir: Path,
    name: str | None = None,
    *,
    purge_runtime: bool = False,
    clients: frozenset[str] = _ALL_CLIENTS,
    codex_bin: str | None = None,
    claude_bin: str | None = None,
    codex_home: Path | None = None,
    claude_home: Path | None = None,
    claude_desktop_home: Path | None = None,
    project_hooks_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    registration_timeout: float = 30.0,
) -> DisconnectResult:
    """Remove only Belay-managed entries (compare-and-swap: a target
    changed since Belay's own last write is never overwritten). Retains
    `.belay/belay.db` always; retains `.belay/belay.wrap.json` and the
    manifest unless `purge_runtime=True`, and even then only after every
    target either was cleanly removed or was already absent."""
    iso = _Isolation(
        clients=clients, codex_bin=codex_bin, claude_bin=claude_bin,
        codex_home=codex_home, claude_home=claude_home,
        claude_desktop_home=claude_desktop_home, project_hooks_path=project_hooks_path,
        env=env, registration_timeout=registration_timeout,
    )
    resolved_dir = project_dir.resolve()
    manifest = ConnectionManifest.load(resolved_dir)
    if manifest is None:
        return DisconnectResult(manifest=None, purged=False)
    if name is not None and manifest.name != name:
        return DisconnectResult(manifest=None, purged=False)

    codex_bin_resolved = iso.resolved_codex_bin()
    claude_bin_resolved = iso.resolved_claude_bin()

    conflicts: list[str] = []
    kept_targets: list[ClientTarget] = []
    for target in manifest.targets:
        ok = _remove_target_cas(
            target.client, manifest.name, target, codex_bin=codex_bin_resolved,
            claude_bin=claude_bin_resolved, iso=iso,
        )
        if not ok:
            conflicts.append(f"{target.client}:{target.config_path}")
            kept_targets.append(target)

    hook_kept: ClientTarget | None = None
    if manifest.hook_target is not None:
        ok = _remove_target_cas(
            manifest.hook_target.client, manifest.name, manifest.hook_target,
            codex_bin=codex_bin_resolved, claude_bin=claude_bin_resolved, iso=iso,
        )
        if not ok:
            conflicts.append(f"{manifest.hook_target.client}:{manifest.hook_target.config_path}")
            hook_kept = manifest.hook_target

    if conflicts:
        failed_manifest = manifest.evolve(
            status="rollback_incomplete", targets=tuple(kept_targets), hook_target=hook_kept,
            failure=f"disconnect could not remove: {conflicts}",
        )
        failed_manifest.save(resolved_dir)
        raise RollbackIncompleteError(failed_manifest, conflicts)

    from datetime import UTC, datetime

    disconnected_manifest = manifest.evolve(
        status="disconnected", targets=(), hook_target=None,
        disconnected_at=datetime.now(UTC).isoformat(), failure=None,
    )

    if purge_runtime:
        wrap_path = Path(manifest.runtime.wrap_path)
        if wrap_path.is_file():
            wrap_path.unlink()
        manifest_path = ConnectionManifest.manifest_path(resolved_dir)
        if manifest_path.is_file():
            manifest_path.unlink()
        belay_dir = manifest_path.parent
        if belay_dir.is_dir() and not any(belay_dir.iterdir()):
            belay_dir.rmdir()
        return DisconnectResult(manifest=None, purged=True)

    disconnected_manifest.save(resolved_dir)
    return DisconnectResult(manifest=disconnected_manifest, purged=False)
