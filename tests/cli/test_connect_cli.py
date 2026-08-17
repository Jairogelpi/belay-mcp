"""E22 Task 7: thin `belay connect`/`disconnect` CLI commands, plus their
`belay doctor`/`belay repair` extensions.

The CLI layer only parses options, calls `belay/cli/connection.py`'s
orchestration functions, and renders/translates their results -- these
tests monkeypatch `belay.cli.connection.connect`/`disconnect`/
`inspect_connection` (the exact names `belay/cli/main.py` lazily imports,
so patching the module attribute before invoking the CLI takes effect) to
verify that parsing/rendering/exit-code layer in isolation, without
re-running a real transaction (already covered end-to-end by
`tests/cli/test_connection.py`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from belay.cli import connection as connection_module
from belay.cli.connection_models import (
    ClientTarget,
    ConnectionManifest,
    FileSnapshot,
    RuntimeInfo,
)
from belay.cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def _runtime(project_dir: Path) -> RuntimeInfo:
    return RuntimeInfo(
        wrap_path=str(project_dir / ".belay" / "belay.wrap.json"),
        db_path=str(project_dir / ".belay" / "belay.db"),
        contracts_path=str(project_dir / ".belay" / "contracts.yaml"),
        upstream_argv=("npx", "-y", "server@1.0.0", str(project_dir)),
    )


def _target(client: str, name: str) -> ClientTarget:
    path = f"/fake/{client}/config"
    return ClientTarget(
        client=client, name=name, config_path=path,
        before=FileSnapshot(path=path, existed=False, content_b64=None, sha256=None),
        after_sha256="sha256:deadbeef",
    )


def _manifest(project_dir: Path, *, status: str = "connected") -> ConnectionManifest:
    return ConnectionManifest.new(
        name="myproj-abc12345", project_dir=project_dir, runtime=_runtime(project_dir),
        targets=(_target("codex", "myproj-abc12345"),),
    ).evolve(status=status)


# --------------------------------------------------------------------------
# connect
# --------------------------------------------------------------------------


def test_connect_help_exits_zero() -> None:
    result = runner.invoke(app, ["connect", "--help"])
    assert result.exit_code == 0
    assert "connect" in result.stdout.lower()


def test_disconnect_help_exits_zero() -> None:
    result = runner.invoke(app, ["disconnect", "--help"])
    assert result.exit_code == 0
    assert "disconnect" in result.stdout.lower()


def test_connect_no_args_defaults_to_cwd_all_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_connect(project_dir: Path, name: str | None = None, *, clients=None, **kw):  # type: ignore[no-untyped-def]
        captured["project_dir"] = project_dir
        captured["name"] = name
        captured["clients"] = clients
        return connection_module.ConnectResult(manifest=_manifest(project_dir))

    monkeypatch.setattr(connection_module, "connect", fake_connect)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["connect"])
    assert result.exit_code == 0
    assert captured["name"] is None
    assert captured["clients"] == frozenset({"codex", "claude", "claude-desktop"})
    assert "connected" in result.stdout.lower()


def test_connect_with_name_and_project_and_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_connect(project_dir: Path, name: str | None = None, *, clients=None, **kw):  # type: ignore[no-untyped-def]
        captured["project_dir"] = project_dir
        captured["name"] = name
        captured["clients"] = clients
        return connection_module.ConnectResult(manifest=_manifest(project_dir))

    monkeypatch.setattr(connection_module, "connect", fake_connect)

    result = runner.invoke(
        app,
        [
            "connect", "--project", str(tmp_path), "--name", "my-name",
            "--client", "codex,claude",
        ],
    )
    assert result.exit_code == 0
    assert captured["project_dir"] == tmp_path.resolve()
    assert captured["name"] == "my-name"
    assert captured["clients"] == frozenset({"codex", "claude"})


def test_connect_unknown_client_value_exits_nonzero() -> None:
    result = runner.invoke(app, ["connect", "--client", "not-a-real-client"])
    assert result.exit_code != 0


def test_connect_reports_concise_success_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path).evolve(
        targets=(_target("codex", "n"), _target("claude", "n")),
        hook_target=_target("claude-code-hooks", "n"),
    )

    def fake_connect(project_dir: Path, name: str | None = None, **kw):  # type: ignore[no-untyped-def]
        return connection_module.ConnectResult(manifest=manifest)

    monkeypatch.setattr(connection_module, "connect", fake_connect)
    result = runner.invoke(app, ["connect", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert "codex" in result.stdout
    assert "claude" in result.stdout
    assert "hooks" in result.stdout.lower()


def test_connect_collision_error_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_connect(project_dir: Path, name: str | None = None, **kw):  # type: ignore[no-untyped-def]
        raise connection_module.CollisionError("codex already has 'x' registered")

    monkeypatch.setattr(connection_module, "connect", fake_connect)
    result = runner.invoke(app, ["connect", "--project", str(tmp_path)])
    assert result.exit_code == 1
    assert "already has" in result.stdout + (result.stderr or "")


def test_connect_no_client_detected_error_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_connect(project_dir: Path, name: str | None = None, **kw):  # type: ignore[no-untyped-def]
        raise connection_module.DependencyError("no supported MCP client detected")

    monkeypatch.setattr(connection_module, "connect", fake_connect)
    result = runner.invoke(app, ["connect", "--project", str(tmp_path)])
    assert result.exit_code == 1
    assert "no supported mcp client" in (result.stdout + (result.stderr or "")).lower()


def test_connect_rollback_conflict_exits_with_distinct_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path, status="rollback_incomplete")

    def fake_connect(project_dir: Path, name: str | None = None, **kw):  # type: ignore[no-untyped-def]
        raise connection_module.RollbackIncompleteError(manifest, ["codex:/fake/codex/config"])

    monkeypatch.setattr(connection_module, "connect", fake_connect)
    result = runner.invoke(app, ["connect", "--project", str(tmp_path)])
    assert result.exit_code == 2
    assert result.exit_code != 1


def test_connect_already_connected_reports_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_connect(project_dir: Path, name: str | None = None, **kw):  # type: ignore[no-untyped-def]
        return connection_module.ConnectResult(
            manifest=_manifest(project_dir), already_connected=True
        )

    monkeypatch.setattr(connection_module, "connect", fake_connect)
    result = runner.invoke(app, ["connect", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert "already connected" in result.stdout.lower()


# --------------------------------------------------------------------------
# disconnect
# --------------------------------------------------------------------------


def test_disconnect_purge_runtime_flag_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_disconnect(project_dir: Path, name: str | None = None, *, purge_runtime=False, **kw):  # type: ignore[no-untyped-def]
        captured["purge_runtime"] = purge_runtime
        return connection_module.DisconnectResult(manifest=None, purged=True)

    monkeypatch.setattr(connection_module, "disconnect", fake_disconnect)
    result = runner.invoke(
        app, ["disconnect", "--project", str(tmp_path), "--purge-runtime"]
    )
    assert result.exit_code == 0
    assert captured["purge_runtime"] is True
    assert "purged" in result.stdout.lower()


def test_disconnect_missing_manifest_reports_nothing_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_disconnect(project_dir: Path, name: str | None = None, **kw):  # type: ignore[no-untyped-def]
        return connection_module.DisconnectResult(manifest=None, purged=False)

    monkeypatch.setattr(connection_module, "disconnect", fake_disconnect)
    result = runner.invoke(app, ["disconnect", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert "nothing to disconnect" in result.stdout.lower()


def test_disconnect_rollback_conflict_exits_with_distinct_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path, status="rollback_incomplete")

    def fake_disconnect(project_dir: Path, name: str | None = None, **kw):  # type: ignore[no-untyped-def]
        raise connection_module.RollbackIncompleteError(manifest, ["codex:/fake/codex/config"])

    monkeypatch.setattr(connection_module, "disconnect", fake_disconnect)
    result = runner.invoke(app, ["disconnect", "--project", str(tmp_path)])
    assert result.exit_code == 2


# --------------------------------------------------------------------------
# doctor / repair -- E22 preamble
# --------------------------------------------------------------------------


def test_doctor_renders_inspect_connection_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    manifest.save(tmp_path)
    before_bytes = ConnectionManifest.manifest_path(tmp_path).read_bytes()

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "myproj-abc12345" in result.stdout
    # Read-only: doctor must never modify the manifest it just reported on.
    assert ConnectionManifest.manifest_path(tmp_path).read_bytes() == before_bytes


def test_repair_refuses_rollback_incomplete_with_disconnect_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path, status="rollback_incomplete")
    manifest.save(tmp_path)

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["repair", "--yes"])
    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "rollback_incomplete" in combined
    assert "disconnect" in combined


def test_repair_calls_connect_for_unhealthy_but_not_conflicted_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    manifest.save(tmp_path)
    # Delete the codex config file the manifest points at, so
    # inspect_connection reports it "missing" (repairable), not a conflict.

    called: dict[str, object] = {}

    def fake_connect(project_dir: Path, name: str | None = None, **kw):  # type: ignore[no-untyped-def]
        called["invoked"] = True
        return connection_module.ConnectResult(manifest=manifest)

    monkeypatch.setattr(connection_module, "connect", fake_connect)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["repair", "--yes"])
    assert result.exit_code == 0
    assert called.get("invoked") is True
