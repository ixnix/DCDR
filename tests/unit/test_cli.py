"""Command-line interface (cli.py), exercised in-process with Typer's CliRunner."""

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


def test_process_rejects_zero_workers() -> None:
    """`--workers` sets how many stations are built at a time; at least one."""
    result = CliRunner().invoke(app, ["process", "--years", "2024", "--workers", "0"])
    assert result.exit_code != 0
    assert "--workers" in result.output
