"""Command-line interface. Parses arguments and calls pipeline functions; no logic here."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import polars as pl
import typer

from dcdr import __version__, pipeline
from dcdr.config import DEFAULT_CONFIG, Settings, load_settings

app = typer.Typer(help="DCDR pipeline command-line interface.", no_args_is_help=True)

ConfigOpt = typer.Option(DEFAULT_CONFIG, "--config", help="Config YAML file.")
ProfileOpt = typer.Option(None, "--profile", help="Profile YAML merged over the config.")
StationsOpt = typer.Option(None, "--stations", help="Station list CSV (default from config).")
YearsOpt = typer.Option(..., "--years", help="Years, e.g. 2024, 2022-2024 or 2020,2022.")


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


def _station_ids(settings: Settings, stations: Path | None) -> list[str]:
    return pipeline.read_station_list(stations or settings.station_list_path)


def _execute(
    command: str,
    config: Path,
    profile: Path | None,
    body: Callable[[pipeline.RunContext], dict[str, Any]],
) -> None:
    ctx = pipeline.start_run(load_settings(config, profile), command)
    try:
        summary = body(ctx)
    except Exception as exc:
        pipeline.finish_run(ctx, "failed", {"error": repr(exc)})
        raise
    pipeline.finish_run(ctx, "ok", summary)
    typer.echo(f"run {ctx.run_id}: {summary}")


@app.command()
def ingest(
    years: str = YearsOpt,
    stations: Path | None = StationsOpt,
    config: Path = ConfigOpt,
    profile: Path | None = ProfileOpt,
) -> None:
    """Download new or changed station-years from NOAA S3 into bronze."""

    def body(ctx: pipeline.RunContext) -> dict[str, Any]:
        ids = _station_ids(ctx.settings, stations)
        s = pipeline.run_ingest(ctx, ids, pipeline.parse_years(years))
        return asdict(s)

    _execute("ingest", config, profile, body)


@app.command()
def process(
    years: str = YearsOpt,
    stations: Path | None = StationsOpt,
    force: bool = typer.Option(False, "--force", help="Rebuild even if current."),
    config: Path = ConfigOpt,
    profile: Path | None = ProfileOpt,
) -> None:
    """Process bronze into silver: parse, QC, classify dust, pick synoptic slots (incremental)."""

    def body(ctx: pipeline.RunContext) -> dict[str, Any]:
        ids = _station_ids(ctx.settings, stations)
        s = pipeline.run_process(ctx, ids, pipeline.parse_years(years), force=force)
        return {
            "built": s.built,
            "skipped": s.skipped,
            "missing_bronze": s.missing_bronze,
            "failed": s.failed,
            "failures": s.failures,
        }

    _execute("process", config, profile, body)


@app.command()
def aggregate(
    years: str = YearsOpt,
    stations: Path | None = StationsOpt,
    config: Path = ConfigOpt,
    profile: Path | None = ProfileOpt,
) -> None:
    """Aggregate silver into gold products in a new version, check them and activate it."""

    def body(ctx: pipeline.RunContext) -> dict[str, Any]:
        ids = _station_ids(ctx.settings, stations)
        return {"gold": str(pipeline.run_aggregate(ctx, ids, pipeline.parse_years(years)))}

    _execute("aggregate", config, profile, body)


@app.command()
def run(
    years: str = YearsOpt,
    stations: Path | None = StationsOpt,
    force: bool = typer.Option(False, "--force", help="Rebuild silver even if current."),
    config: Path = ConfigOpt,
    profile: Path | None = ProfileOpt,
) -> None:
    """Ingest, process and aggregate: bronze, then silver, then gold."""

    def body(ctx: pipeline.RunContext) -> dict[str, Any]:
        ids, yrs = _station_ids(ctx.settings, stations), pipeline.parse_years(years)
        ing = pipeline.run_ingest(ctx, ids, yrs)
        sil = pipeline.run_process(ctx, ids, yrs, force=force)
        gold = pipeline.run_aggregate(ctx, ids, yrs)
        return {
            "ingest": asdict(ing),
            "silver_built": sil.built,
            "silver_skipped": sil.skipped,
            "silver_failed": sil.failed,
            "silver_failures": sil.failures,
            "gold": str(gold),
        }

    _execute("run", config, profile, body)


@app.command()
def status(config: Path = ConfigOpt, profile: Path | None = ProfileOpt) -> None:
    """Show source-manifest counts and the most recent runs."""
    root = load_settings(config, profile).data_root / "_state"
    sources, runs = root / "sources.parquet", root / "runs.parquet"
    if sources.exists():
        typer.echo(pl.read_parquet(sources).group_by("status").len().sort("status"))
    else:
        typer.echo("no source manifest yet")
    if runs.exists():
        cols = ["run_id", "command", "status", "git_commit"]
        typer.echo(pl.read_parquet(runs).tail(5).select(cols))
