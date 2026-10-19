"""Stage runner: run context, incremental silver decisions and run records (spec §11-12).

Where this fits
---------------
`cli.py` calls the functions here; they call the stage modules:

    run_ingest    -> ingest.py               NOAA S3 -> bronze
    run_process   -> process/build.py        bronze  -> silver   (one station-year at a time)
    run_aggregate -> aggregate.py            silver  -> gold     (all stations at once)

and `start_run` / `finish_run` wrap every command with a run ID and a run-log entry.

Incremental processing (the key idea in `run_process`)
------------------------------------------------------
A silver file is rebuilt only if something that affects it changed. Every silver file stores, in
its Parquet metadata, what it was built from: the method version, the silver config hash, the MD5
of its bronze input (and of the previous year's, used for the haze look-back), and whether the
next year's bronze existed. If all of these still match, the file is skipped. So re-running
`dcdr run` after adding one new year rebuilds only what that year touches.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from dcdr import __version__
from dcdr.aggregate import build_gold
from dcdr.config import Settings, canonical_yaml, config_hash, silver_config_hash
from dcdr.ingest import IngestSummary, bronze_path, ingest, make_store
from dcdr.process import METHOD_VERSION
from dcdr.process.build import build_station_year, write_silver
from dcdr.state import (
    Manifest,
    append_run_record,
    atomic_write_bytes,
    git_revision,
    new_run_id,
    read_parquet_metadata,
    utcnow,
)

log = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).resolve().parent  # a git checkout only when run from source


@dataclass
class RunContext:
    """Everything a stage needs to know about the current run, passed from stage to stage."""

    settings: Settings
    run_id: str
    command: str
    started_at: datetime
    git_commit: str
    git_dirty: bool

    @property
    def root(self) -> Path:
        """The data folder (bronze/, silver/, gold/, _state/ live here)."""
        return self.settings.data_root

    @property
    def state_dir(self) -> Path:
        """Folder for the manifest, the run log and per-run config snapshots."""
        return self.root / "_state"


def start_run(settings: Settings, command: str) -> RunContext:
    """Create the run context and save the exact settings used, for reproducibility."""
    settings.data_root.mkdir(parents=True, exist_ok=True)  # create the data folder if missing
    commit, dirty = git_revision(PACKAGE_DIR)
    ctx = RunContext(settings, new_run_id(), command, utcnow(), commit, dirty)
    # Snapshot of the fully merged settings (defaults + profile + DCDR_DATA) for this run.
    atomic_write_bytes(
        ctx.state_dir / "runs" / ctx.run_id / "config.resolved.yaml",
        canonical_yaml(settings).encode(),
    )
    log.info("run %s started (%s) commit=%s dirty=%s", ctx.run_id, command, commit[:8], dirty)
    return ctx


def finish_run(ctx: RunContext, status: str, summary: dict[str, Any]) -> None:
    """Append this run's outcome ("ok" or "failed") and summary to the run log."""
    append_run_record(
        ctx.state_dir / "runs.parquet",
        {
            "run_id": ctx.run_id,
            "command": ctx.command,
            "started_at": ctx.started_at,
            "finished_at": utcnow(),
            "status": status,
            "git_commit": ctx.git_commit,
            "git_dirty": ctx.git_dirty,
            "dcdr_version": __version__,
            "config_hash": config_hash(ctx.settings),
            "summary": json.dumps(summary, default=str),  # default=str: paths etc. become text
        },
    )


def read_station_list(path: Path) -> list[str]:
    """Station IDs from a CSV with a `station_id` column (other columns are ignored)."""
    with path.open(newline="") as fh:
        return [row["station_id"].strip() for row in csv.DictReader(fh)]


def parse_years(text: str) -> list[int]:
    """Turn the `--years` text into a sorted list: "2024", "2022-2024" or "2020,2022-2023"."""
    years: list[int] = []
    for part in text.split(","):
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            years.extend(range(lo, hi + 1))  # inclusive range
        else:
            years.append(int(part))
    return sorted(set(years))  # set() removes duplicates


def run_ingest(
    ctx: RunContext, stations: list[str], years: list[int], store: Any = None
) -> IngestSummary:
    """Ingest the requested station-years, plus the year before the first one.

    The extra year is needed because silver's haze rule looks back 24 hours, which on 1 January
    reaches into the previous year. `store` can be replaced by a fake in tests (no network).
    """
    padded = sorted(set(years) | {min(years) - 1})  # previous year feeds the haze look-back
    manifest = Manifest(ctx.state_dir / "sources.parquet")
    summary = ingest(
        store if store is not None else make_store(ctx.settings.ingest),
        ctx.settings.ingest,
        ctx.root,
        manifest,
        set(stations),
        padded,
        ctx.run_id,
    )
    manifest.save()
    return summary


@dataclass
class ProcessSummary:
    """Counts reported by the process step."""

    built: int = 0  # silver files (re)built in this run
    skipped: int = 0  # already up to date
    missing_bronze: int = 0  # no bronze for that station-year (station did not report)
    failed: int = 0  # build failed; any previous silver file is kept
    failures: list[str] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)  # every current silver file (built + skipped)


def silver_path(root: Path, year: int, station: str) -> Path:
    """Where one station-year lives in silver (same layout as bronze)."""
    return root / "silver" / f"year={year}" / f"{station}.parquet"


def _bronze_md5(path: Path) -> str:
    """MD5 of the NOAA source behind a bronze file ("" if there is no such file)."""
    return read_parquet_metadata(path).get("source_md5", "") if path.exists() else ""


def _silver_is_current(
    path: Path, cfg_hash: str, md5: str, prev_md5: str, next_present: bool
) -> bool:
    """True if the silver file exists and was built from exactly the current inputs and method.

    Any mismatch means a rebuild: new code (METHOD_VERSION), new settings (cfg_hash), a changed
    source file (md5), a changed previous year (prev_md5, used by the haze look-back), or the next
    year's bronze appearing/disappearing (decides who owns 31-December slot candidates).
    """
    if not path.exists():
        return False
    meta = read_parquet_metadata(path)
    return (
        meta.get("method_version") == METHOD_VERSION
        and meta.get("config_hash") == cfg_hash
        and meta.get("source_md5") == md5
        and meta.get("prev_source_md5") == prev_md5
        and meta.get("next_year_present") == ("1" if next_present else "0")
    )


def run_process(
    ctx: RunContext, stations: list[str], years: list[int], force: bool = False
) -> ProcessSummary:
    """Build silver for every requested station-year that is missing or out of date.

    `force=True` rebuilds everything (e.g. after changing the code without bumping
    METHOD_VERSION). Station-years are independent, so one failure is logged and skipped.
    """
    summary = ProcessSummary()
    cfg_hash = silver_config_hash(ctx.settings)
    for station in stations:
        for year in years:
            bpath = bronze_path(ctx.root, year, station)
            if not bpath.exists():
                summary.missing_bronze += 1
                continue
            prev_path = bronze_path(ctx.root, year - 1, station)
            next_present = bronze_path(ctx.root, year + 1, station).exists()
            md5, prev_md5 = _bronze_md5(bpath), _bronze_md5(prev_path)
            spath = silver_path(ctx.root, year, station)
            if not force and _silver_is_current(spath, cfg_hash, md5, prev_md5, next_present):
                summary.skipped += 1
                summary.paths.append(spath)
                continue
            try:  # one bad station-year never aborts the stage; the previous silver file stays
                previous = pl.read_parquet(prev_path) if prev_path.exists() else None
                df = build_station_year(
                    pl.read_parquet(bpath),
                    previous,
                    year,
                    ctx.settings,
                    next_year_exists=next_present,
                )
                write_silver(
                    df,
                    spath,
                    run_id=ctx.run_id,
                    config_hash=cfg_hash,
                    source_md5=md5,
                    prev_source_md5=prev_md5,
                    next_year_present=next_present,
                )
            except Exception as exc:
                log.error("silver %s %s failed: %s", station, year, exc)
                summary.failed += 1
                summary.failures.append(f"{station}/{year}: {exc!r}"[:300])
                continue
            summary.built += 1
            summary.paths.append(spath)
    return summary


def all_silver_paths(root: Path, stations: list[str], years: list[int]) -> list[Path]:
    """Existing silver files for these stations and years (missing station-years are skipped)."""
    paths = [silver_path(root, y, s) for s in stations for y in years]
    return [p for p in paths if p.exists()]


def run_aggregate(ctx: RunContext, stations: list[str], years: list[int]) -> Path:
    """Build a new gold version from the current silver files; returns its folder."""
    return build_gold(ctx.settings, all_silver_paths(ctx.root, stations, years), ctx.run_id)
