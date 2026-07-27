"""End-to-end lifecycle tests for `belay init`/`belay uninstall`/`belay doctor`
against a project-scoped client config (claude-code's `.mcp.json`, resolved
relative to cwd -- `monkeypatch.chdir` keeps every test inside its own
`tmp_path`, never touching a real home directory).

These are the regression tests for four P0s found reviewing commit 2e815ef:

1. Reinstalling (running `init` twice) used to overwrite `.belay-backup` with
   already-belay-containing content, so a later `uninstall` "restore" put
   belay right back instead of reverting to the true original.
2. `init` into a config file that didn't exist yet, followed by `uninstall`,
   left behind an empty `{"mcpServers": {}}` instead of removing the file.
3. `uninstall` used the CLI's own `--name` default instead of the name
   actually recorded in the manifest, so `init --name foo` followed by plain
   `uninstall` silently left `foo` installed while claiming success.
4. `init`'s dry-run preview could diverge from the real write: the write pass
   re-rendered from a fresh disk read instead of reusing what was
   previewed/confirmed, so an external edit in that window got clobbered.

See `belay/cli/client_configs.py` and `belay/cli/main.py` (`_write_client_config`,
`init`, `uninstall`, `doctor`) for the fixes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from belay.cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "belay.wrap.json").write_text('{"target": {"tool": "x"}}\n', encoding="utf-8")


def _mcp_servers(target: Path) -> dict[str, object]:
    return json.loads(target.read_text(encoding="utf-8"))["mcpServers"]


def test_init_init_uninstall_restores_original_exact(tmp_path: Path) -> None:
    """P0 #1: init -> init -> uninstall must give back the exact pre-install file,
    not a file that still has belay in it."""
    target = tmp_path / ".mcp.json"
    original = '{"mcpServers": {"other": {"command": "echo"}}}\n'
    target.write_text(original, encoding="utf-8")

    result = runner.invoke(app, ["init", "--client", "claude-code", "--yes"])
    assert result.exit_code == 0, result.output
    assert "belay" in _mcp_servers(target)

    result = runner.invoke(app, ["init", "--client", "claude-code", "--yes"])
    assert result.exit_code == 0, result.output
    assert "belay" in _mcp_servers(target)

    result = runner.invoke(app, ["uninstall", "--client", "claude-code", "--yes"])
    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".mcp.json.belay-manifest.json").is_file()


def test_init_on_missing_file_then_uninstall_removes_file(tmp_path: Path) -> None:
    """P0 #2: a config that didn't exist before install should not exist after
    a clean uninstall -- not be left behind as an empty {"mcpServers": {}}."""
    target = tmp_path / ".mcp.json"
    assert not target.is_file()

    result = runner.invoke(app, ["init", "--client", "claude-code", "--yes"])
    assert result.exit_code == 0, result.output
    assert target.is_file()

    result = runner.invoke(app, ["uninstall", "--client", "claude-code", "--yes"])
    assert result.exit_code == 0, result.output
    assert not target.is_file()
    assert not (tmp_path / ".mcp.json.belay-manifest.json").is_file()


def test_custom_name_survives_external_modification_and_uninstall_removes_it(
    tmp_path: Path,
) -> None:
    """P0 #3: uninstall must use manifest.name (what was actually registered),
    not the --name default/flag -- otherwise a non-default --name at install
    time is never actually removed."""
    target = tmp_path / ".mcp.json"

    result = runner.invoke(
        app, ["init", "--client", "claude-code", "--name", "belay-safe", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "belay-safe" in _mcp_servers(target)

    # External modification since install (invalidates the after_hash match,
    # forcing surgical-only removal -- exercises the same code path as the
    # reviewer's "modification externa" scenario).
    doc = json.loads(target.read_text(encoding="utf-8"))
    doc["mcpServers"]["other-tool"] = {"command": "echo"}
    target.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    # No --name here on purpose: uninstall must find "belay-safe" on its own.
    result = runner.invoke(app, ["uninstall", "--client", "claude-code", "--yes"])
    assert result.exit_code == 0, result.output
    servers = _mcp_servers(target)
    assert "belay-safe" not in servers
    assert "other-tool" in servers


def test_dry_run_then_external_modification_before_confirm_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0 #4: if the config changes in the window between preview and the
    actual write (here: during the confirmation prompt), init must abort
    without writing anything, not silently clobber the external change."""
    target = tmp_path / ".mcp.json"
    original = '{"mcpServers": {"other": {"command": "echo"}}}\n'
    target.write_text(original, encoding="utf-8")

    def _confirm_but_race(*_args: object, **_kwargs: object) -> bool:
        # Simulate something else editing the file in the gap between the
        # preview render and the write pass (the same gap a --dry-run then a
        # separate confirmed run would straddle).
        target.write_text(
            '{"mcpServers": {"other": {"command": "echo"}, "sneaky": {}}}\n', encoding="utf-8"
        )
        return True

    monkeypatch.setattr(typer, "confirm", _confirm_but_race)

    result = runner.invoke(app, ["init", "--client", "claude-code"])
    assert result.exit_code != 0
    assert "changed after preview" in result.output

    servers = _mcp_servers(target)
    assert "belay" not in servers
    assert "sneaky" in servers  # the external edit itself must survive untouched
    assert not (tmp_path / ".mcp.json.belay-manifest.json").is_file()


def test_manifest_write_failure_rolls_back_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure writing the manifest must not leave belay 'installed' (config
    written) but unmanaged (no manifest to uninstall/doctor it by)."""
    target = tmp_path / ".mcp.json"
    original = '{"mcpServers": {"other": {"command": "echo"}}}\n'
    target.write_text(original, encoding="utf-8")

    import belay.cli.client_configs as client_configs

    def _boom(*_args: object, **_kwargs: object) -> Path:
        raise OSError("simulated disk failure writing manifest")

    monkeypatch.setattr(client_configs, "write_manifest", _boom)

    result = runner.invoke(app, ["init", "--client", "claude-code", "--yes"])
    assert result.exit_code != 0

    assert target.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".mcp.json.belay-manifest.json").is_file()


def test_doctor_reports_broken_when_manifest_present_but_entry_absent(tmp_path: Path) -> None:
    """A manifest existing is not proof the entry is still there -- if it was
    hand-edited out, doctor must say BROKEN, not claim it's still registered."""
    target = tmp_path / ".mcp.json"

    result = runner.invoke(app, ["init", "--client", "claude-code", "--yes"])
    assert result.exit_code == 0, result.output

    target.write_text('{"mcpServers": {}}\n', encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--client", "claude-code"])
    assert result.exit_code == 0, result.output
    assert "BROKEN" in result.output
    assert "unchanged since install" not in result.output
    assert "MODIFIED since install" not in result.output


def test_doctor_reports_other_mcp_servers_as_a_bypass_route(tmp_path: Path) -> None:
    """E18.4: any MCP server configured alongside belay is reachable by the
    agent's own client directly, outside belay's contract-enforcing proxy --
    `doctor` must surface that, not just report belay's own registration."""
    target = tmp_path / ".mcp.json"
    result = runner.invoke(app, ["init", "--client", "claude-code", "--yes"])
    assert result.exit_code == 0, result.output

    servers = _mcp_servers(target)
    servers["github"] = {"command": "npx", "args": ["mcp-github"]}
    target.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--client", "claude-code"])
    assert result.exit_code == 0, result.output
    assert "github" in result.output
    assert "not routed through belay" in result.output


def test_doctor_reports_no_bypass_note_when_belay_is_the_only_server(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--client", "claude-code", "--yes"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["doctor", "--client", "claude-code"])
    assert result.exit_code == 0, result.output
    assert "not routed through belay" not in result.output


def test_doctor_reports_bypass_servers_even_when_not_belay_managed(tmp_path: Path) -> None:
    """No manifest at all (never ran `belay init` here) still gets a bypass
    report -- there's real MCP server exposure to flag even when belay
    itself was never registered for this client."""
    target = tmp_path / ".mcp.json"
    target.write_text(json.dumps({"mcpServers": {"github": {"command": "npx"}}}), encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--client", "claude-code"])
    assert result.exit_code == 0, result.output
    assert "not belay-managed" in result.output
    assert "github" in result.output
