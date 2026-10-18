from typer.testing import CliRunner

from dcdr import __version__
from dcdr.cli import app


def test_version_flag_prints_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"dcdr {__version__}" in result.output


def test_stage_commands_are_named_by_action() -> None:
    """Code and commands are actions (ingest/process/aggregate); data folders keep layer names."""
    out = CliRunner().invoke(app, ["--help"]).output
    for command in ("ingest", "process", "aggregate", "run", "status"):
        assert command in out
    assert "build" not in out
