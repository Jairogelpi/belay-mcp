"""Typer entry point for the `belay` CLI.

Subcommands (`wrap`, `run`, `plan`, `approvals`, `rewind`, `verify`) are
implemented incrementally in E3-E7; E0 only wired the app so `belay
--help` works. E3 adds `wrap` and `run` (spec §3, §4.6, Appendix C).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import typer

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
) -> None:
    """Start the Belay MCP proxy over stdio (spec §3, Appendix C)."""
    import os

    import anyio

    from belay.contracts.loader import load_contract_set
    from belay.ledger.store import LedgerStore
    from belay.proxy.config import WrapConfig
    from belay.proxy.server import BelayProxyServer
    from belay.proxy.upstream import connect_stdio

    wrap_config = WrapConfig.load(config)
    contract_set = load_contract_set(wrap_config.contracts)
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
