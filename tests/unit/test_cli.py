"""Command-line interface (cli.py), exercised in-process with Typer's CliRunner."""

import re

from typer.testing import CliRunner

from dcdr import __version__
from dcdr.cli import app
from dcdr.config import Settings


def test_version_flag_prints_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"dcdr {__version__}" in result.output


def test_stage_commands_are_named_by_action() -> None:
    """Code and commands are actions (ingest/process/aggregate); data folders keep layer names."""
    out = CliRunner().invoke(app, ["--help"]).output
    for command in ("ingest", "process", "verify-silver", "aggregate", "run", "status"):
        assert command in out
    assert "build" not in out


def test_process_rejects_zero_workers() -> None:
    """`--workers` sets how many stations are built at a time; at least one."""
    result = CliRunner().invoke(app, ["process", "--years", "2024", "--workers", "0"])
    assert result.exit_code != 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)  # CI prints colour codes in the text
    assert "--workers" in plain


def test_verify_silver_with_nothing_carried_checks_nothing(settings: Settings) -> None:
    profile = settings.data_root.parent / "here.yaml"
    profile.write_text(f"paths:\n  data_root: {settings.data_root}\n")
    result = CliRunner().invoke(app, ["verify-silver", "--sample", "5", "--profile", str(profile)])
    assert result.exit_code == 0, result.output
    assert "'checked': 0" in result.output
