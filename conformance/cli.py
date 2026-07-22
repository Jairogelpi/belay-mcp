"""`belay-conformance` console script (plan.md E8, spec §13).

    belay-conformance run --target belay --level 3
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from conformance.report import ConformanceReport, markers_for_level

app = typer.Typer(
    name="belay-conformance",
    help="Run the Belay public conformance suite against a ConformanceTarget.",
    no_args_is_help=True,
)

_TESTS_DIR = Path(__file__).resolve().parent / "tests"


@app.command()
def run(
    target: str = typer.Option("belay", help="'belay' or a 'module:ClassName' adapter path."),
    level: int = typer.Option(3, min=1, max=3, help="Conformance level to report (spec §13)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the L1..`level` scenarios against `--target` and print a report."""
    markers = " or ".join(markers_for_level(level))
    args = [
        str(_TESTS_DIR),
        "-m",
        markers,
        f"--conformance-target={target}",
        "-p",
        "no:cacheprovider",
        "--override-ini=addopts=",
    ]
    if verbose:
        args.append("-v")
    exit_code = pytest.main(args)

    report = ConformanceReport(target=target, level=level, exit_code=int(exit_code))
    typer.echo(report.render())
    raise typer.Exit(code=0 if report.passed else 1)


@app.command()
def version() -> None:
    """Print the conformance suite's spec version (spec §13/§14)."""
    typer.echo("belay-conformance: spec 0.1")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
