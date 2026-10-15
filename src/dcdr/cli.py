"""Command-line interface. Parses arguments and calls pipeline functions; no logic here."""

from __future__ import annotations

import logging

import typer

from dcdr import __version__

app = typer.Typer(help="DCDR pipeline command-line interface.", no_args_is_help=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"dcdr {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """DCDR pipeline."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
