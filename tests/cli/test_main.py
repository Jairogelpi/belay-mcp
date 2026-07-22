"""belay --help runs (plan.md E0 (c))."""

from belay.cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "belay" in result.stdout.lower()
