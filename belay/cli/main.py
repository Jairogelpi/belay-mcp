"""Typer entry point for the `belay` CLI.

Subcommands (`wrap`, `run`, `plan`, `approvals`, `rewind`, `verify`) are
implemented incrementally in E3-E7; E0 only wired the app so `belay
--help` works. E3 adds `wrap` and `run` (spec §3, §4.6, Appendix C).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import typer

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

    typer.echo(f"events: {len(events)}")
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
        ..., help="Directory of the upstream MCP server (must contain server.py)."
    ),
    contracts: list[str] = typer.Option(  # noqa: B008
        ..., "--contracts", help="Path to a contract document (repeatable)."
    ),
    unsafe_passthrough: str = typer.Option(
        "",
        "--unsafe-passthrough",
        help="Comma-separated tool names to allow through with no contract (spec §4.6).",
    ),
    db: str = typer.Option("belay.db", "--db", help="Ledger SQLite file path."),
    out: str = typer.Option(
        "belay.wrap.json", "--out", "-o", help="Where to write the wrap config."
    ),
) -> None:
    """Register an upstream MCP server + its contract set (spec §4.6, Appendix C)."""
    from belay.contracts.loader import load_contract_set
    from belay.proxy.config import UpstreamCommand, WrapConfig

    server_path = Path(server_dir).resolve()
    entry = server_path / "server.py"
    if not entry.is_file():
        typer.echo(f"error: {entry} not found (expected an MCP server entry point)", err=True)
        raise typer.Exit(code=1)

    # Validate the contract set now so `wrap` fails fast on bad contracts,
    # rather than at first `run`.
    load_contract_set(contracts)

    tools = [t.strip() for t in unsafe_passthrough.split(",") if t.strip()]
    config = WrapConfig(
        upstream=UpstreamCommand(command=sys.executable, args=[str(entry)]),
        contracts=[str(Path(c).resolve()) for c in contracts],
        unsafe_passthrough=tools,
        db=db,
    )
    config.save(out)
    typer.echo(f"wrote {out}")


@app.command()
def run(
    config: str = typer.Option("belay.wrap.json", "--config", "-c", help="Wrap config path."),
    policy: str = typer.Option(
        "",
        "--policy",
        help="Policy document path (spec §6.1); default is the out-of-the-box policy.",
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
            )
            proxy.lifecycle.start_session()
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
) -> None:
    """List every approval item, oldest first (spec §7.1)."""
    queue = _approval_queue(db)
    items = queue.list()
    if not items:
        typer.echo("no approval items")
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
        typer.echo(f"  step {outcome.step_seq}: {outcome.tool} -> {outcome.status}")
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
