"""Typer entry point for the `belay` CLI.

Subcommands (`wrap`, `run`, `plan`, `approvals`, `rewind`, `verify`) are
implemented incrementally in E3-E7; E0 only wires the app so `belay
--help` works and the executable exists.
"""

from __future__ import annotations

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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
