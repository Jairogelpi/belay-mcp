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


def _register_client(client: str, wrap_path: Path, name: str, scope: str = "project") -> Path:
    """Merge a `belay` entry into one client's MCP config. Returns the config path touched.

    Writes go through `atomic_write_with_backup` (temp file + `os.replace`,
    plus a `.belay-backup` of anything overwritten) -- a crash mid-write
    leaves the original file intact, never half-written.
    """
    import json

    from belay.cli.client_configs import atomic_write_with_backup

    target = _client_config_path(client, scope)
    command = sys.executable
    args = ["-m", "belay.cli.main", "run", "--config", str(wrap_path)]

    if client == "codex":
        from belay.cli.client_configs import render_codex_toml

        existing_toml = target.read_text(encoding="utf-8") if target.is_file() else ""
        new_text = render_codex_toml(existing_toml, name, command, args)
        atomic_write_with_backup(target, new_text)
        return target

    if client == "opencode":
        from belay.cli.client_configs import render_opencode_json

        existing_json = target.read_text(encoding="utf-8") if target.is_file() else ""
        new_text = render_opencode_json(existing_json, name, [command, *args])
        atomic_write_with_backup(target, new_text)
        return target

    doc: dict[str, object] = {}
    if target.is_file():
        text = target.read_text(encoding="utf-8").strip()
        if text:
            doc = json.loads(text)

    servers = doc.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{target}: 'mcpServers' is not an object")
    servers[name] = {"command": command, "args": args}
    atomic_write_with_backup(target, json.dumps(doc, indent=2) + "\n")
    return target


@app.command()
def init(
    client: str = typer.Option(
        ...,
        "--client",
        help="MCP client(s) to register Belay with, comma-separated: claude-desktop, "
        "claude-code, cursor, codex, opencode, or 'all'.",
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
) -> None:
    """Register Belay as an MCP server in one or more clients' configs (no manual JSON).

    Merges into each client's existing config -- other servers the agent
    already talks to are left untouched, so the agent sees Belay alongside
    its other tools rather than in place of them. Writes are atomic (temp
    file + rename) with a `.belay-backup` of anything overwritten.
    """
    if scope not in ("project", "user"):
        typer.echo(f"error: --scope must be 'project' or 'user', got {scope!r}", err=True)
        raise typer.Exit(code=1)

    clients = (
        list(_CLIENT_CONFIG_PATHS) if client == "all" else [c.strip() for c in client.split(",")]
    )
    for c in clients:
        if c not in _CLIENT_CONFIG_PATHS:
            typer.echo(
                f"error: unknown --client {c!r} (expected one of: "
                f"{', '.join(_CLIENT_CONFIG_PATHS)}, or 'all')",
                err=True,
            )
            raise typer.Exit(code=1)

    wrap_path = Path(config).resolve()
    if not wrap_path.is_file():
        typer.echo(
            f"error: {wrap_path} not found -- run `belay wrap` first to create it", err=True
        )
        raise typer.Exit(code=1)

    for c in clients:
        try:
            target = _register_client(c, wrap_path, name, scope)
        except ValueError as exc:
            typer.echo(f"error registering {c}: {exc} -- nothing was written", err=True)
            raise typer.Exit(code=1) from None
        typer.echo(f"registered '{name}' in {target}")
    typer.echo("restart the client(s) for the change to take effect")


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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
