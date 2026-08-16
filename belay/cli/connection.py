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

import sys
from dataclasses import dataclass
from pathlib import Path

import anyio
from mcp.types import Tool

from belay.bundled_packs import filesystem_pack
from belay.cli.connection_models import RuntimeInfo
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
    bundled, pinned Filesystem pack's own contracts file directly (never a
    copy) and the *canonicalized* (`.resolve()`d) project directory as both
    the upstream's allowed-directory argument and the identity this
    runtime is forever tied to."""
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
        contracts_path=pack.contracts_path,
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
