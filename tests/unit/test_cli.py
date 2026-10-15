from typer.testing import CliRunner

from dcdr import __version__
from dcdr.cli import app


def test_version_flag_prints_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"dcdr {__version__}" in result.output
