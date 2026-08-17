"""E22 Tasks 5-6: the transactional `connect`/`disconnect` state machine and
the read-only `inspect_connection` report.

Every test uses FAKE `codex`/`claude` executables (see `tests/cli/
conftest.py`) and isolated home/config directories -- never this
machine's real `~/.codex`, `~/.claude.json`, or Claude Desktop config.
`connect()` itself always runs a REAL MCP preflight (spawns the real
`belay run` -> real `npx` filesystem server), so every test here that
calls `connect()` is `@pytest.mark.slow` and requires Node/`npx` on PATH,
matching this project's established pattern (see
`tests/packs/test_filesystem_pack.py`).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from belay.cli import connection as connection_module
from belay.cli.client_registration import (
    claude_desktop_config_path,
    claude_user_config_path,
    codex_config_path,
)
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

pytestmark = pytest.mark.slow


def _require_npx() -> None:
    if shutil.which("npx") is None:
        pytest.skip("npx (Node.js) not found on PATH -- needed to run the real filesystem server")


@pytest.fixture(autouse=True)
def _npx_guard() -> None:
    _require_npx()


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
    claude_config = claude_user_config_path(claude_home)
    log_path = tmp_path / "cli.log"

    codex_bin = make_fake_codex(tmp_path / "codex_fake_home", codex_config, log_path)
    claude_bin = make_fake_claude(tmp_path / "claude_fake_home", claude_config, log_path)

    # A deliberately MINIMAL, fully isolated environment -- no inherited
    # os.environ. Only what a subprocess literally needs to start on
    # Windows. The fakes themselves don't read env vars at all (their
    # state/config/log paths are baked into the script -- see conftest.py)
    # so this also proves `connect`/`disconnect` never depend on the fakes
    # reading anything from a real developer's environment.
    env: dict[str, str] = {}
    if sys.platform == "win32":
        import os

        for key in ("SYSTEMROOT", "PATH", "PATHEXT", "TEMP", "TMP"):
            if key in os.environ:
                env[key] = os.environ[key]
    else:
        import os

        if "PATH" in os.environ:
            env["PATH"] = os.environ["PATH"]

    return dict(
        codex_bin=str(codex_bin),
        claude_bin=str(claude_bin),
        codex_home=codex_home,
        claude_home=claude_home,
        claude_desktop_home=desktop_home,
        env=env,
    )


def _enable_claude_desktop(desktop_home: Path) -> None:
    claude_desktop_config_path(desktop_home, platform=sys.platform).parent.mkdir(
        parents=True, exist_ok=True
    )


# --------------------------------------------------------------------------
# Success + idempotency
# --------------------------------------------------------------------------


def test_connect_registers_only_detected_clients(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    # Only "codex" is requested/considered -- claude/claude-desktop are not
    # detected as installed for this test (claude IS installed via the fake
    # bin, but we restrict `clients` to prove only requested+detected ones
    # get registered).
    result = connect(project_dir, clients=frozenset({"codex"}), **kwargs)

    assert result.manifest.status == "connected"
    assert {t.client for t in result.manifest.targets} == {"codex"}
    assert result.manifest.hook_target is None

    codex_config = codex_config_path(kwargs["codex_home"])
    doc = json.loads(codex_config.read_text(encoding="utf-8"))
    assert result.manifest.name in doc


def test_connect_installs_project_hooks_only_when_claude_detected(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    hooks_path = project_dir / ".claude" / "settings.json"
    result = connect(
        project_dir, clients=frozenset({"claude"}), project_hooks_path=hooks_path, **kwargs
    )

    assert result.manifest.hook_target is not None
    assert hooks_path.is_file()
    doc = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert "PreToolUse" in doc["hooks"]
    assert "PostToolUse" in doc["hooks"]


def test_connect_second_call_is_a_healthy_noop(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    first = connect(project_dir, clients=frozenset({"codex"}), **kwargs)
    assert first.already_connected is False

    second = connect(project_dir, clients=frozenset({"codex"}), **kwargs)
    assert second.already_connected is True
    assert second.manifest == first.manifest


def test_connect_requires_at_least_one_detected_client(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    # `clients=frozenset()` deterministically means "consider no client
    # kind at all", regardless of what's really installed on this
    # particular dev/CI machine (this machine, e.g., has a real `claude`
    # on PATH via nvm -- relying on absence would be flaky).
    with pytest.raises(DependencyError):
        connect(
            project_dir,
            clients=frozenset(),
            claude_desktop_home=tmp_path / "no-desktop-here",
        )


def test_connect_claude_desktop_fallback(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    _enable_claude_desktop(kwargs["claude_desktop_home"])

    result = connect(project_dir, clients=frozenset({"claude-desktop"}), **kwargs)
    assert {t.client for t in result.manifest.targets} == {"claude-desktop"}

    desktop_config = claude_desktop_config_path(kwargs["claude_desktop_home"])
    doc = json.loads(desktop_config.read_text(encoding="utf-8"))
    assert result.manifest.name in doc["mcpServers"]


def test_connect_collision_with_untracked_entry(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    name = "collide-me"

    from belay.cli.client_registration import CodexAdapter

    CodexAdapter(
        codex_bin=kwargs["codex_bin"], codex_home=kwargs["codex_home"],
        env=kwargs["env"],
    ).add(name, "some-other-command", [])

    with pytest.raises(CollisionError):
        connect(project_dir, name=name, clients=frozenset({"codex"}), **kwargs)


# --------------------------------------------------------------------------
# Failure injection + rollback
# --------------------------------------------------------------------------


def test_connect_failure_during_registration_rolls_back_earlier_targets(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    # codex registers fine; claude's own fake CLI is rigged to fail for the
    # magic name "trigger-fail" -- this exercises "failure after one
    # client registration succeeded, during the next".
    with pytest.raises(TransactionFailedError):
        connect(
            project_dir, name="trigger-fail", clients=frozenset({"codex", "claude"}), **kwargs
        )

    assert ConnectionManifest.load(project_dir) is None
    codex_config = codex_config_path(kwargs["codex_home"])
    # Belay's own compare-and-swap restore must have removed the codex
    # entry it registered moments before claude failed -- exact absence,
    # not just "some cleanup happened".
    if codex_config.is_file():
        doc = json.loads(codex_config.read_text(encoding="utf-8"))
        assert "trigger-fail" not in doc


def test_connect_failure_on_first_client_leaves_no_manifest_and_no_runtime(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    with pytest.raises(TransactionFailedError):
        connect(project_dir, name="trigger-fail", clients=frozenset({"codex"}), **kwargs)

    assert ConnectionManifest.load(project_dir) is None
    assert not (project_dir / ".belay" / "belay.wrap.json").is_file()


def test_connect_failure_after_hook_install_rolls_back_client_and_hook(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration (claude) and hook install both genuinely succeed --
    then the LATER final-verification stage fails (the realistic shape of
    "failure after hook write": something downstream of a real, tracked
    write goes wrong, not the write itself lying about its own success).
    Both the claude registration and the hook install must be rolled back."""
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    hooks_path = project_dir / ".claude" / "settings.json"

    real_connect_stdio = connection_module.connect_stdio
    calls = {"n": 0}

    def _flaky_connect_stdio(*a: object, **kw: object):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] > 1:  # first call = initial preflight; let it pass
            raise RuntimeError("simulated post-hook-install verification failure")
        return real_connect_stdio(*a, **kw)

    monkeypatch.setattr(connection_module, "connect_stdio", _flaky_connect_stdio)

    with pytest.raises(TransactionFailedError):
        connect(
            project_dir, clients=frozenset({"claude"}), project_hooks_path=hooks_path, **kwargs
        )

    assert ConnectionManifest.load(project_dir) is None
    # Both the claude registration and the hook install were rolled back --
    # belay's own restore step covers the hook target the same as any
    # other, since it's just another ClientTarget once registered) -- file
    # absent again, exactly matching its pre-connect state.
    assert not hooks_path.is_file()
    claude_config = claude_user_config_path(kwargs["claude_home"])
    assert not claude_config.is_file() or json.loads(
        claude_config.read_text(encoding="utf-8")
    ) == {}


def test_connect_failure_after_verification_rolls_back(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)

    real_connect_stdio = connection_module.connect_stdio
    calls = {"n": 0}

    def _flaky_connect_stdio(*a: object, **kw: object):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] > 1:  # first call = initial preflight; let it pass
            raise RuntimeError("simulated post-registration verification failure")
        return real_connect_stdio(*a, **kw)

    monkeypatch.setattr(connection_module, "connect_stdio", _flaky_connect_stdio)

    with pytest.raises(TransactionFailedError):
        connect(project_dir, clients=frozenset({"codex"}), **kwargs)

    assert ConnectionManifest.load(project_dir) is None
    codex_config = codex_config_path(kwargs["codex_home"])
    assert not codex_config.is_file() or json.loads(
        codex_config.read_text(encoding="utf-8")
    ) == {}


# --------------------------------------------------------------------------
# Concurrent edit -> rollback_incomplete
# --------------------------------------------------------------------------


def test_disconnect_never_overwrites_a_concurrently_edited_target(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    result = connect(project_dir, clients=frozenset({"codex", "claude"}), **kwargs)
    assert result.manifest.status == "connected"

    codex_config = codex_config_path(kwargs["codex_home"])
    # Simulate a real concurrent external edit: something else touched the
    # codex config after Belay's own post-write snapshot.
    tampered = codex_config.read_text(encoding="utf-8") + "\n// hand-edited\n"
    codex_config.write_text(tampered, encoding="utf-8")

    with pytest.raises(RollbackIncompleteError) as excinfo:
        disconnect(project_dir, **kwargs)

    assert any("codex" in c for c in excinfo.value.conflicts)
    # Never overwritten -- the concurrent edit survives byte-for-byte.
    assert codex_config.read_text(encoding="utf-8") == tampered

    manifest = ConnectionManifest.load(project_dir)
    assert manifest is not None
    assert manifest.status == "rollback_incomplete"
    remaining_clients = {t.client for t in manifest.targets}
    assert "codex" in remaining_clients
    assert "claude" not in remaining_clients  # the non-conflicting target WAS removed

    # A later retry, after the human resolves the conflict by hand
    # (simulated here by restoring the pre-tamper bytes), reaches
    # `disconnected` cleanly.
    original = tampered.replace("\n// hand-edited\n", "")
    codex_config.write_text(original, encoding="utf-8")
    final = disconnect(project_dir, **kwargs)
    assert final.manifest is not None
    assert final.manifest.status == "disconnected"


# --------------------------------------------------------------------------
# inspect_connection (read-only)
# --------------------------------------------------------------------------


def test_inspect_connection_no_manifest() -> None:
    inspection = inspect_connection(Path.cwd() / "definitely-not-a-real-belay-project")
    assert inspection.manifest_status is None
    assert inspection.healthy is False


def test_inspect_connection_healthy_after_connect(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    connect(project_dir, clients=frozenset({"codex"}), **kwargs)

    inspection = inspect_connection(
        project_dir, codex_bin=kwargs["codex_bin"], claude_bin=kwargs["claude_bin"],
        codex_home=kwargs["codex_home"], claude_home=kwargs["claude_home"],
        claude_desktop_home=kwargs["claude_desktop_home"],
    )
    assert inspection.manifest_status == "connected"
    assert inspection.healthy is True
    assert inspection.rollback_incomplete is False


def test_inspect_connection_reports_modified_target(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    connect(project_dir, clients=frozenset({"codex"}), **kwargs)

    codex_config = codex_config_path(kwargs["codex_home"])
    codex_config.write_text(
        codex_config.read_text(encoding="utf-8") + "\n// tampered\n", encoding="utf-8"
    )

    inspection = inspect_connection(
        project_dir, codex_bin=kwargs["codex_bin"], claude_bin=kwargs["claude_bin"],
        codex_home=kwargs["codex_home"], claude_home=kwargs["claude_home"],
        claude_desktop_home=kwargs["claude_desktop_home"],
    )
    assert inspection.healthy is False
    codex_inspections = [t for t in inspection.targets if t.client == "codex"]
    assert codex_inspections[0].state == "modified"
    assert inspection.has_conflict is False  # not rollback_incomplete yet -- just drift


def test_inspect_connection_distinguishes_conflict_from_repairable_drift(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    connect(project_dir, clients=frozenset({"codex", "claude"}), **kwargs)

    codex_config = codex_config_path(kwargs["codex_home"])
    codex_config.write_text(
        codex_config.read_text(encoding="utf-8") + "\n// tampered\n", encoding="utf-8"
    )
    with pytest.raises(RollbackIncompleteError):
        disconnect(project_dir, **kwargs)

    inspection = inspect_connection(
        project_dir, codex_bin=kwargs["codex_bin"], claude_bin=kwargs["claude_bin"],
        codex_home=kwargs["codex_home"], claude_home=kwargs["claude_home"],
        claude_desktop_home=kwargs["claude_desktop_home"],
    )
    assert inspection.rollback_incomplete is True
    assert inspection.has_conflict is True
    codex_inspections = [t for t in inspection.targets if t.client == "codex"]
    assert codex_inspections[0].state == "conflict"


# --------------------------------------------------------------------------
# disconnect
# --------------------------------------------------------------------------


def test_disconnect_healthy_removes_only_belay_entry(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)

    from belay.cli.client_registration import CodexAdapter

    CodexAdapter(
        codex_bin=kwargs["codex_bin"], codex_home=kwargs["codex_home"], env=kwargs["env"],
    ).add("unrelated-server", "other-cmd", [])

    result = connect(project_dir, clients=frozenset({"codex"}), **kwargs)
    codex_config = codex_config_path(kwargs["codex_home"])
    before_doc = json.loads(codex_config.read_text(encoding="utf-8"))
    assert "unrelated-server" in before_doc
    assert result.manifest.name in before_doc

    disconnect_result = disconnect(project_dir, **kwargs)
    assert disconnect_result.manifest is not None
    assert disconnect_result.manifest.status == "disconnected"

    after_doc = json.loads(codex_config.read_text(encoding="utf-8"))
    assert "unrelated-server" in after_doc  # preserved
    assert result.manifest.name not in after_doc  # removed


def test_disconnect_missing_manifest_is_a_noop(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    result = disconnect(project_dir)
    assert result.manifest is None
    assert result.purged is False


def test_disconnect_is_idempotent(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    connect(project_dir, clients=frozenset({"codex"}), **kwargs)

    first = disconnect(project_dir, **kwargs)
    second = disconnect(project_dir, **kwargs)
    assert first.manifest is not None and first.manifest.status == "disconnected"
    assert second.manifest is not None and second.manifest.status == "disconnected"


def test_disconnect_retains_runtime_by_default(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    result = connect(project_dir, clients=frozenset({"codex"}), **kwargs)
    wrap_path = Path(result.manifest.runtime.wrap_path)
    assert wrap_path.is_file()

    disconnect(project_dir, **kwargs)
    assert wrap_path.is_file()
    assert ConnectionManifest.manifest_path(project_dir).is_file()


def test_disconnect_purge_runtime_removes_wrap_and_manifest_but_never_db(
    tmp_path: Path, make_fake_codex: Path, make_fake_claude: Path
) -> None:
    project_dir = _project(tmp_path)
    kwargs = _connect_kwargs(tmp_path, make_fake_codex, make_fake_claude)
    result = connect(project_dir, clients=frozenset({"codex"}), **kwargs)
    wrap_path = Path(result.manifest.runtime.wrap_path)
    db_path = Path(result.manifest.runtime.db_path)
    assert wrap_path.is_file()

    purge_result = disconnect(project_dir, purge_runtime=True, **kwargs)
    assert purge_result.purged is True
    assert purge_result.manifest is None
    assert not wrap_path.is_file()
    assert not ConnectionManifest.manifest_path(project_dir).is_file()
    # belay.db is never deleted by disconnect, purge or not -- the real
    # preflight/verify MCP sessions above created it for real (SQLAlchemy
    # creates the file on first connection), and it must survive purge.
    assert db_path.name == "belay.db"
    assert db_path.is_file()
