"""E22 Task 4: generate the exact protected proxy runtime `belay connect`
will register, and preflight it for real -- spawn the exact launch argv,
speak real MCP `initialize`/`list_tools` THROUGH Belay's own proxy (never
by connecting to the pinned Filesystem upstream directly), and prove
directory confinement the same way.

The `@pytest.mark.slow` tests below spawn real subprocesses (`python -m
belay.cli.main run` -> `npx -y @modelcontextprotocol/server-filesystem@...`)
and require `npx` (Node.js) on PATH; they skip cleanly if it's absent,
matching `tests/packs/test_filesystem_pack.py`'s own established pattern.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from belay.bundled_packs import filesystem_pack
from belay.cli.connection import (
    PreflightError,
    belay_launch_argv,
    build_proposed_runtime,
    preflight_runtime,
    write_runtime,
)
from belay.proxy.config import WrapConfig
from belay.proxy.upstream import connect_stdio

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _require_npx() -> None:
    if shutil.which("npx") is None:
        pytest.skip("npx (Node.js) not found on PATH -- needed to run the real filesystem server")


# --------------------------------------------------------------------------
# Runtime generation (pure, no subprocess)
# --------------------------------------------------------------------------


def test_proposed_runtime_paths(tmp_path: Path) -> None:
    runtime = build_proposed_runtime(tmp_path)
    canonical = tmp_path.resolve()
    assert runtime.wrap_path == canonical / ".belay" / "belay.wrap.json"
    assert runtime.db_path == canonical / ".belay" / "belay.db"
    assert runtime.project_dir == canonical


def test_proposed_runtime_uses_bundled_contracts(tmp_path: Path) -> None:
    runtime = build_proposed_runtime(tmp_path)
    pack = filesystem_pack()
    assert runtime.contracts_path == pack.contracts_path


def test_proposed_runtime_exact_pinned_upstream_argv(tmp_path: Path) -> None:
    runtime = build_proposed_runtime(tmp_path)
    assert runtime.upstream_argv == (
        "npx", "-y", "@modelcontextprotocol/server-filesystem@2026.7.10", str(tmp_path.resolve()),
    )


def test_launch_argv_non_frozen(tmp_path: Path) -> None:
    wrap_path = tmp_path / ".belay" / "belay.wrap.json"
    argv = belay_launch_argv(wrap_path)
    assert argv == (sys.executable, "-m", "belay.cli.main", "run", "--config", str(wrap_path))


def test_launch_argv_frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    wrap_path = tmp_path / ".belay" / "belay.wrap.json"
    argv = belay_launch_argv(wrap_path)
    assert argv == (sys.executable, "run", "--config", str(wrap_path))


def test_write_runtime_produces_a_loadable_wrap_config(tmp_path: Path) -> None:
    runtime = build_proposed_runtime(tmp_path)
    write_runtime(runtime)

    assert runtime.wrap_path.is_file()
    loaded = WrapConfig.load(runtime.wrap_path)
    assert loaded.upstream.command == "npx"
    assert loaded.upstream.args == [
        "-y", "@modelcontextprotocol/server-filesystem@2026.7.10", str(tmp_path.resolve()),
    ]
    assert loaded.contracts == [str(runtime.contracts_path)]
    assert loaded.db == str(runtime.db_path)


# --------------------------------------------------------------------------
# Real MCP-through-Belay preflight
# --------------------------------------------------------------------------


@pytest.mark.slow
async def test_preflight_runtime_lists_real_filesystem_tools_through_belay(
    tmp_path: Path,
) -> None:
    _require_npx()
    runtime = build_proposed_runtime(tmp_path)
    write_runtime(runtime)

    tools = await preflight_runtime(runtime)

    tool_names = {t.name for t in tools}
    assert "write_file" in tool_names
    assert "read_file" in tool_names
    assert "list_directory" in tool_names


@pytest.mark.slow
async def test_preflight_runtime_fails_loudly_when_wrap_config_missing(tmp_path: Path) -> None:
    _require_npx()
    # No write_runtime() call -- the wrap config doesn't exist, so `belay
    # run` itself must fail fast, and the preflight must translate that
    # into PreflightError (never a raw subprocess/SDK traceback).
    runtime = build_proposed_runtime(tmp_path)
    with pytest.raises(PreflightError):
        await preflight_runtime(runtime, timeout=30.0)


@pytest.mark.slow
async def test_directory_confinement_through_the_generated_belay_proxy(tmp_path: Path) -> None:
    """Never connects to the Filesystem upstream directly -- every call here
    goes through the exact registered `belay run` launch argv."""
    _require_npx()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("do not read me", encoding="utf-8")

    runtime = build_proposed_runtime(project_dir)
    write_runtime(runtime)
    command, *args = runtime.launch_argv

    async with connect_stdio(command, args) as client:
        inside_result = await client.call_tool(
            "write_file", {"path": str(project_dir / "inside.txt"), "content": "hello"}
        )
        assert inside_result.isError is not True
        assert (project_dir / "inside.txt").read_text(encoding="utf-8") == "hello"

        listed = await client.call_tool("list_directory", {"path": str(project_dir)})
        assert listed.isError is not True

        outside_result = await client.call_tool(
            "read_file", {"path": str(outside_dir / "secret.txt")}
        )
        assert outside_result.isError is True
