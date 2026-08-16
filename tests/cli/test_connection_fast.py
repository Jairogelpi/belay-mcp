"""E22 Tasks 5-6, fast lane: the SAME `connect`/`disconnect`/
`inspect_connection` state-machine branches `tests/cli/test_connection.py`
exercises end-to-end against a real MCP server, but with the real-MCP
stages (`preflight_runtime`, the post-registration `connect_stdio`
verification) replaced by an in-memory fake -- no `npx`, no real subprocess
MCP session, no `@pytest.mark.slow`.

This exists because `belay/cli/connection.py`'s own orchestration logic
(collision detection, snapshot/rollback bookkeeping, the repair
skip-healthy-targets branch, disconnect's compare-and-swap) is otherwise
covered ONLY by the slow, npx-gated tests -- which this repo's own
`pytest.ini` marker filter (`not slow`) excludes from the fast,
coverage-gated default `pytest` run. Client registration itself still goes
through the REAL fake `codex`/`claude` CLI subprocess (fast, no npx
needed) -- only the two real-MCP stages are faked, so this still proves
belay's own transaction/rollback logic against a real external process,
just not against the real Filesystem MCP server."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest
from belay.cli import connection as connection_module
from belay.cli.client_registration import claude_desktop_config_path, codex_config_path
from belay.cli.connection import (
    CollisionError,
    ConnectionManifest,
    DependencyError,
    RollbackIncompleteError,
    TransactionFailedError,
    connect,
    disconnect,
    inspect_connection,
)


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeMCPClient:
    async def list_tools(self) -> list[_FakeTool]:
        return [_FakeTool(n) for n in ("write_file", "read_file", "list_directory")]

    async def call_tool(self, tool: str, args: dict) -> object:  # pragma: no cover - unused here
        raise NotImplementedError


class _FakeConnectStdioCM:
    async def __aenter__(self) -> _FakeMCPClient:
        return _FakeMCPClient()

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _fake_connect_stdio(*args: object, **kwargs: object) -> _FakeConnectStdioCM:
    return _FakeConnectStdioCM()


async def _fake_preflight_runtime(runtime: object, *, timeout: float = 0.0) -> list[_FakeTool]:
    return [_FakeTool(n) for n in ("write_file", "read_file")]


@pytest.fixture(autouse=True)
def _fake_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module gets both real-MCP stages faked --
    `connect()`/`disconnect()` never actually spawn `npx` here."""
    monkeypatch.setattr(connection_module, "connect_stdio", _fake_connect_stdio)
    monkeypatch.setattr(connection_module, "preflight_runtime", _fake_preflight_runtime)


def _project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p


def _connect_kwargs(tmp_path: Path, make_fake_codex, make_fake_claude) -> dict:
    codex_home = tmp_path / "codex_home"
    claude_home = tmp_path / "claude_home"
    desktop_home = tmp_path / "desktop_home"
    codex_home.mkdir(exist_ok=True)
    claude_home.mkdir(exist_ok=True)
    desktop_home.mkdir(exist_ok=True)
    codex_config = codex_config_path(codex_home)
    from belay.cli.client_registration import claude_user_config_path

    claude_config = claude_user_config_path(claude_home)
    log_path = tmp_path / "cli.log"

    codex_bin = make_fake_codex(tmp_path / "codex_fake_home", codex_config, log_path)
    claude_bin = make_fake_claude(tmp_path / "claude_fake_home", claude_config, log_path)

    return dict(
        codex_bin=str(codex_bin),
        claude_bin=str(claude_bin),
        codex_home=codex_home,
        claude_home=claude_home,
        claude_desktop_home=desktop_home,
        env={},
    )


def _enable_claude_desktop(desktop_home: Path) -> None:
    import sys

    claude_desktop_config_path(desktop_home, platform=sys.platform).parent.mkdir(
        parents=True, exist_ok=True
    )


# --------------------------------------------------------------------------
# Success + idempotency + collisions
# --------------------------------------------------------------------------


def test_connect_success_single_client(
    tmp_path: Path, make_fake_codex, make_fake_claude
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    result = connect(project_dir, clients=frozenset({"codex"}), **kwargs)
    assert result.manifest.status == "connected"
    assert {t.client for t in result.manifest.targets} == {"codex"}


def test_connect_with_hooks(tmp_path: Path, make_fake_codex, make_fake_claude) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    hooks_path = project_dir / ".claude" / "settings.json"
    result = connect(
        project_dir, clients=frozenset({"claude"}), project_hooks_path=hooks_path, **kwargs
    )
    assert result.manifest.hook_target is not None
    assert hooks_path.is_file()


def test_connect_claude_desktop(tmp_path: Path, make_fake_codex, make_fake_claude) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    _enable_claude_desktop(kwargs["claude_desktop_home"])
    result = connect(project_dir, clients=frozenset({"claude-desktop"}), **kwargs)
    assert {t.client for t in result.manifest.targets} == {"claude-desktop"}


def test_connect_idempotent_noop(tmp_path: Path, make_fake_codex, make_fake_claude) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    first = connect(project_dir, clients=frozenset({"codex"}), **kwargs)
    second = connect(project_dir, clients=frozenset({"codex"}), **kwargs)
    assert first.already_connected is False
    assert second.already_connected is True
    assert second.manifest == first.manifest


def test_connect_no_clients_requested_is_dependency_error(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    with pytest.raises(DependencyError):
        connect(project_dir, clients=frozenset(), claude_desktop_home=tmp_path / "no-desktop")


def test_connect_collision(tmp_path: Path, make_fake_codex, make_fake_claude) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    from belay.cli.client_registration import CodexAdapter

    CodexAdapter(
        codex_bin=kwargs["codex_bin"], codex_home=kwargs["codex_home"], env=kwargs["env"],
    ).add("taken-name", "some-cmd", [])

    with pytest.raises(CollisionError):
        connect(project_dir, name="taken-name", clients=frozenset({"codex"}), **kwargs)


# --------------------------------------------------------------------------
# Failure injection + rollback
# --------------------------------------------------------------------------


def test_connect_failure_registering_second_client_rolls_back_first(
    tmp_path: Path, make_fake_codex, make_fake_claude
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    with pytest.raises(TransactionFailedError):
        connect(
            project_dir, name="trigger-fail", clients=frozenset({"codex", "claude"}), **kwargs
        )
    assert ConnectionManifest.load(project_dir) is None
    codex_config = codex_config_path(kwargs["codex_home"])
    if codex_config.is_file():
        doc = json.loads(codex_config.read_text(encoding="utf-8"))
        assert "trigger-fail" not in doc


def test_connect_failure_on_only_client_leaves_no_manifest_or_runtime(
    tmp_path: Path, make_fake_codex, make_fake_claude
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    with pytest.raises(TransactionFailedError):
        connect(project_dir, name="trigger-fail", clients=frozenset({"codex"}), **kwargs)
    assert ConnectionManifest.load(project_dir) is None
    assert not (project_dir / ".belay" / "belay.wrap.json").is_file()


def test_connect_failure_after_hook_and_registration_rolls_back_both(
    tmp_path: Path, make_fake_codex, make_fake_claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    hooks_path = project_dir / ".claude" / "settings.json"

    def _flaky_connect_stdio(*a: object, **kw: object) -> _FakeConnectStdioCM:
        # `preflight_runtime` is separately faked in this module (it never
        # calls `connect_stdio` at all here) -- the ONLY caller left is
        # `connect()`'s own post-registration verification step, so simply
        # always failing here simulates "verification fails" cleanly.
        raise RuntimeError("simulated post-hook verification failure")

    monkeypatch.setattr(connection_module, "connect_stdio", _flaky_connect_stdio)

    with pytest.raises(TransactionFailedError):
        connect(
            project_dir, clients=frozenset({"claude"}), project_hooks_path=hooks_path, **kwargs
        )

    assert ConnectionManifest.load(project_dir) is None
    assert not hooks_path.is_file()
    from belay.cli.client_registration import claude_user_config_path

    claude_config = claude_user_config_path(kwargs["claude_home"])
    assert not claude_config.is_file() or json.loads(
        claude_config.read_text(encoding="utf-8")
    ) == {}


def test_connect_failure_during_preflight_leaves_nothing(
    tmp_path: Path, make_fake_codex, make_fake_claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)

    async def _failing_preflight(runtime: object, *, timeout: float = 0.0) -> list[object]:
        raise connection_module.PreflightError("simulated preflight failure")

    monkeypatch.setattr(connection_module, "preflight_runtime", _failing_preflight)

    with pytest.raises(connection_module.PreflightError):
        connect(project_dir, clients=frozenset({"codex"}), **kwargs)

    assert ConnectionManifest.load(project_dir) is None
    assert not (project_dir / ".belay" / "belay.wrap.json").is_file()


# --------------------------------------------------------------------------
# Repair (unhealthy-but-not-conflicted -> re-register only unhealthy targets)
# --------------------------------------------------------------------------


def test_connect_repair_only_re_registers_unhealthy_targets(
    tmp_path: Path, make_fake_codex, make_fake_claude
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    first = connect(project_dir, clients=frozenset({"codex", "claude"}), **kwargs)
    assert {t.client for t in first.manifest.targets} == {"codex", "claude"}

    # Simulate the codex registration going missing (e.g. a hand-removed
    # entry) -- claude stays healthy. Removed through the adapter itself
    # (not just deleting the file) so the fake CLI's own internal state
    # agrees the entry is gone too, same as a real `codex mcp remove`
    # would leave it -- otherwise `codex mcp get` would still report it
    # present even though the tracked config file is gone.
    from belay.cli.client_registration import CodexAdapter

    codex_config = codex_config_path(kwargs["codex_home"])
    CodexAdapter(
        codex_bin=kwargs["codex_bin"], codex_home=kwargs["codex_home"], env=kwargs["env"],
    ).remove(first.manifest.name)
    assert first.manifest.name not in json.loads(codex_config.read_text(encoding="utf-8"))

    repaired = connect(project_dir, clients=frozenset({"codex", "claude"}), **kwargs)
    assert repaired.already_connected is False
    assert {t.client for t in repaired.manifest.targets} == {"codex", "claude"}
    assert codex_config.is_file()


# --------------------------------------------------------------------------
# Concurrent edit -> rollback_incomplete, then reconciled retry
# --------------------------------------------------------------------------


def test_disconnect_never_overwrites_a_concurrent_edit_and_retry_succeeds(
    tmp_path: Path, make_fake_codex, make_fake_claude
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    connect(project_dir, clients=frozenset({"codex", "claude"}), **kwargs)

    codex_config = codex_config_path(kwargs["codex_home"])
    tampered = codex_config.read_text(encoding="utf-8") + "\n// hand-edited\n"
    codex_config.write_text(tampered, encoding="utf-8")

    with pytest.raises(RollbackIncompleteError) as excinfo:
        disconnect(project_dir, **kwargs)
    assert any("codex" in c for c in excinfo.value.conflicts)
    assert codex_config.read_text(encoding="utf-8") == tampered

    manifest = ConnectionManifest.load(project_dir)
    assert manifest is not None and manifest.status == "rollback_incomplete"
    assert {t.client for t in manifest.targets} == {"codex"}

    codex_config.write_text(tampered.replace("\n// hand-edited\n", ""), encoding="utf-8")
    final = disconnect(project_dir, **kwargs)
    assert final.manifest is not None and final.manifest.status == "disconnected"


def test_connect_refuses_when_manifest_is_rollback_incomplete(
    tmp_path: Path, make_fake_codex, make_fake_claude
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    connect(project_dir, clients=frozenset({"codex", "claude"}), **kwargs)
    codex_config = codex_config_path(kwargs["codex_home"])
    codex_config.write_text(
        codex_config.read_text(encoding="utf-8") + "\n// x\n", encoding="utf-8"
    )
    with contextlib.suppress(RollbackIncompleteError):
        disconnect(project_dir, **kwargs)

    with pytest.raises(RollbackIncompleteError):
        connect(project_dir, clients=frozenset({"codex", "claude"}), **kwargs)


# --------------------------------------------------------------------------
# inspect_connection
# --------------------------------------------------------------------------


def test_inspect_connection_variants(
    tmp_path: Path, make_fake_codex, make_fake_claude
) -> None:
    project_dir = _project(tmp_path)
    assert inspect_connection(project_dir).manifest_status is None

    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    connect(project_dir, clients=frozenset({"codex"}), **kwargs)
    healthy = inspect_connection(
        project_dir, codex_bin=kwargs["codex_bin"], claude_bin=kwargs["claude_bin"],
        codex_home=kwargs["codex_home"], claude_home=kwargs["claude_home"],
        claude_desktop_home=kwargs["claude_desktop_home"],
    )
    assert healthy.healthy is True

    codex_config = codex_config_path(kwargs["codex_home"])
    codex_config.write_text(
        codex_config.read_text(encoding="utf-8") + "\n// x\n", encoding="utf-8"
    )
    modified = inspect_connection(
        project_dir, codex_bin=kwargs["codex_bin"], claude_bin=kwargs["claude_bin"],
        codex_home=kwargs["codex_home"], claude_home=kwargs["claude_home"],
        claude_desktop_home=kwargs["claude_desktop_home"],
    )
    assert modified.healthy is False
    assert modified.targets[0].state == "modified"
    assert modified.has_conflict is False


# --------------------------------------------------------------------------
# disconnect variants
# --------------------------------------------------------------------------


def test_disconnect_variants(tmp_path: Path, make_fake_codex, make_fake_claude) -> None:
    project_dir = _project(tmp_path)

    assert disconnect(project_dir).manifest is None

    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    result = connect(project_dir, clients=frozenset({"codex"}), **kwargs)
    wrap_path = Path(result.manifest.runtime.wrap_path)
    db_path = Path(result.manifest.runtime.db_path)

    first = disconnect(project_dir, **kwargs)
    second = disconnect(project_dir, **kwargs)
    assert first.manifest is not None and first.manifest.status == "disconnected"
    assert second.manifest is not None and second.manifest.status == "disconnected"
    assert wrap_path.is_file()  # retained by default

    reconnected = connect(project_dir, clients=frozenset({"codex"}), **kwargs)
    assert reconnected.already_connected is False
    purge_result = disconnect(project_dir, purge_runtime=True, **kwargs)
    assert purge_result.purged is True
    assert not wrap_path.is_file()
    assert not ConnectionManifest.manifest_path(project_dir).is_file()
    assert not db_path.exists() or db_path.name == "belay.db"
