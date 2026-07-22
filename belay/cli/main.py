"""Typer entry point for the `belay` CLI.

Subcommands (`wrap`, `run`, `plan`, `approvals`, `rewind`, `verify`) are
implemented incrementally in E3-E7; E0 only wires the app so `belay
--help` works and the executable exists.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="belay",
    help="Belay: safe, reversible tool execution for AI agents.",
    no_args_is_help=True,
)


@app.callback()
def callback() -> None:
    """Belay: safe, reversible tool execution for AI agents.

    Subcommands (wrap, run, plan, approvals, rewind, verify) land in
    later entregas; this stub only guarantees `belay --help` works.
    """


def main() -> None:
    app()


if __name__ == "__main__":
    main()
