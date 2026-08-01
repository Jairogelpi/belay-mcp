"""Typer entry point for the `belay` CLI.

Subcommands (`wrap`, `run`, `plan`, `approvals`, `rewind`, `verify`) are
implemented incrementally in E3-E7; E0 only wired the app so `belay
--help` works. E3 adds `wrap` and `run` (spec §3, §4.6, Appendix C).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from belay.errors import BelayError

if TYPE_CHECKING:
    from belay.approvals.queue import ApprovalQueue
    from belay.ledger.store import LedgerStore

app = typer.Typer(
    name="belay",
    help="Belay: safe, reversible tool execution for AI agents.",
    no_args_is_help=True,
)


@app.callback()
def callback() -> None:
    """Belay: safe, reversible tool execution for AI agents.

    Subcommands (wrap, run, plan, approvals, rewind) land in later
    entregas; `verify` lands in E2 alongside the ledger.
    """


@app.command()
def verify(db: str = typer.Argument(..., help="Path to a Belay SQLite ledger file.")) -> None:
    """Verify a ledger's hash chain and step coherence (spec §9.2)."""
    from belay.ledger.store import LedgerStore
    from belay.ledger.verify import verify_chain, verify_coherence

    db_path = Path(db).resolve()
    store = LedgerStore(f"sqlite:///{db_path.as_posix()}")
    events = store.read_all()

    chain_report = verify_chain(events)
    coherence_report = verify_coherence(events)

    started = next((e for e in events if e.type == "session_started"), None)
    typer.echo(f"events: {len(events)}")
    if started is not None:
        typer.echo(f"initiated_by: {started.initiated_by}")
        if started.on_behalf_of is not None:
            typer.echo(f"on_behalf_of: {started.on_behalf_of}")
    if chain_report.ok:
        typer.echo("chain: OK")
    else:
        typer.echo(f"chain: FAILED ({'; '.join(chain_report.errors)})")
    if coherence_report.ok:
        typer.echo("coherence: OK")
    else:
        typer.echo(f"coherence: FAILED ({'; '.join(coherence_report.errors)})")

    if not (chain_report.ok and coherence_report.ok):
        raise typer.Exit(code=1)


@app.command("keygen")
def keygen(
    path: str = typer.Argument(..., help="Where to write the private Ed25519 signing key (PEM)."),
) -> None:
    """Generate an Ed25519 signing key for `verify-export` (spec/plan-v2 E13).

    The private key is written to `path`; a companion `<path>.pub` file
    holds the public key as hex text, for `verify-evidence --pubkey`.
    """
    from belay.ledger.signing import SigningKey

    key = SigningKey.generate()
    key.save(path)
    pub_path = f"{path}.pub"
    Path(pub_path).write_text(key.public_hex() + "\n", encoding="utf-8")
    typer.echo(f"private key -> {path} (keep this offline and secret)")
    typer.echo(f"public key  -> {pub_path} ({key.public_hex()})")


@app.command("verify-export")
def verify_export(
    session_id: str = typer.Argument(..., help="Session to export signed evidence for."),
    key: str = typer.Option(
        ..., "--key", help="Path to an Ed25519 private signing key (PEM, `belay keygen`)."
    ),
    db: str = typer.Option("belay.db", "--db", help="Ledger SQLite file path."),
    out: str = typer.Option(..., "--out", "-o", help="Where to write the signed evidence file."),
) -> None:
    """Export a self-contained, offline-verifiable signed evidence bundle (plan-v2 E13)."""
    from belay.ledger.signing import SigningKey, sign_session
    from belay.ledger.store import LedgerStore

    db_path = Path(db).resolve()
    store = LedgerStore(f"sqlite:///{db_path.as_posix()}")
    events = store.read(session_id)
    if not events:
        typer.echo(f"no events found for session {session_id!r} in {db_path}", err=True)
        raise typer.Exit(code=1)

    signing_key = SigningKey.load(key)
    bundle = sign_session(events, signing_key)
    Path(out).write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(
        f"signed evidence for session {session_id} ({len(events)} events) -> {out} "
        f"(public key {bundle.public_key})"
    )


@app.command("verify-evidence")
def verify_evidence_cmd(
    file: str = typer.Argument(..., help="Signed evidence file (from `belay verify-export`)."),
    pubkey: str = typer.Option(
        "",
        "--pubkey",
        help="Path to a trusted public key (hex text, `belay keygen`'s .pub file). "
        "If omitted, the public key embedded in the file is used -- weaker trust, "
        "since a tampered file could embed a matching forged key.",
    ),
) -> None:
    """Verify a signed evidence bundle -- needs ONLY this file (+ optional pubkey).

    No database, no network, no live Belay installation required (plan-v2 E13).
    """
    from belay.ledger.signing import SignedEvidence, verify_evidence

    bundle = SignedEvidence.model_validate_json(Path(file).read_text(encoding="utf-8"))
    trusted = Path(pubkey).read_text(encoding="utf-8").strip() if pubkey else None
    report = verify_evidence(bundle, trusted_public_key_hex=trusted)

    typer.echo(f"session: {bundle.session_id}")
    typer.echo(f"events: {bundle.event_count}")
    typer.echo(f"initiated_by: {bundle.initiated_by}")
    if bundle.on_behalf_of is not None:
        typer.echo(f"on_behalf_of: {bundle.on_behalf_of}")
    if report.ok:
        typer.echo("evidence: VALID (chain, coherence, signature, and summary all check out)")
    else:
        typer.echo(f"evidence: INVALID (failed at stage: {report.stage})")
        for e in report.errors:
            typer.echo(f"  - {e}")
        raise typer.Exit(code=1)


@app.command()
def wrap(
    server_dir: str = typer.Argument(
        ...,
        help="Directory of the upstream MCP server (must contain server.py), "
        "unless --command overrides the launch entirely.",
    ),
    contracts: list[str] = typer.Option(  # noqa: B008
        ..., "--contracts", help="Path to a contract document (repeatable)."
    ),
    command: str = typer.Option(
        "",
        "--command",
        help="Executable to launch the upstream server (e.g. 'npx'). "
        "Overrides the default 'python server_dir/server.py' launch -- "
        "use for non-Python MCP servers.",
    ),
    arg: list[str] = typer.Option(  # noqa: B008
        [],
        "--arg",
        help="Argument to pass to --command (repeatable, in order). "
        "Ignored if --command is not set.",
    ),
    unsafe_passthrough: str = typer.Option(
        "",
        "--unsafe-passthrough",
        help="Comma-separated tool names to allow through with no contract (spec §4.6).",
    ),
    db: str = typer.Option("belay.db", "--db", help="Ledger SQLite file path."),
    initiated_by: str = typer.Option(
        "unknown",
        "--initiated-by",
        help="Default identity (E14) that started sessions of this wrapped server. "
        "Never silently blank -- an unattributed session must say so explicitly.",
    ),
    on_behalf_of: str = typer.Option(
        "",
        "--on-behalf-of",
        help="Optional: the accountable identity this default initiator acts for (E14).",
    ),
    out: str = typer.Option(
        "belay.wrap.json", "--out", "-o", help="Where to write the wrap config."
    ),
) -> None:
    """Register an upstream MCP server + its contract set (spec §4.6, Appendix C)."""
    from belay.contracts.loader import load_contract_set
    from belay.proxy.config import UpstreamCommand, WrapConfig

    if command:
        upstream = UpstreamCommand(command=command, args=list(arg))
    else:
        server_path = Path(server_dir).resolve()
        entry = server_path / "server.py"
        if not entry.is_file():
            typer.echo(
                f"error: {entry} not found (expected an MCP server entry point, "
                "or pass --command for a non-Python server)",
                err=True,
            )
            raise typer.Exit(code=1)
        upstream = UpstreamCommand(command=sys.executable, args=[str(entry)])

    # Validate the contract set now so `wrap` fails fast on bad contracts,
    # rather than at first `run`.
    load_contract_set(contracts)

    tools = [t.strip() for t in unsafe_passthrough.split(",") if t.strip()]
    config = WrapConfig(
        upstream=upstream,
        contracts=[str(Path(c).resolve()) for c in contracts],
        unsafe_passthrough=tools,
        db=db,
        initiated_by=initiated_by,
        on_behalf_of=on_behalf_of or None,
    )
    config.save(out)
    typer.echo(f"wrote {out}")


_CLIENT_CONFIG_PATHS: dict[str, str] = {
    "claude-desktop": "~claude-desktop~",  # resolved per-OS below
    "claude-code": ".mcp.json",
    "cursor": ".cursor/mcp.json",
    "codex": "~codex~",  # resolved per-OS below
    "opencode": "opencode.json",
}


def _claude_desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        import os

        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))
        return Path(appdata) / "Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def _client_config_path(client: str, scope: str = "project") -> Path:
    if client == "claude-desktop":
        return _claude_desktop_config_path()
    if client == "codex":
        # Codex supports project-scoped config; default there (matches the
        # project wrap.json being registered), --scope user for ~/.codex/.
        if scope == "user":
            return Path.home() / ".codex" / "config.toml"
        return Path(".codex/config.toml").resolve()
    return Path(_CLIENT_CONFIG_PATHS[client]).resolve()


def _render_client_config(client: str, target: Path, wrap_path: Path, name: str) -> str:
    """Pure: compute the new config text for `client`, without touching disk.
    Raises `ValueError` on invalid existing content -- never partially applies."""
    import json

    command = sys.executable
    args = ["-m", "belay.cli.main", "run", "--config", str(wrap_path)]

    if client == "codex":
        from belay.cli.client_configs import render_codex_toml

        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        return render_codex_toml(existing, name, command, args)

    if client == "opencode":
        from belay.cli.client_configs import render_opencode_json

        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        return render_opencode_json(existing, name, [command, *args])

    doc: dict[str, object] = {}
    if target.is_file():
        text = target.read_text(encoding="utf-8").strip()
        if text:
            doc = json.loads(text)
    servers = doc.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{target}: 'mcpServers' is not an object")
    servers[name] = {"command": command, "args": args}
    return json.dumps(doc, indent=2) + "\n"


def _write_client_config(client: str, target: Path, name: str, new_text: str,
                          before_text: str | None,
                          extra_files: list[str] | None = None) -> None:
    """Write `new_text` to `target` and record a manifest for it. `before_text`
    must be the *actual current content* of `target` (or `None` if it doesn't
    exist) -- callers that previewed `new_text`/`before_text` earlier are
    responsible for re-verifying `target` still matches `before_text` right
    before calling this, so a write never silently clobbers a change that
    happened in the gap between preview and write (see `init`'s pre-write
    re-check).

    Writes go through `atomic_write_with_backup`/`atomic_write` (temp file +
    `os.replace`) -- a crash mid-write leaves the original file intact, never
    half-written. Also writes a `.belay-manifest.json` (before/after content
    hash, backup path, timestamp) that `belay uninstall`/`belay doctor` use to
    know whether the file has changed since, without guessing from content
    alone.

    Reinstall handling (running `init` again over a config `belay` already
    manages) is the subtle part, because `<target>.belay-backup` is a fixed
    path: naively re-backing-up on every install overwrites that file with
    whatever's on disk *right now* -- which, on a reinstall, already contains
    the belay entry. A later `uninstall` "restoring" that backup would then
    put back a belay-containing file and declare success. Three cases:

    - No prior manifest (first-ever install here): back up normally.
    - Prior manifest, and the file's current hash still matches what that
      manifest recorded as `after_hash` (nothing touched it since): this is a
      clean reinstall. Carry the *original* `before_hash`/`backup_path`
      forward unchanged instead of re-deriving them from the current
      (already-belay) content, and don't touch the backup file on disk --
      it's still the correct one.
    - Prior manifest, but the current hash doesn't match (something else
      edited the file since -- another MCP server added, etc.): the old
      backup no longer represents a safe full-revert target. Never let a
      future `uninstall` restore it wholesale -- record `backup_path=None` so
      `uninstall` always falls back to surgical (belay-entry-only) removal
      for this file from here on.
    """
    from belay.cli.client_configs import (
        atomic_write,
        atomic_write_with_backup,
        load_manifest,
        sha256_of,
        write_manifest,
    )

    prior_manifest = load_manifest(target)
    unchanged_reinstall = (
        prior_manifest is not None
        and before_text is not None
        and sha256_of(before_text) == prior_manifest.after_hash
    )
    if unchanged_reinstall:
        assert prior_manifest is not None  # narrows for type checkers
        record_before_hash = prior_manifest.before_hash
        record_backup_path = (
            Path(prior_manifest.backup_path) if prior_manifest.backup_path else None
        )
        atomic_write(target, new_text)
    elif prior_manifest is not None:
        record_before_hash = sha256_of(before_text) if before_text is not None else None
        record_backup_path = None
        atomic_write(target, new_text)
    else:
        record_before_hash = sha256_of(before_text) if before_text is not None else None
        record_backup_path = atomic_write_with_backup(target, new_text)

    try:
        write_manifest(
            client, target, name, record_before_hash, new_text, record_backup_path,
            extra_files=extra_files,
        )
    except BaseException:
        # Never leave belay "installed" (config written) without the manifest
        # that makes it manageable -- put the config back exactly as it was.
        if before_text is not None:
            atomic_write(target, before_text)
        else:
            target.unlink(missing_ok=True)
        raise


def _register_client(client: str, wrap_path: Path, name: str, scope: str = "project") -> Path:
    """Merge a `belay` entry into one client's MCP config. Returns the config path touched."""
    target = _client_config_path(client, scope)
    before_text = target.read_text(encoding="utf-8") if target.is_file() else None
    new_text = _render_client_config(client, target, wrap_path, name)
    _write_client_config(client, target, name, new_text, before_text)
    return target


@app.command()
def detect() -> None:
    """Report which supported MCP clients (claude-code, cursor, codex, opencode,
    claude-desktop) are actually installed on this machine (plan-v2 E19.1).

    Read-only -- never writes anything. `belay init --client auto` uses this
    same detection to register Belay only where a real client was found,
    rather than blindly writing config for a tool that isn't installed here.
    """
    from belay.cli.host_detection import detect_all_clients

    detected = detect_all_clients(claude_desktop_config_dir=_claude_desktop_config_path().parent)
    for name, d in detected.items():
        path_note = f" ({d.binary_path})" if d.binary_path else ""
        if d.installed and d.version:
            typer.echo(f"{name}: detected -- {d.version}{path_note}")
        elif d.installed:
            typer.echo(f"{name}: detected{path_note}")
        else:
            typer.echo(f"{name}: not detected")


@app.command()
def init(
    client: str = typer.Option(
        ...,
        "--client",
        help="MCP client(s) to register Belay with, comma-separated: claude-desktop, "
        "claude-code, cursor, codex, opencode, 'all' (every supported type, "
        "installed or not -- e.g. preparing committed config for teammates), or "
        "'auto' (E19.1: only clients actually detected installed on this machine, "
        "see `belay detect`).",
    ),
    config: str = typer.Option(
        "belay.wrap.json",
        "--config",
        "-c",
        help="Wrap config path written by `belay wrap` (must already exist).",
    ),
    name: str = typer.Option(
        "belay", "--name", help="MCP server name the client will list Belay under."
    ),
    scope: str = typer.Option(
        "project",
        "--scope",
        help="For --client codex: 'project' (.codex/config.toml, default -- matches "
        "the project's own wrap.json) or 'user' (~/.codex/config.toml). Ignored by "
        "other clients (their scope is fixed by their own convention).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show which files would change and how, write nothing."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt (for CI/scripts)."
    ),
) -> None:
    """Register Belay as an MCP server in one or more clients' configs (no manual JSON).

    Merges into each client's existing config -- other servers the agent
    already talks to are left untouched, so the agent sees Belay alongside
    its other tools rather than in place of them. Writes are atomic (temp
    file + rename) with a `.belay-backup` and a `.belay-manifest.json`
    (content hashes, for `belay uninstall`/`belay doctor`) alongside anything
    overwritten. Shows every file that would change and asks for one
    confirmation before touching any of them, unless `--yes`.
    """
    import re

    if scope not in ("project", "user"):
        typer.echo(f"error: --scope must be 'project' or 'user', got {scope!r}", err=True)
        raise typer.Exit(code=1)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        typer.echo(
            f"error: --name {name!r} must be 1-64 characters of letters, digits, "
            "'-', or '_' (it becomes a config key/table name)",
            err=True,
        )
        raise typer.Exit(code=1)

    if client == "auto":
        from belay.cli.host_detection import detect_all_clients

        desktop_dir = _claude_desktop_config_path().parent
        detected = detect_all_clients(claude_desktop_config_dir=desktop_dir)
        # NOT `name` -- that's this function's own `--name` parameter (the
        # server name to register under, default "belay"); a P0 caught in
        # this exact spot shadowed it with the loop variable here, silently
        # overwriting it to whichever detected client happened to be last in
        # iteration order before it was ever used to render/write anything.
        clients = [client_name for client_name, d in detected.items() if d.installed]
        for client_name, d in detected.items():
            status = f"detected ({d.version})" if d.installed and d.version else (
                "detected" if d.installed else "not detected"
            )
            typer.echo(f"{client_name}: {status}")
        if not clients:
            typer.echo(
                "error: --client auto detected no supported clients installed on this "
                "machine -- pass --client explicitly (or 'all' to register regardless "
                "of detection)",
                err=True,
            )
            raise typer.Exit(code=1)
    else:
        clients = (
            list(_CLIENT_CONFIG_PATHS)
            if client == "all"
            else [c.strip() for c in client.split(",")]
        )
        for c in clients:
            if c not in _CLIENT_CONFIG_PATHS:
                typer.echo(
                    f"error: unknown --client {c!r} (expected one of: "
                    f"{', '.join(_CLIENT_CONFIG_PATHS)}, 'all', or 'auto')",
                    err=True,
                )
                raise typer.Exit(code=1)

    wrap_path = Path(config).resolve()
    if not wrap_path.is_file():
        typer.echo(
            f"error: {wrap_path} not found -- run `belay wrap` first to create it", err=True
        )
        raise typer.Exit(code=1)

    # Preview pass: render every client's new config text without writing anything,
    # so the confirmation below (and --dry-run) reflect exactly what will happen.
    # Captures `before_text` too, so the pre-write re-check below and the actual
    # write both use this exact snapshot -- the write pass never re-renders from
    # a fresh disk read, so it cannot diverge from what was previewed/confirmed.
    previews: list[tuple[str, Path, str | None, str]] = []
    for c in clients:
        target = _client_config_path(c, scope)
        before_text = target.read_text(encoding="utf-8") if target.is_file() else None
        try:
            new_text = _render_client_config(c, target, wrap_path, name)
        except ValueError as exc:
            typer.echo(f"error rendering {c}: {exc} -- nothing was written", err=True)
            raise typer.Exit(code=1) from None
        previews.append((c, target, before_text, new_text))

    typer.echo(f"this will register '{name}' in {len(previews)} config file(s):")
    for c, target, before_text, _ in previews:
        exists = "update" if before_text is not None else "create"
        typer.echo(f"  {exists}: {target}  ({c})")

    if dry_run:
        typer.echo("--dry-run: nothing written")
        return

    if not yes:
        typer.confirm("Proceed?", abort=True)

    # Re-verify nothing changed on disk since the preview above, before writing
    # anything. All-or-nothing: if any file moved since preview, abort without
    # writing to any of them, rather than risk silently clobbering whatever
    # changed it (another process, the user, etc.) in that window.
    for c, target, before_text, _ in previews:
        current_text = target.read_text(encoding="utf-8") if target.is_file() else None
        if current_text != before_text:
            typer.echo(
                f"error: {target} ({c}) changed after preview -- aborting without writing anything",
                err=True,
            )
            raise typer.Exit(code=1)

    for c, target, before_text, new_text in previews:
        try:
            _write_client_config(c, target, name, new_text, before_text)
        except ValueError as exc:
            typer.echo(f"error registering {c}: {exc} -- nothing was written", err=True)
            raise typer.Exit(code=1) from None
        typer.echo(f"registered '{name}' in {target}")
    typer.echo("restart the client(s) for the change to take effect")


@app.command()
def uninstall(
    client: str = typer.Option(
        ...,
        "--client",
        help="MCP client(s) to remove Belay from, comma-separated, or 'all'.",
    ),
    scope: str = typer.Option("project", "--scope", help="Same meaning as `belay init --scope`."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Remove Belay's entry from one or more clients' MCP configs.

    Uses the `.belay-manifest.json` written by `belay init` to decide how --
    including which entry name to remove (`manifest.name`, i.e. whatever
    `belay init --name ...` actually registered; there is deliberately no
    `--name` override here, since guessing wrong would silently leave the
    real entry installed while claiming success):

    - Current content hash matches the manifest's `after_hash` (nothing else
      touched the file since install) and `before_hash` is `None` (the file
      didn't exist before `belay init` created it): delete the file --
      belay's the only reason it exists.
    - Current content hash matches `after_hash` and a backup is on record:
      restore that pre-install backup in full, byte-for-byte.
    - Anything else (hash mismatch, i.e. the file changed since install --
      another MCP server added, some other edit -- or no backup was ever
      safe to trust, e.g. an install over an externally-modified file):
      surgically remove only the belay entry itself, leaving everything else
      exactly as it is now. Nothing is guessed: the manifest is the one
      source of truth.
    """
    from belay.cli.client_configs import (
        Manifest,
        atomic_restore,
        atomic_write_with_backup,
        load_manifest,
        manifest_path,
        remove_codex_entry,
        remove_json_mcp_entry,
        sha256_of,
    )

    clients = (
        list(_CLIENT_CONFIG_PATHS) if client == "all" else [c.strip() for c in client.split(",")]
    )
    # (client, target, action, manifest-or-None)
    plan: list[tuple[str, Path, str, Manifest | None]] = []
    for c in clients:
        if c not in _CLIENT_CONFIG_PATHS:
            typer.echo(f"error: unknown --client {c!r}", err=True)
            raise typer.Exit(code=1)
        target = _client_config_path(c, scope)
        if not target.is_file():
            plan.append((c, target, "skip (no config file)", None))
            continue
        manifest = load_manifest(target)
        if manifest is None:
            plan.append(
                (c, target, "skip (no belay manifest -- was this installed by belay?)", None)
            )
            continue
        current_hash = sha256_of(target.read_text(encoding="utf-8"))
        if current_hash != manifest.after_hash:
            action = f"surgically remove only the '{manifest.name}' entry"
        elif manifest.before_hash is None:
            action = "delete file (belay created it; nothing else ever touched it)"
        elif manifest.backup_path:
            action = "restore full pre-install backup"
        else:
            action = f"surgically remove only the '{manifest.name}' entry"
        plan.append((c, target, action, manifest))

    typer.echo(f"this will modify {len([p for p in plan if 'skip' not in p[2]])} config file(s):")
    for c, target, action, _ in plan:
        typer.echo(f"  {target} ({c}): {action}")

    if not yes:
        typer.confirm("Proceed?", abort=True)

    for c, target, action, manifest in plan:
        if action.startswith("skip"):
            continue
        assert manifest is not None
        if action.startswith("delete file"):
            target.unlink()
        elif action.startswith("restore"):
            assert manifest.backup_path is not None
            atomic_restore(target, Path(manifest.backup_path))
        else:
            existing = target.read_text(encoding="utf-8")
            if c == "codex":
                new_text = remove_codex_entry(existing, manifest.name)
            elif c == "opencode":
                new_text = remove_json_mcp_entry(existing, manifest.name, key="mcp")
            else:
                new_text = remove_json_mcp_entry(existing, manifest.name)
            atomic_write_with_backup(target, new_text)
        manifest_path(target).unlink(missing_ok=True)
        typer.echo(f"removed belay from {target}")


@app.command()
def disable_bypass(
    client: str = typer.Argument(
        ...,
        help="Client whose config to edit: claude-desktop, claude-code, cursor, codex, "
        "opencode.",
    ),
    name: str = typer.Argument(..., help="The MCP server name to remove (must not be 'belay')."),
    scope: str = typer.Option("project", "--scope", help="Same meaning as `belay init --scope`."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Remove one non-belay MCP server entry from a client's config (plan-v2 E19.2)
    -- the write half of `belay doctor`'s read-only bypass detection (E18.4).

    Does NOT attempt to re-wrap the removed server through Belay -- that
    would require guessing its command/args to reconstruct a `belay wrap`
    config for it, a much bigger and riskier automatic action than this
    command makes. This only closes the direct, ungated route: the agent
    simply won't see that server through this client anymore, until/unless
    someone wraps it and registers it through Belay properly (`belay wrap`
    + `belay init`).

    Same safety guarantees as `belay uninstall`: atomic write with backup,
    preview + one confirmation, a pre-write re-check that aborts if the
    file changed since the preview instead of clobbering it. Refuses to
    touch belay's own entry -- use `belay uninstall` for that.
    """
    from belay.cli.client_configs import (
        atomic_write_with_backup,
        entry_present,
        remove_codex_entry,
        remove_json_mcp_entry,
    )

    if client not in _CLIENT_CONFIG_PATHS:
        typer.echo(
            f"error: unknown client {client!r} (expected one of: "
            f"{', '.join(_CLIENT_CONFIG_PATHS)})",
            err=True,
        )
        raise typer.Exit(code=1)
    if name == "belay":
        typer.echo(
            "error: won't remove belay's own entry this way -- use `belay uninstall` instead",
            err=True,
        )
        raise typer.Exit(code=1)

    target = _client_config_path(client, scope)
    if not target.is_file():
        typer.echo(f"{client}: {target} does not exist -- nothing to do")
        return

    before_text = target.read_text(encoding="utf-8")
    try:
        present = entry_present(client, before_text, name)
    except (ValueError, LookupError) as exc:
        typer.echo(f"error: {target} does not parse as {client}'s config format: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if not present:
        typer.echo(f"{client}: {name!r} is not registered in {target} -- nothing to do")
        return

    if client == "codex":
        new_text = remove_codex_entry(before_text, name)
    elif client == "opencode":
        new_text = remove_json_mcp_entry(before_text, name, key="mcp")
    else:
        new_text = remove_json_mcp_entry(before_text, name)

    typer.echo(f"this will remove {name!r} from {target}")
    if not yes:
        typer.confirm("Proceed?", abort=True)

    if target.read_text(encoding="utf-8") != before_text:
        typer.echo(
            f"error: {target} changed after preview -- aborting without writing anything",
            err=True,
        )
        raise typer.Exit(code=1)

    atomic_write_with_backup(target, new_text)
    typer.echo(f"removed {name!r} from {target}")


@app.command()
def doctor(
    client: str = typer.Option(
        "all", "--client", help="MCP client(s) to check, comma-separated, or 'all' (default)."
    ),
    scope: str = typer.Option("project", "--scope", help="Same meaning as `belay init --scope`."),
) -> None:
    """Report whether each client's config is registered, modified since, missing,
    or BROKEN.

    Read-only -- never writes anything. For each client: whether Belay is
    actually registered there right now, whether the file has changed since
    `belay init` ran (per the recorded manifest hash), and whether a
    pre-install backup exists to restore from. A manifest existing is not
    proof the entry is still there -- if the file (or just the belay table/
    key within it) was hand-edited so the entry itself is gone, that's
    reported as BROKEN rather than "registered", since `belay uninstall`
    would otherwise have nothing to actually remove.
    """
    from belay.cli.client_configs import entry_present, list_server_names, load_manifest, sha256_of

    clients = (
        list(_CLIENT_CONFIG_PATHS) if client == "all" else [c.strip() for c in client.split(",")]
    )
    for c in clients:
        if c not in _CLIENT_CONFIG_PATHS:
            typer.echo(f"{c}: unknown client", err=True)
            continue
        target = _client_config_path(c, scope)
        if not target.is_file():
            typer.echo(f"{c}: not configured ({target} does not exist)")
            continue
        current_text = target.read_text(encoding="utf-8")
        try:
            all_servers = list_server_names(c, current_text)
        except (ValueError, LookupError):
            all_servers = []  # config doesn't parse as this client's format; nothing to report
        manifest = load_manifest(target)
        if manifest is None:
            typer.echo(f"{c}: config exists at {target}, but no belay manifest (not belay-managed)")
            if all_servers:
                typer.echo(
                    f"{c}:   {len(all_servers)} MCP server(s) configured here, reachable "
                    f"outside belay's proxy (not belay-managed, so this can't tell which -- if "
                    f"any -- of them is belay itself): {', '.join(sorted(all_servers))}"
                )
            continue
        other_servers = [n for n in all_servers if n != manifest.name]
        try:
            present = entry_present(c, current_text, manifest.name)
        except (ValueError, LookupError):
            present = False  # config no longer parses as this client's format
        if not present:
            typer.echo(
                f"{c}: BROKEN -- manifest says '{manifest.name}' should be registered at "
                f"{target}, but that entry is not there"
            )
            continue
        current_hash = sha256_of(current_text)
        if current_hash == manifest.after_hash:
            status = "unchanged since install"
        else:
            status = "MODIFIED since install"
        backup_note = "backup available" if manifest.backup_path else "no backup (file was new)"
        typer.echo(f"{c}: registered at {target} -- {status}, {backup_note}")
        if other_servers:
            typer.echo(
                f"{c}:   {len(other_servers)} other MCP server(s) configured here, reachable "
                f"directly by the agent's own client -- not routed through belay's contract "
                f"enforcement: {', '.join(sorted(other_servers))} "
                f"(remove one with `belay disable-bypass {c} <name>`)"
            )


@app.command()
def repair(
    config: str = typer.Option(
        "belay.wrap.json",
        "--config",
        "-c",
        help="Wrap config path -- same requirement as `belay init`, needed to "
        "re-render any broken MCP client registration.",
    ),
    client: str = typer.Option(
        "all",
        "--client",
        help="Which MCP clients to consider repairing, comma-separated, or 'all'.",
    ),
    scope: str = typer.Option("project", "--scope", help="Same meaning as `belay init --scope`."),
    db: str = typer.Option(
        "belay-hooks.db",
        "--db",
        help="Hooks db anchor -- same meaning as `belay hooks install --db`.",
    ),
    hooks: bool = typer.Option(
        True, "--hooks/--no-hooks", help="Also repair Claude Code hooks if they're broken."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Detect and restore any belay-managed registration that's gone BROKEN
    (plan-v2 E19.4) -- the manifest says an entry should be registered, but
    it's been hand-removed from the config since. Same detection `belay
    doctor`/`belay hooks doctor` already report; this orchestrates repairing
    everything broken in one command instead of requiring doctor -> read the
    output -> manually re-run `init`/`hooks install` yourself for each one.

    Reuses `init`'s/`hooks install`'s own atomic-write + backup + manifest
    machinery for the actual write -- this command only figures out WHICH
    registrations need repairing and drives that, it does not reimplement
    how one gets written. Unregistered (never installed) or already-healthy
    clients are left alone; this never touches anything that isn't BROKEN.
    """
    from belay.cli.client_configs import (
        claude_hooks_entry_present,
        entry_present,
        load_manifest,
        render_claude_hooks_settings,
    )

    clients = (
        list(_CLIENT_CONFIG_PATHS) if client == "all" else [c.strip() for c in client.split(",")]
    )
    for c in clients:
        if c not in _CLIENT_CONFIG_PATHS:
            typer.echo(f"error: unknown --client {c!r}", err=True)
            raise typer.Exit(code=1)

    broken_mcp: list[tuple[str, str]] = []  # (client, name)
    for c in clients:
        target = _client_config_path(c, scope)
        if not target.is_file():
            continue
        manifest = load_manifest(target)
        if manifest is None:
            continue
        current_text = target.read_text(encoding="utf-8")
        try:
            present = entry_present(c, current_text, manifest.name)
        except (ValueError, LookupError):
            present = False
        if not present:
            broken_mcp.append((c, manifest.name))

    broken_hooks = False
    hooks_target = _claude_settings_path(scope)
    if hooks and hooks_target.is_file():
        hooks_manifest = load_manifest(hooks_target)
        if hooks_manifest is not None:
            current_text = hooks_target.read_text(encoding="utf-8")
            try:
                present_by_event = {
                    hook_event: claude_hooks_entry_present(current_text, event=hook_event)
                    for hook_event in _HOOKS_EVENTS
                }
            except (ValueError, LookupError):
                present_by_event = dict.fromkeys(_HOOKS_EVENTS, False)
            broken_hooks = not all(present_by_event.values())

    if not broken_mcp and not broken_hooks:
        typer.echo("nothing broken -- every belay-managed registration checked out fine")
        return

    typer.echo("this will repair:")
    for c, name in broken_mcp:
        typer.echo(f"  {c}: re-register '{name}'")
    if broken_hooks:
        typer.echo(f"  claude-code hooks: re-register {'/'.join(_HOOKS_EVENTS)}")
    if not yes:
        typer.confirm("Proceed?", abort=True)

    wrap_path = Path(config).resolve() if broken_mcp else None
    if broken_mcp and (wrap_path is None or not wrap_path.is_file()):
        typer.echo(
            f"error: {wrap_path} not found -- run `belay wrap` first, or pass the right "
            "--config, to repair MCP registrations",
            err=True,
        )
        raise typer.Exit(code=1)

    for c, name in broken_mcp:
        assert wrap_path is not None
        target = _client_config_path(c, scope)
        before_text = target.read_text(encoding="utf-8") if target.is_file() else None
        new_text = _render_client_config(c, target, wrap_path, name)
        _write_client_config(c, target, name, new_text, before_text)
        typer.echo(f"repaired {c}: re-registered '{name}' at {target}")

    if broken_hooks:
        before_text = hooks_target.read_text(encoding="utf-8") if hooks_target.is_file() else None
        new_text = before_text or ""
        for hook_event in _HOOKS_EVENTS:
            new_text = render_claude_hooks_settings(
                new_text, _hooks_command_for(db, hook_event), event=hook_event
            )
        _write_client_config(
            "claude-code-hooks", hooks_target, "belay-hooks", new_text, before_text
        )
        typer.echo(f"repaired claude-code hooks: re-registered {'/'.join(_HOOKS_EVENTS)}")


hooks_app = typer.Typer(
    name="hooks",
    help="Native Agent Gate (plan-v2 E18): PreToolUse/PostToolUse hooks gating native "
    "Bash/file-edit/MCP tool calls the same way the MCP proxy gates calls made through "
    "it. First slice -- Claude Code only; Codex/Cursor/OpenCode are not yet wired up "
    "(said plainly, not implied).",
    no_args_is_help=True,
)
app.add_typer(hooks_app, name="hooks")

_HOOKS_SUPPORTED_CLIENTS = ("claude-code",)

_DB_ANCHOR_HELP = (
    "Project identity anchor (a path unique to this project/install). Despite the "
    "flag name, this is never opened directly as a database -- it's hashed into a "
    "private storage location under this user's belay home directory, so the project "
    "(and anything running inside it) can't reach the real approvals data. Pick any "
    "stable path; the conventional default works for that purpose without meaning "
    "anything else."
)


def _claude_settings_path(scope: str = "project") -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    return Path(".claude/settings.json").resolve()


_HOOKS_EVENTS = ("PreToolUse", "PostToolUse")


def _hooks_command_for(db: str, event: str) -> str:
    db_path = Path(db).resolve()
    return f'"{sys.executable}" -m belay.cli.main hooks run {event} --db "{db_path}"'


@hooks_app.command("run")
def hooks_run(
    event: str = typer.Argument(..., help="Hook event name from the agent, e.g. PreToolUse."),
    db: str = typer.Option("belay-hooks.db", "--db", help=_DB_ANCHOR_HELP),
    host: str = typer.Option(
        "claude-code", "--host", help="Which host adapter normalizes this event."
    ),
) -> None:
    """Hook entrypoint: reads the calling agent's JSON payload from stdin, sends it to
    this install's local supervisor (spawning it on demand if it isn't already running --
    ARCH-001/008, see docs/adr/0020-extended-requirement-catalog.md), prints the response
    JSON to stdout, exits 0. Not for direct human
    use -- this is what `belay hooks install` points the agent's own hook config at.

    PreToolUse gets a real decision (allow/deny). PostToolUse only records
    evidence (result, duration, output digest) to the durable ledger -- the
    tool already ran by then, there's no decision left to make, so the
    response is an empty ack. Malformed stdin, or an event this slice
    doesn't handle yet (anything but those two), exits 0 with no output --
    normal flow applies. If the supervisor can't be reached at all, the
    connection itself fails closed (deny) rather than this command crashing
    or hanging.
    """
    import json

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    if event not in _HOOKS_EVENTS:
        return

    from belay.supervisor.addressing import supervisor_identity
    from belay.supervisor.client import send_hook_event

    db_path = Path(db).resolve()
    identity = supervisor_identity(db_path)
    result = send_hook_event(identity, str(db_path), host, payload)
    typer.echo(json.dumps(result))


supervisor_app = typer.Typer(
    name="supervisor",
    help="The per-install local supervisor (ARCH-001) that `belay hooks run` talks "
    "to. Rarely invoked directly -- `belay hooks run` starts one on demand.",
    no_args_is_help=True,
)
app.add_typer(supervisor_app, name="supervisor")


@supervisor_app.command("serve")
def supervisor_serve(
    db: str = typer.Option("belay-hooks.db", "--db", help=_DB_ANCHOR_HELP),
) -> None:
    """Run the supervisor in the foreground (blocks). `belay hooks run` normally spawns
    this itself, detached, when no supervisor is already listening -- direct use is for
    debugging or explicit service-manager integration."""
    import logging

    from belay.supervisor.addressing import supervisor_identity
    from belay.supervisor.server import Supervisor

    logging.basicConfig(level=logging.INFO)
    identity = supervisor_identity(Path(db).resolve())
    Supervisor(identity).serve_forever()


@supervisor_app.command("status")
def supervisor_status(
    db: str = typer.Option("belay-hooks.db", "--db", help=_DB_ANCHOR_HELP),
) -> None:
    """Report whether this install's supervisor is currently reachable."""
    from belay.supervisor.addressing import supervisor_identity
    from belay.supervisor.lifecycle import is_listening

    identity = supervisor_identity(Path(db).resolve())
    if is_listening(identity):
        typer.echo(f"running -- listening on {identity.address}")
        typer.echo(f"data: {identity.data_path}")
    else:
        typer.echo("not running")
        raise typer.Exit(code=1)


@supervisor_app.command("stop")
def supervisor_stop(
    db: str = typer.Option("belay-hooks.db", "--db", help=_DB_ANCHOR_HELP),
) -> None:
    """Ask this install's supervisor to stop, if one is running."""
    from belay.supervisor.addressing import supervisor_identity
    from belay.supervisor.client import send_shutdown

    identity = supervisor_identity(Path(db).resolve())
    if send_shutdown(identity):
        typer.echo(f"stopped supervisor for {identity.data_path}")
    else:
        typer.echo("not running")


@hooks_app.command("install")
def hooks_install(
    client: str = typer.Option(
        "claude-code", "--client", help="Agent(s) to install the gate for, comma-separated."
    ),
    scope: str = typer.Option(
        "project",
        "--scope",
        help="'project' (.claude/settings.json, default) or 'user' (~/.claude/settings.json).",
    ),
    db: str = typer.Option("belay-hooks.db", "--db", help=_DB_ANCHOR_HELP),
    contracts: str | None = typer.Option(
        None,
        "--contracts",
        help="Opt-in (R1 first slice): a contracts YAML/JSON file. When set, native "
        "Edit/Write/NotebookEdit calls are resolved against it the same way belay run's "
        "MCP proxy resolves a tool -- no matching contract denies (contract_missing) "
        "instead of allowing by default. Omit for today's unchanged behavior.",
    ),
    quota_max: int | None = typer.Option(
        None,
        "--quota-max",
        help="Opt-in (R1 fourth slice): once this OS user has this many *approved* "
        "hook-gated actions within --quota-window, a new pause-worthy action is denied "
        "outright instead of being queued -- an operator must intervene directly. "
        "Requires --quota-window too. Omit for today's unchanged behavior (no cap).",
    ),
    quota_window: str = typer.Option(
        "1d",
        "--quota-window",
        help="Rolling window for --quota-max, e.g. '1d', '12h', '30m'. Ignored if "
        "--quota-max is not set.",
    ),
    allowlist_extra: str | None = typer.Option(
        None,
        "--allowlist-extra",
        help="Opt-in (R1 fifth slice): a text file, one literal safe command prefix per "
        "line ('#'-comments/blank lines ignored). A Bash command exactly matching an "
        "entry (or that entry followed by more arguments) is allowed instead of pausing "
        "-- additive only, never affects the built-in allowlist or the shell-metacharacter "
        "guard. Omit for today's unchanged behavior.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change, write nothing."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt (for CI/scripts)."
    ),
) -> None:
    """Register belay's PreToolUse and PostToolUse hooks in an agent's own hook config.

    Same safety guarantees as `belay init`: atomic write with backup, a
    `.belay-manifest.json` recording before/after hashes for `belay hooks
    uninstall`/`belay hooks doctor`, a preview + one confirmation before
    anything is written, and a TOCTOU re-check immediately before writing so
    an external edit in that window aborts the write instead of being
    clobbered.
    """
    from belay.cli.client_configs import render_claude_hooks_settings

    if scope not in ("project", "user"):
        typer.echo(f"error: --scope must be 'project' or 'user', got {scope!r}", err=True)
        raise typer.Exit(code=1)

    contracts_path: Path | None = None
    if contracts is not None:
        from belay.contracts.loader import load_contract_set
        from belay.errors import BelayError

        contracts_path = Path(contracts).resolve()
        try:
            load_contract_set([contracts_path])
        except BelayError as exc:
            typer.echo(
                f"error: --contracts {contracts_path} is invalid: {exc} -- nothing was written",
                err=True,
            )
            raise typer.Exit(code=1) from None
        except OSError as exc:
            typer.echo(
                f"error: could not read --contracts {contracts_path}: {exc} -- "
                "nothing was written",
                err=True,
            )
            raise typer.Exit(code=1) from None

    if quota_max is not None:
        from belay.policy.quota import parse_window

        try:
            parse_window(quota_window)
        except ValueError as exc:
            typer.echo(f"error: --quota-window: {exc} -- nothing was written", err=True)
            raise typer.Exit(code=1) from None
        if quota_max < 1:
            typer.echo(
                f"error: --quota-max must be at least 1, got {quota_max} -- nothing was written",
                err=True,
            )
            raise typer.Exit(code=1)

    allowlist_extra_path: Path | None = None
    if allowlist_extra is not None:
        from belay.hooks.decision import load_extra_allowlist

        allowlist_extra_path = Path(allowlist_extra).resolve()
        try:
            load_extra_allowlist(allowlist_extra_path)
        except ValueError as exc:
            typer.echo(
                f"error: --allowlist-extra {allowlist_extra_path} is invalid: {exc} -- "
                "nothing was written",
                err=True,
            )
            raise typer.Exit(code=1) from None
        except OSError as exc:
            typer.echo(
                f"error: could not read --allowlist-extra {allowlist_extra_path}: {exc} -- "
                "nothing was written",
                err=True,
            )
            raise typer.Exit(code=1) from None

    clients = [c.strip() for c in client.split(",")]
    for c in clients:
        if c not in _HOOKS_SUPPORTED_CLIENTS:
            typer.echo(
                f"error: unknown/unsupported --client {c!r} (only "
                f"{', '.join(_HOOKS_SUPPORTED_CLIENTS)} so far)",
                err=True,
            )
            raise typer.Exit(code=1)

    target = _claude_settings_path(scope)
    before_text = target.read_text(encoding="utf-8") if target.is_file() else None
    try:
        new_text = before_text or ""
        for hook_event in _HOOKS_EVENTS:
            new_text = render_claude_hooks_settings(
                new_text, _hooks_command_for(db, hook_event), event=hook_event
            )
    except ValueError as exc:
        typer.echo(f"error rendering {target}: {exc} -- nothing was written", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"this will register belay's {'/'.join(_HOOKS_EVENTS)} hooks in {target}")
    if dry_run:
        typer.echo("--dry-run: nothing written")
        return
    if not yes:
        typer.confirm("Proceed?", abort=True)

    current_text = target.read_text(encoding="utf-8") if target.is_file() else None
    if current_text != before_text:
        typer.echo(
            f"error: {target} changed after preview -- aborting without writing anything",
            err=True,
        )
        raise typer.Exit(code=1)

    from belay.supervisor.addressing import supervisor_identity

    identity = supervisor_identity(Path(db).resolve())
    # R1.6: `extra_files` always reflects exactly this call's flags -- a
    # reinstall that omits a flag it was previously given must record an
    # empty list here (see `write_manifest`'s docstring), and this
    # invocation itself deletes any stale pointer the omitted flag left
    # behind (below), rather than leaving it in place to silently keep
    # affecting a freshly (re)installed supervisor.
    extra_files: list[str] = []
    if contracts_path is not None:
        extra_files.append(str(identity.contracts_pointer_path))
    if quota_max is not None:
        extra_files.append(str(identity.quota_config_path))
    if allowlist_extra_path is not None:
        extra_files.append(str(identity.extra_allowlist_pointer_path))

    _write_client_config(
        "claude-code-hooks", target, "belay-hooks", new_text, before_text,
        extra_files=extra_files,
    )
    typer.echo(f"installed {'/'.join(_HOOKS_EVENTS)} hooks in {target}")

    typer.echo(
        f"approvals queued by this hook land in {identity.data_path} (private -- outside this "
        f"project, so the agent this gates can't reach it directly) -- review with "
        f"`belay hooks approvals list --db {db}` (NOT `belay approvals`, which opens "
        f"--db as a literal file rather than this project's identity anchor)"
    )
    if contracts_path is not None:
        identity.contracts_pointer_path.parent.mkdir(parents=True, exist_ok=True)
        identity.contracts_pointer_path.write_text(str(contracts_path), encoding="utf-8")
        typer.echo(
            f"native Edit/Write/NotebookEdit calls will now be checked against "
            f"{contracts_path} -- an undeclared tool denies (contract_missing) instead of "
            f"allowing by default. A running supervisor for this install must be restarted "
            f"(`belay supervisor stop --db {db}`) to pick this up."
        )
    else:
        # R1.6: a reinstall that no longer passes --contracts must not
        # leave a stale prior pointer file in place -- that would keep
        # gating native edits against a config this invocation never
        # asked for, indistinguishable from it still being intentionally
        # configured.
        identity.contracts_pointer_path.unlink(missing_ok=True)
    if quota_max is not None:
        import json

        identity.quota_config_path.parent.mkdir(parents=True, exist_ok=True)
        identity.quota_config_path.write_text(
            json.dumps({"max_actions": quota_max, "window": quota_window}), encoding="utf-8"
        )
        typer.echo(
            f"once an OS user has {quota_max} approved actions within {quota_window}, a new "
            f"pause-worthy action denies outright instead of being queued. A running "
            f"supervisor for this install must be restarted (`belay supervisor stop --db "
            f"{db}`) to pick this up."
        )
    else:
        identity.quota_config_path.unlink(missing_ok=True)
    if allowlist_extra_path is not None:
        identity.extra_allowlist_pointer_path.parent.mkdir(parents=True, exist_ok=True)
        identity.extra_allowlist_pointer_path.write_text(
            str(allowlist_extra_path), encoding="utf-8"
        )
        typer.echo(
            f"Bash commands matching an entry in {allowlist_extra_path} will now be allowed "
            f"instead of pausing. A running supervisor for this install must be restarted "
            f"(`belay supervisor stop --db {db}`) to pick this up."
        )
    else:
        identity.extra_allowlist_pointer_path.unlink(missing_ok=True)
    typer.echo("restart the agent for the hook to take effect")


@hooks_app.command("uninstall")
def hooks_uninstall(
    client: str = typer.Option(
        "claude-code", "--client", help="Agent(s) to remove the gate from, comma-separated."
    ),
    scope: str = typer.Option("project", "--scope", help="Same meaning as `belay hooks install`."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Remove belay's hook entry, same restore-vs-surgical logic as `belay uninstall`
    (spec-adjacent, plan-v2 E17/E18): unchanged since install -> restore the full
    pre-install backup; changed since (the user added their own hooks) ->
    surgically remove only belay's entry."""
    from belay.cli.client_configs import (
        atomic_restore,
        atomic_write_with_backup,
        load_manifest,
        manifest_path,
        remove_claude_hooks_entry,
        sha256_of,
    )

    clients = [c.strip() for c in client.split(",")]
    for c in clients:
        if c not in _HOOKS_SUPPORTED_CLIENTS:
            typer.echo(f"error: unknown/unsupported --client {c!r}", err=True)
            raise typer.Exit(code=1)

    target = _claude_settings_path(scope)
    if not target.is_file():
        typer.echo(f"{target} does not exist -- nothing to uninstall")
        return
    manifest = load_manifest(target)
    if manifest is None:
        typer.echo(f"{target}: no belay manifest -- was this installed by `belay hooks install`?")
        return

    current_hash = sha256_of(target.read_text(encoding="utf-8"))
    if current_hash != manifest.after_hash:
        action = "surgically remove only belay's hook entry"
    elif manifest.before_hash is None:
        action = "delete file"
    elif manifest.backup_path:
        action = "restore full pre-install backup"
    else:
        action = "surgically remove only belay's hook entry"

    typer.echo(f"{target}: {action}")
    if not yes:
        typer.confirm("Proceed?", abort=True)

    if action == "delete file":
        target.unlink()
    elif action.startswith("restore"):
        assert manifest.backup_path is not None
        atomic_restore(target, Path(manifest.backup_path))
    else:
        new_text = target.read_text(encoding="utf-8")
        for hook_event in _HOOKS_EVENTS:
            new_text = remove_claude_hooks_entry(new_text, event=hook_event)
        atomic_write_with_backup(target, new_text)
    # R1.6: remove the contracts/quota/allowlist pointer files this install
    # wrote (belay/supervisor/addressing.py, under belay_home() -- outside
    # `target`, so none of the settings.json actions above touch them).
    # Without this, they used to survive uninstall entirely and a later
    # bare reinstall (no flags) would silently pick the old ones back up.
    for extra_file in manifest.extra_files:
        Path(extra_file).unlink(missing_ok=True)
    manifest_path(target).unlink(missing_ok=True)
    typer.echo(f"removed belay's hooks from {target}")


def _deep_check_hook_command(command: str) -> list[str]:
    """Real reachability checks for one registered hook command string
    (plan-v2 E19.3) -- not just "the config file looks right", but "this
    would actually work if the host invoked it right now". Returns a list
    of problems found (empty means everything checked out).

    Side effect, said plainly rather than hidden: if the supervisor for
    the embedded --db anchor isn't already running, this spawns one on
    demand (`ensure_running`) to check reachability -- exactly what a real
    hook invocation would do anyway, not an extra action invented for this
    check. `--deep` trades "purely read-only" for "actually proves the
    thing works", the same tradeoff any real health check makes.
    """
    import re
    import subprocess

    issues: list[str] = []
    interpreter_match = re.match(r'"([^"]+)"', command)
    interpreter = interpreter_match.group(1) if interpreter_match else None
    if interpreter is None or not Path(interpreter).is_file():
        issues.append(f"interpreter not found on disk: {interpreter!r}")
        return issues  # nothing else here is checkable without a real interpreter

    try:
        import_check = subprocess.run(
            [interpreter, "-c", "import belay"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        issues.append(f"could not run {interpreter} to check belay is importable: {exc}")
    else:
        if import_check.returncode != 0:
            stderr = import_check.stderr.strip().splitlines()[-1] if import_check.stderr else ""
            issues.append(f"belay is not importable from {interpreter}: {stderr}")

    db_match = re.search(r'--db "([^"]+)"', command)
    if db_match is None:
        issues.append("could not find --db in the hook command to check supervisor reachability")
        return issues

    from belay.supervisor.addressing import supervisor_identity
    from belay.supervisor.lifecycle import ensure_running

    db_path = db_match.group(1)
    identity = supervisor_identity(Path(db_path))
    if not ensure_running(identity, db_path):
        issues.append("supervisor could not be reached or spawned for this install")
    return issues


@hooks_app.command("doctor")
def hooks_doctor(
    client: str = typer.Option(
        "claude-code", "--client", help="Agent(s) to check, comma-separated."
    ),
    scope: str = typer.Option("project", "--scope", help="Same meaning as `belay hooks install`."),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="E19.3: also verify the hook would actually work right now -- the "
        "registered interpreter exists and can import belay, and the supervisor "
        "for this install is genuinely reachable (spawning one on demand if "
        "needed, same as a real hook invocation would). Slower, and not purely "
        "read-only (may start a background process) -- omit for the fast, "
        "config-only check.",
    ),
) -> None:
    """Report whether belay's hook is registered, modified since, missing, or BROKEN.
    Read-only (config-only check), same semantics as `belay doctor`, unless
    `--deep` is passed."""
    from belay.cli.client_configs import claude_hooks_entry_present, load_manifest, sha256_of

    clients = [c.strip() for c in client.split(",")]
    for c in clients:
        if c not in _HOOKS_SUPPORTED_CLIENTS:
            typer.echo(f"{c}: unknown client", err=True)
            continue
        target = _claude_settings_path(scope)
        if not target.is_file():
            typer.echo(f"{c}: not configured ({target} does not exist)")
            continue
        manifest = load_manifest(target)
        if manifest is None:
            typer.echo(f"{c}: config exists at {target}, but no belay manifest (not belay-managed)")
            continue
        current_text = target.read_text(encoding="utf-8")
        try:
            present = {
                hook_event: claude_hooks_entry_present(current_text, event=hook_event)
                for hook_event in _HOOKS_EVENTS
            }
        except (ValueError, LookupError):
            present = dict.fromkeys(_HOOKS_EVENTS, False)
        missing = [hook_event for hook_event, ok in present.items() if not ok]
        if missing:
            typer.echo(
                f"{c}: BROKEN -- manifest says {'/'.join(missing)} should be registered at "
                f"{target}, but {'it isn' if len(missing) == 1 else 'they aren'}'t there"
            )
            continue
        current_hash = sha256_of(current_text)
        if current_hash == manifest.after_hash:
            status = "unchanged since install"
        else:
            status = "MODIFIED since install"
        backup_note = "backup available" if manifest.backup_path else "no backup (file was new)"
        typer.echo(
            f"{c}: {'/'.join(_HOOKS_EVENTS)} hooks registered at {target} -- {status}, "
            f"{backup_note}"
        )
        if deep:
            import json as _json

            doc = _json.loads(current_text)
            command = doc["hooks"][_HOOKS_EVENTS[0]][0]["hooks"][0]["command"]
            issues = _deep_check_hook_command(command)
            if issues:
                for issue in issues:
                    typer.echo(f"{c}:   DEEP CHECK FAILED -- {issue}")
            else:
                typer.echo(
                    f"{c}:   deep check OK -- interpreter, belay import, and supervisor "
                    "all reachable"
                )


@hooks_app.command("list-edits")
def hooks_list_edits(
    db: str = typer.Option("belay-hooks.db", "--db", help=_DB_ANCHOR_HELP),
) -> None:
    """List captured native file edits (E18.3), most recent first --
    `belay hooks rewind <event_id>` undoes one."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session as DBSession

    from belay.db.models import FileSnapshotRow
    from belay.supervisor.addressing import supervisor_identity

    data_path = supervisor_identity(Path(db).resolve()).data_path
    if not data_path.is_file():
        typer.echo("no captured edits (nothing installed/run yet)")
        return
    engine = create_engine(f"sqlite:///{data_path}", future=True)
    with DBSession(engine) as session:
        rows = session.scalars(
            select(FileSnapshotRow).order_by(FileSnapshotRow.captured_at.desc())
        ).all()
    if not rows:
        typer.echo("no captured edits")
        return
    for row in rows:
        typer.echo(
            f"{row.event_id}  {row.state:9s}  {row.path}  captured_at={row.captured_at}"
            + (f"  restored_at={row.restored_at}" if row.restored_at else "")
        )


@hooks_app.command("rewind")
def hooks_rewind(
    event_id: str = typer.Argument(..., help="The tool_use_id of a captured Edit/Write call."),
    db: str = typer.Option("belay-hooks.db", "--db", help=_DB_ANCHOR_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Undo one captured native file edit (E18.3, spec §9.2).

    Refuses (never silently overwrites) if the file has changed again since
    the edit this is rewinding -- run `belay hooks list-edits` to find an
    `event_id`, then this to restore that specific edit's pre-edit content
    (or delete the file, if it didn't exist before that edit)."""
    from sqlalchemy import create_engine

    from belay.hooks.file_snapshot import SnapshotStore
    from belay.supervisor.addressing import belay_home, supervisor_identity

    identity = supervisor_identity(Path(db).resolve())
    if not identity.data_path.is_file():
        typer.echo("no captured edits (nothing installed/run yet)", err=True)
        raise typer.Exit(code=1)

    engine = create_engine(f"sqlite:///{identity.data_path}", future=True)
    store = SnapshotStore(engine, belay_home() / "snapshots")
    snapshot = store.get(event_id)
    if snapshot is None:
        typer.echo(f"no captured edit found for event_id {event_id!r}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"this will rewind {event_id}: {snapshot.path} ({snapshot.state})")
    if not yes:
        typer.confirm("Proceed?", abort=True)

    outcome = store.restore(event_id)
    typer.echo(outcome)
    if "conflict" in outcome or "no snapshot" in outcome or "missing" in outcome:
        raise typer.Exit(code=1)


@hooks_app.command("fence")
def hooks_fence(
    host_session_id: str = typer.Argument(
        ..., help="The host session to fence -- see `belay hooks approvals list`'s "
        "session= column, or a host's own session id."
    ),
    host: str = typer.Option(
        "claude-code", "--host", help="Which host adapter's session this is."
    ),
    db: str = typer.Option("belay-hooks.db", "--db", help=_DB_ANCHOR_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Fence a hook session closed to new actions (R1 third slice, ADR 0021).

    Every surface (Bash, file edits, native MCP calls) is denied for this
    session from now on -- a ledger fact (`session_fenced`), the same
    mechanism `belay rewind` already uses to fence an MCP proxy session, so
    it holds even across a supervisor restart, not just in-process state.
    There is no `unfence`: start a new session with the agent instead of
    trying to un-close this one.
    """
    from belay.hooks.gate import session_key
    from belay.rewind.service import is_fenced

    ledger = _hooks_ledger_for(db)
    session_id = session_key(host, host_session_id)
    if is_fenced(ledger, session_id):
        typer.echo(f"{host_session_id} ({host}) is already fenced")
        return

    typer.echo(f"this will fence {host_session_id} ({host}) -- no new actions will be accepted")
    if not yes:
        typer.confirm("Proceed?", abort=True)

    ledger.append(session_id, "session_fenced", {})
    typer.echo(f"fenced {host_session_id} ({host})")


hooks_approvals_app = typer.Typer(
    name="approvals",
    help="Review/approve/reject items the Native Agent Gate queued (paused Bash "
    "commands, E18.4 native MCP calls). Distinct from the top-level `belay "
    "approvals` -- that group opens --db as a literal MCP-path ledger file; these "
    "resolve --db the same way `belay hooks run`/`rewind`/`list-edits` do (a "
    "project-identity anchor -> the private belay-home data file), since that's "
    "where hook-queued approvals actually live (E18.1: never inside the project).",
    no_args_is_help=True,
)
hooks_app.add_typer(hooks_approvals_app, name="approvals")


def _hooks_approval_queue(db: str) -> ApprovalQueue:
    from sqlalchemy import create_engine

    from belay.approvals.queue import ApprovalQueue
    from belay.supervisor.addressing import supervisor_identity

    data_path = supervisor_identity(Path(db).resolve()).data_path
    engine = create_engine(f"sqlite:///{data_path}", future=True)
    return ApprovalQueue(engine=engine)


def _hooks_ledger_for(db: str) -> LedgerStore:
    from sqlalchemy import create_engine

    from belay.ledger.store import LedgerStore
    from belay.supervisor.addressing import supervisor_identity

    data_path = supervisor_identity(Path(db).resolve()).data_path
    # `Supervisor.__init__` normally creates this directory as a side
    # effect of a prior `hooks run`/`hooks install` -- but `belay hooks
    # fence` (R1 third slice) can legitimately be the first hooks command
    # run for an install (e.g. fencing before an agent ever runs), so this
    # can't assume the directory already exists (real bug found writing
    # tests for `hooks fence`: sqlite3.OperationalError, "unable to open
    # database file").
    data_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{data_path}", future=True)
    return LedgerStore(engine=engine)


@hooks_approvals_app.command("list")
def hooks_approvals_list(
    db: str = typer.Option("belay-hooks.db", "--db", help=_DB_ANCHOR_HELP),
) -> None:
    """List every hook-queued approval item, oldest first."""
    items = _hooks_approval_queue(db).list()
    if not items:
        typer.echo("no approval items")
        return
    for item in items:
        typer.echo(
            f"{item.approval_id}  {item.state:9s}  plan={item.plan_id}  "
            f"tool={item.plan.get('tool')}  session={item.session_id}  "
            f"expires_at={item.expires_at.isoformat()}"
        )


@hooks_approvals_app.command("approve")
def hooks_approvals_approve(
    approval_id: str = typer.Argument(..., help="Approval item id."),
    reason: str = typer.Option("", "--reason", help="Optional human-readable reason."),
    by: str = typer.Option("", "--by", help="Approver identity; defaults to the OS user."),
    db: str = typer.Option("belay-hooks.db", "--db", help=_DB_ANCHOR_HELP),
) -> None:
    """Approve a pending hook-queued item (pending -> approved)."""
    import getpass

    from belay.errors import BelayError

    approver = by or getpass.getuser()
    queue = _hooks_approval_queue(db)
    try:
        item = queue.approve(approval_id, approved_by=approver, reason=reason or None)
    except BelayError as exc:
        typer.echo(f"error: {exc.code} ({exc.detail})", err=True)
        raise typer.Exit(code=1) from exc

    _hooks_ledger_for(db).append(
        item.session_id,
        "approval_resolved",
        {
            "approval_id": item.approval_id,
            "plan_id": item.plan_id,
            "state": "approved",
            "approved_by": approver,
            "reason": reason or None,
        },
        step_seq=item.step_seq,
    )
    typer.echo(f"{item.approval_id} approved by {approver}")


@hooks_approvals_app.command("reject")
def hooks_approvals_reject(
    approval_id: str = typer.Argument(..., help="Approval item id."),
    reason: str = typer.Option("", "--reason", help="Optional human-readable reason."),
    by: str = typer.Option("", "--by", help="Approver identity; defaults to the OS user."),
    db: str = typer.Option("belay-hooks.db", "--db", help=_DB_ANCHOR_HELP),
) -> None:
    """Reject a pending hook-queued item (pending -> rejected)."""
    import getpass

    from belay.errors import BelayError

    approver = by or getpass.getuser()
    queue = _hooks_approval_queue(db)
    try:
        item = queue.reject(approval_id, rejected_by=approver, reason=reason or None)
    except BelayError as exc:
        typer.echo(f"error: {exc.code} ({exc.detail})", err=True)
        raise typer.Exit(code=1) from exc

    _hooks_ledger_for(db).append(
        item.session_id,
        "approval_resolved",
        {
            "approval_id": item.approval_id,
            "plan_id": item.plan_id,
            "state": "rejected",
            "rejected_by": approver,
            "reason": reason or None,
        },
        step_seq=item.step_seq,
    )
    typer.echo(f"{item.approval_id} rejected by {approver}")


@app.command()
def dashboard(
    db: str = typer.Option("belay.db", "--db", help="Ledger SQLite file path."),
    out: str = typer.Option(
        "belay-dashboard.html", "--out", "-o", help="Where to write the dashboard HTML."
    ),
) -> None:
    """Render a static HTML snapshot of a ledger: sessions, steps, and approvals.

    No server, no live DB access from the page -- a snapshot, refreshed by
    re-running this command. Pending approvals show the exact `belay
    approvals approve/reject` command to run rather than a clickable button
    (spec §7/§12: approval is a CLI-only, human-typed action by design).
    """
    from belay.cli.dashboard import render_dashboard

    if not Path(db).is_file():
        typer.echo(f"error: {db} not found", err=True)
        raise typer.Exit(code=1)

    html = render_dashboard(db)
    Path(out).write_text(html, encoding="utf-8")
    typer.echo(f"wrote {out}")


@app.command()
def learn(
    approval_id: str = typer.Argument(..., help="A rejected approval (spec §7) to learn from."),
    db: str = typer.Option("belay.db", "--db", help="Ledger/approvals SQLite file path."),
    apply_kind: str = typer.Option(
        "",
        "--apply",
        help="Actually write a rule to --intent-contract: 'forbidden_tools' or "
        "'forbidden_scope'. Omit to only print the proposal (default: propose, never apply).",
    ),
    intent_contract: str = typer.Option(
        "",
        "--intent-contract",
        help="Intent contract YAML to write the rule into (created fresh if missing). "
        "Required with --apply.",
    ),
) -> None:
    """Compile a human's rejection into a durable, enforced rule -- never applied silently.

    Only two proposals are generated, both mechanical (no LLM interpreting
    the rejection reason): forbid the rejected tool outright, or forbid its
    file scope. Printed by default; `--apply <kind> --intent-contract
    <file>` is what actually writes it, and from then on every session
    loading that contract (`belay run --intent-contract`) enforces it --
    this compiles a decision into a control future sessions can't violate,
    not a note Belay merely remembers.
    """
    from belay.approvals.queue import ApprovalQueue
    from belay.intent.learn import apply_rule, propose_rule

    queue = ApprovalQueue(db_url=f"sqlite:///{Path(db).resolve().as_posix()}")
    item = queue.get(approval_id)
    if item is None:
        typer.echo(f"error: no approval item {approval_id!r} in {db}", err=True)
        raise typer.Exit(code=1)

    try:
        rules = propose_rule(item)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"rejection: {item.plan.get('tool')} -- reason: {item.reason or '(none recorded)'}")
    typer.echo("candidate rule(s) (nothing applied yet unless --apply is given):")
    for rule in rules:
        typer.echo(f"  [{rule.kind}] {rule.value}")

    if apply_kind:
        if not intent_contract:
            typer.echo("error: --apply requires --intent-contract <file>", err=True)
            raise typer.Exit(code=1)
        matching = [r for r in rules if r.kind == apply_kind]
        if not matching:
            typer.echo(f"error: no proposed rule of kind {apply_kind!r}", err=True)
            raise typer.Exit(code=1)
        apply_rule(intent_contract, matching[0])
        typer.echo(f"applied [{apply_kind}] {matching[0].value} -> {intent_contract}")


@app.command()
def explore(
    session_ids: list[str] = typer.Argument(  # noqa: B008
        ..., help="Two or more already-run session variants to compare (same task, same "
        "checkpoint -- Belay doesn't generate them, only governs and compares them)."
    ),
    db: str = typer.Option("belay.db", "--db", help="Ledger SQLite file path."),
    config: str = typer.Option(
        "",
        "--config",
        "-c",
        help="Wrap config path -- include to also count each variant's irreversible/"
        "indeterminate steps via a real `belay rewind --dry-run` plan.",
    ),
) -> None:
    """Compare N already-run session variants side by side. Never picks a winner.

    Belay has no agent of its own to generate variants -- this assembles a
    deterministic comparison table from data each session already produced
    (the causal graph, plan effects, optionally a real rewind dry-run plan
    per variant): steps, distinct files touched, tools used, steps proven
    by a test vs not, unknown effects, and irreversible/indeterminate step
    count. No LLM ranks these; a human picks from the evidence.
    """
    from belay.cli.explore import compute_metrics, render_table
    from belay.ledger.store import LedgerStore

    if len(session_ids) < 2:
        typer.echo("error: give at least two session_ids to compare", err=True)
        raise typer.Exit(code=1)

    ledger = LedgerStore(f"sqlite:///{Path(db).resolve().as_posix()}")

    rewind_service = None
    if config:
        from belay.contracts.loader import load_contract_set
        from belay.policy.model import default_policy
        from belay.proxy.config import WrapConfig
        from belay.rewind.service import RewindService

        wrap_config = WrapConfig.load(config)
        contract_set = load_contract_set(wrap_config.contracts)
        rewind_service = RewindService(
            ledger=ledger, policy=default_policy(), contract_set=contract_set
        )

    metrics = []
    for session_id in session_ids:
        events = ledger.read(session_id)
        if not events:
            typer.echo(f"error: no events found for session {session_id!r} in {db}", err=True)
            raise typer.Exit(code=1)

        irreversible_count = None
        if rewind_service is not None:
            import anyio

            async def _no_upstream(tool: str, args: dict[str, object]) -> dict[str, object]:
                raise AssertionError("explore dry-run must never call upstream")

            async def _dry_run_plan(sid: str = session_id) -> Any:
                return await rewind_service.rewind(sid, _no_upstream, dry_run=True, by="explore")

            report = anyio.run(_dry_run_plan)
            irreversible_count = len(report.plan.irreversible) + len(report.plan.indeterminate)

        metrics.append(compute_metrics(session_id, events, irreversible_count))

    typer.echo(render_table(metrics))


@app.command()
def causal(
    session_id: str = typer.Argument(..., help="Session to build the causal graph for."),
    db: str = typer.Option("belay.db", "--db", help="Ledger SQLite file path."),
    format: str = typer.Option(
        "text", "--format", help="Output format: text, mermaid, or json."
    ),
    out: str = typer.Option(
        "", "--out", "-o", help="Write to this file instead of stdout."
    ),
) -> None:
    """Show the causal graph of a session: what read/decided/produced/depends-on what.

    Not a new subsystem -- assembled entirely from ledger events already
    recorded (spec §9): `state_captured` for what a step read before acting,
    `plan_created`'s `intent_id`/`test_ref` tags (adoption/DX, see `belay
    rewind --intent`) for which subgoal/test motivated it, `policy_evaluated`
    for the decision, and same-`path` ordering for a same-resource
    `depends_on` heuristic -- not real data/control-flow analysis, just the
    nearest earlier step that touched the same file.
    """
    import json as jsonlib

    from belay.cli.causal import build_causal_graph, to_mermaid
    from belay.ledger.store import LedgerStore

    ledger = LedgerStore(f"sqlite:///{Path(db).resolve().as_posix()}")
    events = ledger.read(session_id)
    if not events:
        typer.echo(f"error: no events found for session {session_id!r} in {db}", err=True)
        raise typer.Exit(code=1)

    nodes = build_causal_graph(events)

    if format == "mermaid":
        text = to_mermaid(nodes, session_id)
    elif format == "json":
        text = jsonlib.dumps([vars(n) for n in nodes], indent=2)
    else:
        lines = [f"causal graph for {session_id}:"]
        for n in nodes:
            path = n.args.get("path")
            lines.append(f"  step {n.step_seq}: {n.tool}" + (f" ({path})" if path else ""))
            if n.intent_id:
                lines.append(f"    intent: {n.intent_id}")
            if n.test_verified is True:
                ev = n.test_evidence or {}
                label = "VERIFIED ON DIRTY TREE" if ev.get("git_dirty") else "VERIFIED"
                dirty_note = (
                    " -- tree_hash reflects HEAD, not the modified working tree; "
                    "not fully reproducible from the recorded hash alone"
                    if ev.get("git_dirty")
                    else ""
                )
                lines.append(
                    f"    {label} by test: {n.test_ref or ev.get('cmd')} "
                    f"(exit_code=0, output_hash={ev.get('output_hash')}){dirty_note}"
                )
            elif n.test_verified is False:
                ev = n.test_evidence or {}
                lines.append(
                    f"    test FAILED: {n.test_ref or ev.get('cmd')} "
                    f"(exit_code={ev.get('exit_code')})"
                )
            elif n.test_ref:
                lines.append(f"    claimed (never run): {n.test_ref}")
            if n.read_before is not None:
                lines.append(f"    read before deciding: {n.read_before}")
            if n.policy_verdict:
                lines.append(f"    policy: {n.policy_verdict} ({', '.join(n.policy_reasons)})")
            if n.depends_on:
                lines.append(f"    depends on step(s): {n.depends_on}")
            if n.status:
                lines.append(f"    status: {n.status}")
            if n.compensation_tool:
                lines.append(f"    undo via: {n.compensation_tool}")
        text = "\n".join(lines)

    if out:
        Path(out).write_text(text, encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text)


@app.command(name="verify-test")
def verify_test(
    session_id: str = typer.Argument(..., help="Session whose step is being verified."),
    step: int = typer.Option(..., "--step", help="step_seq (spec §9.1) this test proves."),
    runner: str = typer.Option(
        "",
        "--runner",
        help="Test runner ('pytest', 'jest', 'go') -- builds the command mechanically "
        "from the step's own `_belay_test_ref`, never a string typed at verify-test time. "
        "This is the only path that can record mode='test' (VERIFIED in belay causal).",
    ),
    cmd: str = typer.Option(
        "",
        "--cmd",
        help="Free-form command, for steps with no _belay_test_ref. Recorded as "
        "mode='command' -- never shown as a verified *test* (only that some command "
        "passed), since nothing ties an arbitrary --cmd to a specific declared test.",
    ),
    cwd: str = typer.Option("", "--cwd", help="Working directory to run in (default: cwd)."),
    by: str = typer.Option("", "--by", help="Identity running this verification."),
    db: str = typer.Option("belay.db", "--db", help="Ledger SQLite file path."),
    timeout: float = typer.Option(300.0, "--timeout", help="Seconds before the command is killed."),
) -> None:
    """Run the step's own declared test (`--runner`) or a free-form command (`--cmd`).

    Exactly one of `--runner`/`--cmd` is required. `--runner` reads the
    step's `_belay_test_ref` from the ledger and builds the command from a
    fixed template (`belay/ledger/test_evidence.py`'s `RUNNERS`) -- an
    agent or operator cannot substitute a different, easier-to-pass command
    under a false test label, because the command isn't typed in by hand at
    all. Real git context (HEAD, tree hash, dirty flag) and `--by` are
    recorded alongside the result so it's reproducible evidence, not a bare
    pass/fail. Records ledger event `belay:test_verified`
    (deliberately outside `belay.ledger.model.EVENT_TYPES` -- adoption/DX,
    not spec §9.1's closed set).
    """
    import getpass

    from belay.ledger.store import LedgerStore
    from belay.ledger.test_evidence import run_command, run_declared_test

    if bool(runner) == bool(cmd):
        typer.echo("error: give exactly one of --runner or --cmd", err=True)
        raise typer.Exit(code=1)

    ledger = LedgerStore(f"sqlite:///{Path(db).resolve().as_posix()}")
    events = ledger.read(session_id)
    if not events:
        typer.echo(f"error: no events found for session {session_id!r} in {db}", err=True)
        raise typer.Exit(code=1)

    step_status = next(
        (
            e.type
            for e in events
            if e.step_seq == step and e.type in ("step_committed", "step_failed")
        ),
        None,
    )
    if step_status is None:
        typer.echo(
            f"error: step {step} not found or not committed in session {session_id!r}", err=True
        )
        raise typer.Exit(code=1)
    if step_status != "step_committed":
        typer.echo(f"error: step {step} is {step_status!r}, not step_committed", err=True)
        raise typer.Exit(code=1)

    verified_by = by or getpass.getuser()

    if runner:
        test_ref = next(
            (
                e.payload.get("test_ref")
                for e in events
                if e.type == "plan_created" and e.step_seq == step
            ),
            None,
        )
        if not test_ref:
            typer.echo(f"error: step {step} has no _belay_test_ref to verify", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"running declared test: {test_ref!r} via {runner}")
        result = run_declared_test(
            test_ref, runner, cwd=cwd or None, timeout=timeout, verified_by=verified_by
        )
    else:
        typer.echo(f"running free-form command (mode=command, never 'VERIFIED test'): {cmd}")
        result = run_command(cmd, cwd=cwd or None, timeout=timeout, verified_by=verified_by)

    ledger.append(
        session_id,
        "belay:test_verified",
        {
            "mode": result.mode,
            "cmd": result.cmd,
            "test_ref": result.test_ref,
            "exit_code": result.exit_code,
            "output_hash": result.output_hash,
            "duration_ms": result.duration_ms,
            "passed": result.passed,
            "git_head": result.git.head,
            "git_tree_hash": result.git.tree_hash,
            "git_dirty": result.git.dirty,
            "verified_by": result.verified_by,
        },
        step_seq=step,
    )
    status = "PASSED" if result.passed else "FAILED"
    label = "TEST" if result.mode == "test" else "COMMAND"
    typer.echo(
        f"{label} {status} (exit_code={result.exit_code}, {result.duration_ms}ms, "
        f"output_hash={result.output_hash}, git_head={result.git.head}) -- "
        f"recorded against step {step}"
    )
    if not result.passed:
        raise typer.Exit(code=1)


@app.command(name="export-pr")
def export_pr(
    session_id: str = typer.Argument(..., help="Committed session to package as a PR."),
    repo: str = typer.Option(..., "--repo", help="Path to the git repo the files live in."),
    db: str = typer.Option("belay.db", "--db", help="Ledger SQLite file path."),
    base: str = typer.Option("main", "--base", help="Base branch to open the PR against."),
    key: str = typer.Option(
        "", "--key", help="Ed25519 signing key (belay keygen) to attach signed evidence (E13)."
    ),
    config: str = typer.Option(
        "",
        "--config",
        "-c",
        help="Wrap config path -- include to compute a real `belay rewind --dry-run` plan "
        "in the PR body's 'How is this undone?' section.",
    ),
    intent_contract: str = typer.Option(
        "",
        "--intent-contract",
        help="Intent contract YAML -- include to answer 'what was asked?' and 'what "
        "changed without being asked?' (scope deviations) with real data instead of "
        "an explicit 'not available'.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would change without touching git."
    ),
) -> None:
    """Package a committed session's file changes as a real branch + PR, proof-carrying.

    The PR body answers a reviewer's real questions from real data --
    what was asked, what changed outside that scope, what's proven by a
    test, what isn't, what external effects occurred, and how to undo it
    (a real `belay rewind --dry-run` plan) -- rather than just listing
    files. Any section without enough data says so explicitly instead of
    guessing.

    Post-hoc, not a pre-execution gate (see `docs/adr` if one is later added
    for the rejected gate design): the session already ran through the full
    governed pipeline and is sitting in the ledger. Only recognizes the
    read/write/delete-file shape `examples/contracts/fs.yaml` already uses
    (a `path` arg, optional `content`) -- other tool shapes are skipped, not
    guessed at. If the `gh` CLI is on PATH, opens the PR for real; otherwise
    prints the exact command to run.
    """
    from belay.cli.export_pr import (
        ExportPrError,
        apply_changes,
        build_proof_body,
        checkout_branch,
        commit_changes,
        extract_file_changes,
        gh_pr_create_command,
    )
    from belay.ledger.store import LedgerStore

    ledger = LedgerStore(f"sqlite:///{Path(db).resolve().as_posix()}")
    events = ledger.read(session_id)
    if not events:
        typer.echo(f"error: no events found for session {session_id!r} in {db}", err=True)
        raise typer.Exit(code=1)

    changes = extract_file_changes(events)
    if not changes:
        typer.echo("no recognizable file changes (path/content-shaped steps) in this session")
        raise typer.Exit(code=1)

    typer.echo(f"{len(changes)} file change(s) found:")
    for c in changes:
        kind = "delete" if c.after is None else "write"
        typer.echo(f"  step {c.step_seq}: {kind} {c.path} ({c.tool})")

    if dry_run:
        typer.echo("--dry-run: stopping before touching git")
        return

    repo_path = Path(repo).resolve()
    branch = f"belay/{session_id}"
    try:
        checkout_branch(repo_path, branch, base)
        paths = apply_changes(repo_path, changes)
    except ExportPrError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    evidence_note = ""
    if key:
        from belay.ledger.signing import SigningKey, sign_session

        signing_key = SigningKey.load(key)
        bundle = sign_session(events, signing_key)
        evidence_dir = repo_path / ".belay-evidence"
        evidence_dir.mkdir(exist_ok=True)
        evidence_path = evidence_dir / f"{session_id}.json"
        evidence_path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
        paths.append(str(evidence_path.relative_to(repo_path)))
        evidence_note = (
            f"\n\nSigned evidence: `.belay-evidence/{session_id}.json` "
            f"(public key `{bundle.public_key}`) -- verify offline with "
            f"`belay verify-evidence .belay-evidence/{session_id}.json`."
        )

    message = f"belay: {len(changes)} file change(s) from session {session_id}"
    try:
        commit_changes(repo_path, message, paths)
    except ExportPrError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"branch {branch} created in {repo_path} ({len(paths)} file(s) committed)")

    intent_text: str | None = None
    allowed_scope: list[str] | None = None
    intent_verified: bool | None = None
    if intent_contract:
        from belay.canonical import canonical_hash
        from belay.intent.loader import load_intent_contract

        ic = load_intent_contract(intent_contract)
        intent_text = ic.intent
        allowed_scope = ic.allowed_scope

        recorded_hash = next(
            (
                e.payload.get("intent_contract_hash")
                for e in events
                if e.type == "session_started"
            ),
            None,
        )
        if recorded_hash is not None:
            intent_verified = recorded_hash == canonical_hash(ic.model_dump())

    rewind_plan_lines: list[str] | None = None
    if config:
        import anyio

        from belay.contracts.loader import load_contract_set
        from belay.policy.model import default_policy
        from belay.proxy.config import WrapConfig
        from belay.rewind.service import RewindService

        wrap_config = WrapConfig.load(config)
        contract_set_for_rewind = load_contract_set(wrap_config.contracts)
        rewind_service = RewindService(
            ledger=ledger, policy=default_policy(), contract_set=contract_set_for_rewind
        )

        async def _no_upstream(tool: str, args: dict[str, object]) -> dict[str, object]:
            raise AssertionError("PR-body dry-run must never call upstream")

        report = anyio.run(
            lambda: rewind_service.rewind(session_id, _no_upstream, dry_run=True, by="export-pr")
        )
        rewind_plan_lines = [
            f"step {s.step_seq}: {s.tool} -> {s.status}" for s in report.plan.steps
        ]

    body_path = repo_path / f".belay-pr-body-{session_id}.md"
    body = build_proof_body(
        session_id,
        events,
        changes,
        evidence_note,
        intent_text,
        allowed_scope,
        rewind_plan_lines,
        intent_verified,
    )
    body_path.write_text(body, encoding="utf-8")

    import shutil

    if shutil.which("gh"):
        cmd = gh_pr_create_command(branch, base, message, str(body_path))
        result = subprocess_run(cmd, repo_path)
        typer.echo(result)
    else:
        cmd = gh_pr_create_command(branch, base, message, str(body_path))
        typer.echo(
            "gh CLI not found on PATH -- push the branch and open the PR yourself:\n"
            f"  git -C {repo_path} push -u origin {branch}\n"
            f"  {' '.join(cmd)}"
        )


def subprocess_run(cmd: list[str], cwd: Path) -> str:
    import subprocess

    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return result.stdout.strip() or result.stderr.strip()


@app.command()
def replay(
    session_id: str = typer.Argument(..., help="Real session to replay (spec §9)."),
    at_step: int = typer.Option(
        ..., "--at-step", help="step_seq whose args are overridden (spec §9.1 step_seq)."
    ),
    override: str = typer.Option(
        ..., "--override", help="JSON object merged into that step's original args."
    ),
    by: str = typer.Option(..., "--by", help="Identity (E14) driving this replay."),
    config: str = typer.Option("belay.wrap.json", "--config", "-c", help="Wrap config path."),
    policy: str = typer.Option(
        "", "--policy", help="Policy document path (spec §6.1); default is the built-in policy."
    ),
    resume: str = typer.Option(
        "",
        "--resume",
        help="A replay session_id from a previous `belay replay` run that paused for "
        "approval -- continue it from its next step instead of starting a new one "
        "(approve/reject the pause first via `belay approvals`).",
    ),
) -> None:
    """Re-execute a real session against the live upstream, one step overridden.

    Unlike `belay counterfactual` (plan-v2 E12, purely offline, never calls
    the real upstream), this drives a brand-new session through the real
    `Lifecycle` -- resolve -> plan -> policy -> approval -> execute -- for
    every step the original session attempted (`plan_created` events, in
    `step_seq` order), with `--override` merged into the one step named by
    `--at-step`; every other step reuses its originally recorded args
    verbatim. It is real execution, not a simulation: if a step pauses for
    approval or fails for real, replay stops there and reports it rather
    than guessing what happens next -- steps after that point are never
    attempted. The new session is fully real and ledgered (`initiated_by`
    records the source session and driving identity for lineage), never
    written back onto the original session's chain.
    """
    import json as jsonlib
    import os
    import uuid as uuidlib

    import anyio

    from belay.contracts.loader import load_contract_set
    from belay.ledger.store import LedgerStore
    from belay.policy.model import default_policy, load_policy
    from belay.proxy.config import WrapConfig
    from belay.proxy.lifecycle import Lifecycle
    from belay.proxy.upstream import connect_stdio

    override_args = jsonlib.loads(override)
    if not isinstance(override_args, dict):
        typer.echo("error: --override must be a JSON object", err=True)
        raise typer.Exit(code=1)

    wrap_config = WrapConfig.load(config)
    contract_set = load_contract_set(wrap_config.contracts)
    policy_doc = load_policy(policy) if policy else default_policy()
    ledger = LedgerStore(f"sqlite:///{Path(wrap_config.db).resolve().as_posix()}")

    original_events = ledger.read(session_id)
    steps: dict[int, tuple[str, dict[str, Any]]] = {}
    for event in original_events:
        if event.type == "plan_created" and event.step_seq is not None:
            steps.setdefault(event.step_seq, (event.payload["tool"], event.payload["args"]))
    if not steps:
        typer.echo(f"error: no plan_created steps found for session {session_id!r}", err=True)
        raise typer.Exit(code=1)
    if at_step not in steps:
        typer.echo(
            f"error: --at-step {at_step} not in session's steps ({sorted(steps)})", err=True
        )
        raise typer.Exit(code=1)

    resumed_through = 0
    if resume:
        new_session_id = resume
        prior_events = ledger.read(new_session_id)
        if not prior_events:
            typer.echo(f"error: no such replay session to resume: {resume!r}", err=True)
            raise typer.Exit(code=1)
        resumed_through = max((e.step_seq or 0) for e in prior_events)
    else:
        new_session_id = f"replay_{uuidlib.uuid4().hex[:12]}"

    async def _main() -> None:
        async with connect_stdio(
            wrap_config.upstream.command, wrap_config.upstream.args, env=dict(os.environ)
        ) as upstream:
            lifecycle = Lifecycle(
                contract_set=contract_set,
                unsafe_passthrough_tools=frozenset(wrap_config.unsafe_passthrough),
                ledger=ledger,
                session_id=new_session_id,
                policy=policy_doc,
            )
            if resume:
                lifecycle._step_seq = resumed_through
                typer.echo(f"resuming {new_session_id} after step {resumed_through}")
            else:
                lifecycle.start_session(
                    initiated_by=f"replay:{by}", on_behalf_of=f"replay-of:{session_id}"
                )
                typer.echo(f"new session: {new_session_id} (replay of {session_id})")

            async def executor(tool: str, args: dict[str, Any]) -> Any:
                return await upstream.call_tool(tool, args)

            for step_seq in sorted(steps):
                if step_seq <= resumed_through:
                    continue
                tool, args = steps[step_seq]
                if step_seq == at_step:
                    args = {**args, **override_args}
                    typer.echo(f"  step {step_seq}: {tool} (OVERRIDDEN args={args})")
                else:
                    typer.echo(f"  step {step_seq}: {tool}")
                annotations = upstream.annotations_for(tool)
                read_only_hint = bool(annotations and annotations.readOnlyHint)
                try:
                    result = await lifecycle.govern_and_execute(
                        tool, args, read_only_hint=read_only_hint, executor=executor
                    )
                except BelayError as exc:
                    typer.echo(f"    -> stopped: {exc.to_dict()}", err=True)
                    return
                if isinstance(result, dict) and result.get("status") == "pending_approval":
                    typer.echo(
                        f"    -> pending_approval (approval={result['approval_id']}) -- resolve "
                        f"with `belay approvals list/approve/reject --db {wrap_config.db}`, "
                        "replay stops here"
                    )
                    return
                typer.echo("    -> committed")

    anyio.run(_main)


@app.command()
def bootstrap(
    server_dir: str = typer.Argument(
        ..., help="Directory of the upstream MCP server, unless --command overrides it."
    ),
    command: str = typer.Option(
        "", "--command", help="Executable to launch the upstream server (e.g. 'npx')."
    ),
    arg: list[str] = typer.Option(  # noqa: B008
        [], "--arg", help="Argument to pass to --command (repeatable, in order)."
    ),
    contracts: list[str] = typer.Option(  # noqa: B008
        [],
        "--contracts",
        help="Existing contract document(s) (repeatable). Omit to auto-draft one with "
        "`belay draft-contracts` (provenance.verified: false -- review it after).",
    ),
    client: str = typer.Option(
        "all",
        "--client",
        help="MCP client(s) to register with, comma-separated, or 'all' (default).",
    ),
    db: str = typer.Option("belay.db", "--db", help="Ledger SQLite file path."),
    name: str = typer.Option("belay", "--name", help="MCP server name clients will list it under."),
    skip_agent_instructions: bool = typer.Option(
        False,
        "--skip-agent-instructions",
        help="Don't touch AGENTS.md/CLAUDE.md in the current directory.",
    ),
) -> None:
    """One-command setup: wrap, (optionally draft contracts,) register clients, brief the agent.

    Runs `belay draft-contracts` (if `--contracts` wasn't given), `belay
    wrap`, `belay init --client <...>`, and upserts a standing-instruction
    block into `./AGENTS.md` and `./CLAUDE.md` (created if missing) telling
    any agent reading them to use Belay's MCP tools by default -- so a human
    doesn't have to say "use belay" every session for it to happen.
    """
    import anyio

    from belay.cli.agent_instructions import upsert
    from belay.contracts.loader import load_contract_set
    from belay.proxy.config import UpstreamCommand, WrapConfig
    from belay.proxy.upstream import connect_stdio

    if command:
        upstream = UpstreamCommand(command=command, args=list(arg))
    else:
        server_path = Path(server_dir).resolve()
        entry = server_path / "server.py"
        if not entry.is_file():
            typer.echo(
                f"error: {entry} not found (expected an MCP server entry point, "
                "or pass --command for a non-Python server)",
                err=True,
            )
            raise typer.Exit(code=1)
        upstream = UpstreamCommand(command=sys.executable, args=[str(entry)])

    contract_paths = list(contracts)
    if not contract_paths:
        from belay.contracts.draft import draft_contracts as draft_fn

        draft_out = Path("contracts_draft.yaml").resolve()

        async def _draft() -> None:
            import os as os_mod

            import yaml

            async with connect_stdio(
                upstream.command, upstream.args, env=dict(os_mod.environ)
            ) as upstream_client:
                tools = await upstream_client.list_tools()
                results = draft_fn(tools)
                text = yaml.safe_dump_all(
                    [r.document for r in results], sort_keys=False, default_flow_style=False
                )
                draft_out.write_text(text, encoding="utf-8")

        anyio.run(_draft)
        typer.echo(f"1. drafted contracts -> {draft_out} (provenance.verified: false, review it)")
        contract_paths = [str(draft_out)]
    else:
        typer.echo(f"1. using existing contracts: {', '.join(contract_paths)}")

    load_contract_set(contract_paths)  # fail fast on bad contracts

    wrap_out = Path("belay.wrap.json").resolve()
    wrap_config = WrapConfig(
        upstream=upstream,
        contracts=[str(Path(c).resolve()) for c in contract_paths],
        db=db,
        initiated_by="unknown",
    )
    wrap_config.save(str(wrap_out))
    typer.echo(f"2. wrapped -> {wrap_out}")

    clients = (
        list(_CLIENT_CONFIG_PATHS) if client == "all" else [c.strip() for c in client.split(",")]
    )
    for c in clients:
        if c not in _CLIENT_CONFIG_PATHS:
            typer.echo(f"   skipping unknown client {c!r}", err=True)
            continue
        try:
            target = _register_client(c, wrap_out, name)
        except ValueError as exc:
            typer.echo(f"   {c}: {exc} -- nothing was written", err=True)
            continue
        typer.echo(f"3. registered with {c} -> {target}")

    if not skip_agent_instructions:
        for doc_name in ("AGENTS.md", "CLAUDE.md"):
            doc_path = Path(doc_name)
            existing = doc_path.read_text(encoding="utf-8") if doc_path.is_file() else ""
            doc_path.write_text(upsert(existing, name), encoding="utf-8")
            typer.echo(f"4. briefed agent via {doc_path.resolve()}")

    typer.echo("done -- restart your MCP client(s) for the config change to take effect")


@app.command(name="draft-contracts")
def draft_contracts_command(
    server_dir: str = typer.Argument(
        ...,
        help="Directory of the upstream MCP server (must contain server.py), "
        "unless --command overrides the launch entirely.",
    ),
    command: str = typer.Option(
        "", "--command", help="Executable to launch the upstream server (e.g. 'npx')."
    ),
    arg: list[str] = typer.Option(  # noqa: B008
        [], "--arg", help="Argument to pass to --command (repeatable, in order)."
    ),
    out: str = typer.Option(
        "contracts_draft.yaml", "--out", "-o", help="Where to write the draft contract set."
    ),
) -> None:
    """Propose a starting contract per upstream tool from its MCP name/annotations.

    Connects to the real upstream, reads each tool's `readOnlyHint`/
    `destructiveHint` and name (no LLM involved), and drafts a contract per
    tool -- every one `provenance.verified: false`, meant for human review
    and editing, never trusted as-is (spec §4.7's `verified` flag exists
    exactly for this: Belay never treats an unreviewed contract as ground
    truth). Read/write/delete tools sharing a resource name are paired into
    capture+undo contracts, mirroring the hand-written pattern in
    `examples/contracts/fs.yaml`; everything else defaults to `irreversible`
    -- the safe default when no undo path can be inferred.
    """
    import os

    import anyio
    import yaml

    from belay.contracts.draft import draft_contracts
    from belay.proxy.config import UpstreamCommand
    from belay.proxy.upstream import connect_stdio

    if command:
        upstream = UpstreamCommand(command=command, args=list(arg))
    else:
        server_path = Path(server_dir).resolve()
        entry = server_path / "server.py"
        if not entry.is_file():
            typer.echo(
                f"error: {entry} not found (expected an MCP server entry point, "
                "or pass --command for a non-Python server)",
                err=True,
            )
            raise typer.Exit(code=1)
        upstream = UpstreamCommand(command=sys.executable, args=[str(entry)])

    async def _main() -> None:
        async with connect_stdio(
            upstream.command, upstream.args, env=dict(os.environ)
        ) as client:
            tools = await client.list_tools()
            results = draft_contracts(tools)
            docs = [r.document for r in results]
            text = yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False)
            Path(out).write_text(text, encoding="utf-8")
            typer.echo(f"wrote {len(docs)} draft contract(s) to {out}")
            for r in results:
                typer.echo(f"  {r.tool_name}: {r.note}")
            typer.echo("review every one before use -- all are provenance.verified: false")

    anyio.run(_main)


@app.command()
def run(
    config: str = typer.Option("belay.wrap.json", "--config", "-c", help="Wrap config path."),
    policy: str = typer.Option(
        "",
        "--policy",
        help="Policy document path (spec §6.1); default is the out-of-the-box policy.",
    ),
    initiated_by: str = typer.Option(
        "",
        "--initiated-by",
        help="Identity (E14) that started this session. Overrides `belay wrap`'s default; "
        "if neither is given, falls back to the loud explicit \"unknown\".",
    ),
    on_behalf_of: str = typer.Option(
        "",
        "--on-behalf-of",
        help="Optional: the accountable identity this initiator acts for (E14).",
    ),
    intent_contract: str = typer.Option(
        "",
        "--intent-contract",
        help="Intent contract YAML (adoption/DX, not spec-numbered): 'intent: ...', "
        "'allowed_scope'/'forbidden_scope' path globs, 'forbidden_tools', "
        "'budgets.files_changed'. A call violating it is denied before the upstream "
        "ever sees it.",
    ),
) -> None:
    """Start the Belay MCP proxy over stdio (spec §3, Appendix C)."""
    import os

    import anyio

    from belay.contracts.loader import load_contract_set
    from belay.ledger.store import LedgerStore
    from belay.policy.model import default_policy, load_policy
    from belay.proxy.config import WrapConfig
    from belay.proxy.server import BelayProxyServer
    from belay.proxy.upstream import connect_stdio

    wrap_config = WrapConfig.load(config)
    contract_set = load_contract_set(wrap_config.contracts)
    policy_doc = load_policy(policy) if policy else default_policy()
    ledger = LedgerStore(f"sqlite:///{Path(wrap_config.db).resolve().as_posix()}")
    session_id = f"s_{uuid.uuid4().hex[:12]}"
    effective_initiated_by = initiated_by or wrap_config.initiated_by or "unknown"
    effective_on_behalf_of = on_behalf_of or wrap_config.on_behalf_of or None

    intent_contract_obj = None
    if intent_contract:
        from belay.intent.loader import load_intent_contract

        intent_contract_obj = load_intent_contract(intent_contract)

    async def _main() -> None:
        async with connect_stdio(
            wrap_config.upstream.command, wrap_config.upstream.args, env=dict(os.environ)
        ) as upstream:
            proxy = BelayProxyServer(
                upstream,
                contract_set,
                ledger,
                session_id,
                unsafe_passthrough_tools=frozenset(wrap_config.unsafe_passthrough),
                policy=policy_doc,
                intent_contract=intent_contract_obj,
            )
            proxy.lifecycle.start_session(effective_initiated_by, effective_on_behalf_of)
            await proxy.run_stdio()

    anyio.run(_main)


@app.command(name="plan")
def plan_command(
    tool: str = typer.Argument(..., help="Tool name to plan (spec §5.1)."),
    args: str = typer.Option("{}", "--args", help="JSON-encoded tool arguments."),
    config: str = typer.Option("belay.wrap.json", "--config", "-c", help="Wrap config path."),
    policy: str = typer.Option(
        "",
        "--policy",
        help="Policy document path (spec §6.1); default is the out-of-the-box policy.",
    ),
) -> None:
    """Plan one tool call without executing it, printing the full Plan (spec §5.1)."""
    import json as jsonlib

    import anyio

    from belay.contracts.loader import load_contract_set
    from belay.planner.model import Plan, PlanningSession
    from belay.planner.planner import Planner
    from belay.policy.engine import PolicyEngine
    from belay.policy.model import default_policy, load_policy
    from belay.proxy.config import WrapConfig

    wrap_config = WrapConfig.load(config)
    contract_set = load_contract_set(wrap_config.contracts)
    policy_doc = load_policy(policy) if policy else default_policy()
    tool_args = jsonlib.loads(args)
    session = PlanningSession(session_id="cli", contract=contract_set.resolve(tool))

    async def _main() -> Plan:
        raw_plan = await Planner().plan(tool, tool_args, session)
        result = PolicyEngine().evaluate(raw_plan, policy_doc)
        return raw_plan.with_policy(result.verdict, result.reasons, result.requires_approval)

    result_plan = anyio.run(_main)
    typer.echo(result_plan.model_dump_json(indent=2))


approvals_app = typer.Typer(
    name="approvals",
    help="Approval queue operations (spec §7). CLI-only: never exposed to the agent (spec §12).",
    no_args_is_help=True,
)
app.add_typer(approvals_app, name="approvals")


def _approval_queue(db: str) -> ApprovalQueue:
    from belay.approvals.queue import ApprovalQueue

    return ApprovalQueue(db_url=f"sqlite:///{Path(db).resolve().as_posix()}")


def _ledger_for(db: str) -> LedgerStore:
    from belay.ledger.store import LedgerStore

    return LedgerStore(f"sqlite:///{Path(db).resolve().as_posix()}")


@approvals_app.command("list")
def approvals_list(
    db: str = typer.Option("belay.db", "--db", help="Ledger/approvals SQLite file path."),
    triage: bool = typer.Option(
        False,
        "--triage",
        help="Sort pending items highest-risk-first with a deterministic reason "
        "(reversibility/confidence/unknown effects/dimensions fired). Labels the "
        "queue only -- never approves or rejects anything; that stays CLI-only "
        "and human-typed (spec §12).",
    ),
) -> None:
    """List every approval item, oldest first (spec §7.1)."""
    queue = _approval_queue(db)
    items = queue.list()
    if not items:
        typer.echo("no approval items")
        return

    if triage:
        from belay.approvals.triage import triage_queue

        for item, result in triage_queue(items):
            typer.echo(
                f"[{result.risk:6s}] {item.approval_id}  {item.state:9s}  "
                f"plan={item.plan_id}  tool={item.plan.get('tool')}  "
                f"session={item.session_id}  -- {'; '.join(result.reasons)}"
            )
        return

    for item in items:
        typer.echo(
            f"{item.approval_id}  {item.state:9s}  plan={item.plan_id}  "
            f"tool={item.plan.get('tool')}  session={item.session_id}  "
            f"expires_at={item.expires_at.isoformat()}"
        )


@approvals_app.command("approve")
def approvals_approve(
    approval_id: str = typer.Argument(..., help="Approval item id (spec §7.1)."),
    reason: str = typer.Option("", "--reason", help="Optional human-readable reason."),
    by: str = typer.Option(
        "",
        "--by",
        help="Authenticated approver identity (spec §12); defaults to the OS user.",
    ),
    db: str = typer.Option("belay.db", "--db", help="Ledger/approvals SQLite file path."),
) -> None:
    """Approve a pending item (spec §7.1: `pending -> approved`)."""
    import getpass

    from belay.errors import BelayError

    approver = by or getpass.getuser()
    queue = _approval_queue(db)
    try:
        item = queue.approve(approval_id, approved_by=approver, reason=reason or None)
    except BelayError as exc:
        typer.echo(f"error: {exc.code} ({exc.detail})", err=True)
        raise typer.Exit(code=1) from exc

    _ledger_for(db).append(
        item.session_id,
        "approval_resolved",
        {
            "approval_id": item.approval_id,
            "plan_id": item.plan_id,
            "state": "approved",
            "approved_by": approver,
            "reason": reason or None,
        },
        step_seq=item.step_seq,
    )
    typer.echo(f"{item.approval_id} approved by {approver}")


@approvals_app.command("reject")
def approvals_reject(
    approval_id: str = typer.Argument(..., help="Approval item id (spec §7.1)."),
    reason: str = typer.Option("", "--reason", help="Optional human-readable reason."),
    by: str = typer.Option(
        "",
        "--by",
        help="Authenticated approver identity (spec §12); defaults to the OS user.",
    ),
    db: str = typer.Option("belay.db", "--db", help="Ledger/approvals SQLite file path."),
) -> None:
    """Reject a pending item (spec §7.1: `pending -> rejected`)."""
    import getpass

    from belay.errors import BelayError

    approver = by or getpass.getuser()
    queue = _approval_queue(db)
    try:
        item = queue.reject(approval_id, rejected_by=approver, reason=reason or None)
    except BelayError as exc:
        typer.echo(f"error: {exc.code} ({exc.detail})", err=True)
        raise typer.Exit(code=1) from exc

    _ledger_for(db).append(
        item.session_id,
        "approval_resolved",
        {
            "approval_id": item.approval_id,
            "plan_id": item.plan_id,
            "state": "rejected",
            "rejected_by": approver,
            "reason": reason or None,
        },
        step_seq=item.step_seq,
    )
    typer.echo(f"{item.approval_id} rejected by {approver}")


@app.command(name="rewind")
def rewind_command(
    session_id: str = typer.Argument(..., help="Session to rewind (spec §10.1)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the honest rewind plan without executing anything."
    ),
    by: str = typer.Option(
        "", "--by", help="Authenticated operator identity; defaults to the OS user."
    ),
    skip_and_continue: bool = typer.Option(
        False,
        "--skip-and-continue",
        help="Explicit opt-in: don't halt on the first failed/paused compensation (recorded).",
    ),
    to_step: int = typer.Option(
        -1,
        "--to-step",
        help="Rewind only steps with step_seq > this value (default: -1, meaning all).",
    ),
    intent: str = typer.Option(
        "",
        "--intent",
        help="Undo only steps tagged with this `_belay_intent` label (adoption/DX, not "
        "spec-numbered) -- requires the tagged steps to be a safe contiguous trailing "
        "run; refuses otherwise rather than guessing. Combine with --keep.",
    ),
    keep: str = typer.Option(
        "", "--keep", help="With --intent: the label of steps that must NOT be undone."
    ),
    config: str = typer.Option("belay.wrap.json", "--config", "-c", help="Wrap config path."),
    policy: str = typer.Option(
        "", "--policy", help="Policy document path (spec §6.1); default is the built-in policy."
    ),
) -> None:
    """Rewind a session's committed steps in reverse `step_seq` order (spec §10)."""
    import getpass
    import os

    import anyio

    from belay.contracts.loader import load_contract_set
    from belay.ledger.store import LedgerStore
    from belay.policy.model import default_policy, load_policy
    from belay.proxy.config import WrapConfig
    from belay.proxy.upstream import connect_stdio
    from belay.rewind.service import RewindReport, RewindService

    wrap_config = WrapConfig.load(config)
    contract_set = load_contract_set(wrap_config.contracts)
    policy_doc = load_policy(policy) if policy else default_policy()
    ledger = LedgerStore(f"sqlite:///{Path(wrap_config.db).resolve().as_posix()}")
    approver = by or getpass.getuser()
    service = RewindService(ledger=ledger, policy=policy_doc, contract_set=contract_set)

    if intent:
        from belay.rewind.intent import IntentRewindError, resolve_intent_to_step

        resolved_to_step: int | None
        try:
            resolved_to_step = resolve_intent_to_step(
                ledger.read(session_id), intent, keep or None
            )
        except IntentRewindError as exc:
            typer.echo(f"error: {exc.to_dict()}", err=True)
            raise typer.Exit(code=1) from None
        typer.echo(f"--intent {intent!r} resolved to --to-step {resolved_to_step}")
    else:
        resolved_to_step = None if to_step < 0 else to_step

    async def _no_upstream(
        tool: str, args: dict[str, object]
    ) -> dict[str, object]:  # pragma: no cover - defensive
        raise AssertionError("dry-run must never call upstream")

    async def _main() -> RewindReport:
        if dry_run:
            return await service.rewind(
                session_id, _no_upstream, to_step=resolved_to_step, dry_run=True, by=approver
            )
        async with connect_stdio(
            wrap_config.upstream.command, wrap_config.upstream.args, env=dict(os.environ)
        ) as upstream:

            async def executor(tool: str, args: dict[str, object]) -> dict[str, object]:
                result = await upstream.call_tool(tool, args)
                if result.isError:
                    raise RuntimeError(str(result.content))
                content = result.structuredContent or {}
                return dict(content.get("result", content)) if isinstance(content, dict) else {}

            return await service.rewind(
                session_id,
                executor,
                to_step=resolved_to_step,
                dry_run=False,
                by=approver,
                skip_and_continue=skip_and_continue,
            )

    report = anyio.run(_main)

    if report.dry_run:
        typer.echo(f"rewind plan for {session_id} (dry run -- nothing executed):")
        for step in report.plan.steps:
            typer.echo(f"  step {step.step_seq}: {step.tool} -> {step.status}")
        remaining = (
            len(report.plan.irreversible)
            + len(report.plan.conditional_unmet)
            + len(report.plan.indeterminate)
        )
        typer.echo(
            f"{len(report.plan.reversible)} compensation(s), {remaining} irreversible/indeterminate"
        )
        return

    for outcome in report.outcomes:
        result_suffix = f" ({outcome.result})" if outcome.result else ""
        typer.echo(f"  step {outcome.step_seq}: {outcome.tool} -> {outcome.status}{result_suffix}")
    typer.echo(f"verified result: {report.verified_result}")
    if report.fully_rewound:
        typer.echo("compensation executed · verification passed · session fully compensated")
    else:
        typer.echo("session NOT fully rewound -- see step statuses above")
        raise typer.Exit(code=1)


@app.command(name="counterfactual")
def counterfactual_command(
    session_id: str = typer.Argument(..., help="Session to branch (spec-equivalent, plan-v2 E12)."),
    at_step: int = typer.Option(
        ..., "--at-step", help="step_seq of the policy_evaluated event to fork at."
    ),
    override: str = typer.Option(
        ..., "--override", help='JSON-encoded override, e.g. \'{"verdict": "deny"}\'.'
    ),
    db: str = typer.Option("belay.db", "--db", help="Ledger SQLite file path."),
    json_out: bool = typer.Option(False, "--json", help="Print the full report as JSON."),
) -> None:
    """Ask "what if the human had decided differently?" -- purely offline (plan-v2 E12).

    Read-only: reads the ledger once and never opens an upstream connection,
    so it works whether or not the session's `belay run` is still live.
    """
    import json as jsonlib

    from belay.ledger.counterfactual import InvalidForkPoint, run_counterfactual
    from belay.ledger.store import LedgerStore

    ledger = LedgerStore(f"sqlite:///{Path(db).resolve().as_posix()}")
    events = ledger.read(session_id)
    override_dict = jsonlib.loads(override)

    try:
        report = run_counterfactual(events, at_step, override_dict)
    except InvalidForkPoint as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_out:
        typer.echo(jsonlib.dumps(report.to_dict(), indent=2))
        return

    typer.echo(f"counterfactual for {session_id}, forked at step {at_step} with {override_dict}:")
    for step in report.steps:
        basis = f" ({step.basis})" if step.basis else ""
        typer.echo(f"  step {step.step_seq}: {step.tool} -> {step.outcome}{basis}")
    if report.is_noop:
        typer.echo("no-op override: identical to what actually happened")
    else:
        typer.echo(
            f"{len(report.unchanged)} unchanged, {len(report.diverged)} diverged, "
            f"{len(report.unknown)} unknown"
        )


release_app = typer.Typer(
    name="release",
    help="Sign/verify an offline release bundle (plan-v2 E19.6) -- Ed25519 authenticity "
    "signing, NOT OS-level code-signing/notarization (see `belay release sign --help`).",
    no_args_is_help=True,
)
app.add_typer(release_app, name="release")


_RELEASE_SUMS_FILENAME = "SHA256SUMS.txt"
_RELEASE_SIG_FILENAME = "SHA256SUMS.txt.sig"
_RELEASE_PUBKEY_FILENAME = "release.pub"


@release_app.command("sign")
def release_sign(
    dist_dir: str = typer.Argument(
        ..., help="Directory of built artifacts to sign (wheel, binaries, ...)."
    ),
    key: str = typer.Option(
        ..., "--key", help="Path to an Ed25519 private signing key PEM (`belay keygen`)."
    ),
) -> None:
    """Compute SHA256SUMS.txt over every file in DIST_DIR, sign it with an
    Ed25519 key, and write SHA256SUMS.txt/.sig/release.pub alongside the
    artifacts -- everything a downloader needs to verify the bundle with no
    other trust relationship than this one public key.

    This is authenticity signing (Ed25519, via the same key mechanism
    `belay keygen`/`verify-export` already use for signed evidence, spec-
    adjacent plan-v2 E13) -- it proves the bundle came from whoever holds
    --key and hasn't been altered since. It is explicitly NOT OS-level
    code-signing or notarization (Windows Authenticode, Apple Developer ID
    + notarization): those require a paid, identity-verified certificate
    from Microsoft/Apple that this project does not have, so Windows
    SmartScreen/macOS Gatekeeper will still show an "unknown publisher"
    warning on the raw binaries regardless of this signature. Said plainly
    rather than implied -- this closes the "was this file tampered with in
    transit" gap, not the "is this a Microsoft/Apple-vetted publisher" one.
    """
    import hashlib

    from belay.ledger.signing import SigningKey

    root = Path(dist_dir).resolve()
    if not root.is_dir():
        typer.echo(f"error: {root} is not a directory", err=True)
        raise typer.Exit(code=1)

    skip = {_RELEASE_SUMS_FILENAME, _RELEASE_SIG_FILENAME, _RELEASE_PUBKEY_FILENAME}
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.name not in skip)
    if not files:
        typer.echo(f"error: no files found under {root}", err=True)
        raise typer.Exit(code=1)

    lines = [
        f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.relative_to(root).as_posix()}"
        for f in files
    ]
    sums_text = "\n".join(lines) + "\n"

    signing_key = SigningKey.load(key)
    signature_hex = signing_key.sign(sums_text.encode("utf-8")).hex()

    (root / _RELEASE_SUMS_FILENAME).write_text(sums_text, encoding="utf-8")
    (root / _RELEASE_SIG_FILENAME).write_text(signature_hex + "\n", encoding="utf-8")
    (root / _RELEASE_PUBKEY_FILENAME).write_text(signing_key.public_hex() + "\n", encoding="utf-8")

    typer.echo(f"signed {len(files)} file(s) in {root}")
    typer.echo(f"  {_RELEASE_SUMS_FILENAME} (checksums)")
    typer.echo(f"  {_RELEASE_SIG_FILENAME} (Ed25519 signature)")
    typer.echo(f"  {_RELEASE_PUBKEY_FILENAME} (public key: {signing_key.public_hex()})")


@release_app.command("verify")
def release_verify(
    dist_dir: str = typer.Argument(..., help="Directory containing a signed release bundle."),
    pubkey: str = typer.Option(
        "",
        "--pubkey",
        help="Path to a trusted public key (hex text). If omitted, the bundle's own "
        "release.pub is used -- weaker trust, since a tampered bundle could embed a "
        "matching forged key (same caveat as `verify-evidence --pubkey`).",
    ),
) -> None:
    """Verify a release bundle signed by `belay release sign`: the signature over
    SHA256SUMS.txt is valid, and every listed file's actual SHA256 matches
    what's recorded. Exits non-zero on any signature failure, missing file,
    or hash mismatch -- never a partial "mostly OK"."""
    import hashlib

    from belay.ledger.signing import verify_signature

    root = Path(dist_dir).resolve()
    sums_path = root / _RELEASE_SUMS_FILENAME
    sig_path = root / _RELEASE_SIG_FILENAME
    if not sums_path.is_file() or not sig_path.is_file():
        typer.echo(
            f"error: {root} is missing {_RELEASE_SUMS_FILENAME}/{_RELEASE_SIG_FILENAME}",
            err=True,
        )
        raise typer.Exit(code=1)

    pubkey_path = Path(pubkey) if pubkey else (root / _RELEASE_PUBKEY_FILENAME)
    if not pubkey_path.is_file():
        typer.echo(f"error: public key not found: {pubkey_path}", err=True)
        raise typer.Exit(code=1)
    trusted_pubkey = pubkey_path.read_text(encoding="utf-8").strip()

    sums_text = sums_path.read_text(encoding="utf-8")
    signature_hex = sig_path.read_text(encoding="utf-8").strip()
    signature_ok = verify_signature(trusted_pubkey, sums_text.encode("utf-8"), signature_hex)
    typer.echo(f"signature: {'OK' if signature_ok else 'INVALID'}")

    all_ok = signature_ok
    for line in sums_text.strip().splitlines():
        digest, _, rel_path = line.partition("  ")
        target = root / rel_path
        if not target.is_file():
            typer.echo(f"MISSING:  {rel_path}")
            all_ok = False
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual == digest:
            typer.echo(f"OK:       {rel_path}")
        else:
            typer.echo(f"MISMATCH: {rel_path}")
            all_ok = False

    if not all_ok:
        raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
