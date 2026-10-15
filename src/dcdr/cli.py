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
from dcdr.config import DEFAULT_CONFIG, REPO_ROOT, Settings, load_settings

app = typer.Typer(help="DCDR pipeline command-line interface.", no_args_is_help=True)
build_app = typer.Typer(help="Build one data layer.", no_args_is_help=True)
app.add_typer(build_app, name="build")

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
    return pipeline.read_station_list(stations or REPO_ROOT / settings.stations.station_list)


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


@build_app.command("silver")
def build_silver(
    years: str = YearsOpt,
    stations: Path | None = StationsOpt,
    force: bool = typer.Option(False, "--force", help="Rebuild even if current."),
    config: Path = ConfigOpt,
    profile: Path | None = ProfileOpt,
) -> None:
    """Build silver station-years from bronze (incremental unless --force)."""

    def body(ctx: pipeline.RunContext) -> dict[str, Any]:
        ids = _station_ids(ctx.settings, stations)
        s = pipeline.run_silver(ctx, ids, pipeline.parse_years(years), force=force)
        return {"built": s.built, "skipped": s.skipped, "missing_bronze": s.missing_bronze}

    _execute("build silver", config, profile, body)


@build_app.command("gold")
def build_gold_cmd(
    years: str = YearsOpt,
    stations: Path | None = StationsOpt,
    config: Path = ConfigOpt,
    profile: Path | None = ProfileOpt,
) -> None:
    """Rebuild gold products from silver into a new version and activate it."""

    def body(ctx: pipeline.RunContext) -> dict[str, Any]:
        ids = _station_ids(ctx.settings, stations)
        return {"gold": str(pipeline.run_gold(ctx, ids, pipeline.parse_years(years)))}

    _execute("build gold", config, profile, body)


@app.command()
def run(
    years: str = YearsOpt,
    stations: Path | None = StationsOpt,
    force: bool = typer.Option(False, "--force", help="Rebuild silver even if current."),
    config: Path = ConfigOpt,
    profile: Path | None = ProfileOpt,
) -> None:
    """Ingest, then build silver and gold."""

    def body(ctx: pipeline.RunContext) -> dict[str, Any]:
        ids, yrs = _station_ids(ctx.settings, stations), pipeline.parse_years(years)
        ing = pipeline.run_ingest(ctx, ids, yrs)
        sil = pipeline.run_silver(ctx, ids, yrs, force=force)
        gold = pipeline.run_gold(ctx, ids, yrs)
        return {
            "ingest": asdict(ing),
            "silver_built": sil.built,
            "silver_skipped": sil.skipped,
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
