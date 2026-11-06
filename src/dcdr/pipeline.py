"""Stage runner: run context, incremental silver decisions and run records (spec §11-12).

Where this fits
---------------
`cli.py` calls the functions here; they call the stage modules:

    run_ingest    -> ingest.py               NOAA S3 -> bronze
    run_process   -> process/build.py        bronze  -> silver   (station by station, or in parallel)
    run_aggregate -> aggregate.py            silver  -> gold     (all stations at once)
    run_rebuild_statistics -> aggregate.py   gold    -> gold     (monthly, yearly, decadal only)

and `start_run` / `finish_run` wrap every command with a run ID and a run-log entry.

Incremental processing (the key idea in `run_process`)
------------------------------------------------------
A silver file is rebuilt only if something that affects it changed. Every silver file stores, in
its Parquet metadata, what it was built from: the method version, the silver config hash, the MD5
of its bronze input (and of the previous year's, used for the haze look-back), and the station
guards. If all of these still match, the file is skipped. So re-running
`dcdr run` after adding one new year rebuilds only what that year touches.

After a method change, a file the change cannot affect is carried forward instead of rebuilt,
if the change declares a migration (targeted rebuild, process/migrations.py).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import polars as pl

from dcdr import __version__
from dcdr.aggregate import build_gold, rebuild_statistics
from dcdr.config import Settings, canonical_yaml, config_hash, silver_config_hash
from dcdr.ingest import IngestSummary, bronze_path, ingest, make_store
from dcdr.process import METHOD_VERSION
from dcdr.process.build import build_station_year, fill_key, write_silver
from dcdr.process.guard import station_scan
from dcdr.process.migrations import (
    MIGRATIONS,
    Migration,
    effective_version,
    ledger_by_station,
    ledger_path,
    read_ledger,
    update_ledger,
)
from dcdr.process.parse import NoPositionError
from dcdr.state import (
    Manifest,
    append_run_record,
    atomic_write_bytes,
    git_revision,
    new_run_id,
    read_parquet_metadata,
    safe_listdir,
    utcnow,
)
from dcdr.stations import load_isd_history

log = logging.getLogger(__name__)
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"  # console log lines (CLI, workers)
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
    no_position: int = 0  # not built: no usable position (user decision 2026-11-04)
    removed: int = 0  # of those, silver files from an earlier build that were deleted
    carried: int = 0  # files of an earlier method carried forward (process/migrations.py)
    failures: list[str] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)  # every current silver file
    carried_rows: list[dict[str, Any]] = field(default_factory=list)  # new ledger rows
    rebuilt: list[tuple[str, int]] = field(default_factory=list)  # built or removed station-years


def silver_path(root: Path, year: int, station: str) -> Path:
    """Where one station-year lives in silver (same layout as bronze)."""
    return root / "silver" / f"year={year}" / f"{station}.parquet"


def _bronze_md5(path: Path) -> str:
    """MD5 of the NOAA source behind a bronze file ("" if there is no such file)."""
    return read_parquet_metadata(path).get("source_md5", "") if path.exists() else ""


def _silver_state(
    meta: dict[str, str],
    ledger_row: dict[str, Any] | None,
    inputs: dict[str, str],
    migration: Migration | None,
    affected: bool,
) -> str:
    """ "current", "carry" or "rebuild" for an existing silver file (its metadata `meta`).

    The inputs must match in every case: the silver settings (config_hash), the source file
    (source_md5), the previous year (prev_source_md5, haze look-back), the station guards, and the
    isd-history longitude that missing positions took (lon_fill; "" = none needed, so isd-history
    does not matter). Then the file is current if it counts as the running METHOD_VERSION
    (built by it, or carried to it: `effective_version`), and carried forward if it counts as the
    migration's `from_version` and its station is not affected by the change.
    """
    for key, value in inputs.items():
        if key == "lon_fill":
            if meta.get(key, "") not in ("", value):
                return "rebuild"
        elif meta.get(key, "") != value:
            return "rebuild"
    version = effective_version(meta, ledger_row)
    if version == METHOD_VERSION:
        return "current"
    if migration is not None and version == migration.from_version and not affected:
        return "carry"
    return "rebuild"


def run_process(
    ctx: RunContext,
    stations: list[str],
    years: list[int],
    force: bool = False,
    workers: int = 1,
) -> ProcessSummary:
    """Build silver for every requested station-year that is missing or out of date.

    `force=True` rebuilds everything (e.g. after changing the code without bumping
    METHOD_VERSION). Station-years are independent, so one failure is logged and skipped.

    `workers > 1` builds that many stations at a time, each in its own process (stations are
    independent; results are identical to `workers=1`). Each worker runs Polars on one thread,
    so the workers, not Polars, share out the CPU cores.
    """
    job = _StationJob(ctx.settings, ctx.run_id, tuple(years), force, MIGRATIONS.get(METHOD_VERSION))
    summary = ProcessSummary()
    lons = _isd_longitudes(ctx.settings)
    carried = ledger_by_station(read_ledger(ledger_path(ctx.root)))
    args = (stations, [lons.get(s) for s in stations], [carried.get(s, {}) for s in stations])
    try:
        if workers <= 1:
            _collect(summary, map(job, *args), len(stations))
            return summary
        with (
            _env(POLARS_MAX_THREADS="1"),  # inherited by the worker processes when they start
            ProcessPoolExecutor(
                max_workers=workers, mp_context=get_context("spawn"), initializer=_worker_logging
            ) as pool,
        ):
            try:
                _collect(summary, pool.map(job, *args), len(stations))
            except KeyboardInterrupt:  # Ctrl-C: drop the queued stations instead of finishing them
                pool.shutdown(wait=False, cancel_futures=True)
                raise
        return summary
    finally:  # also after Ctrl-C: the files carried so far stay carried
        update_ledger(ledger_path(ctx.root), summary.carried_rows, summary.rebuilt)


def _isd_longitudes(settings: Settings) -> dict[str, float]:
    """Each station's isd-history longitude (stations without a position left out).

    Without the file (e.g. a scratch data folder) nothing is filled from it; silver files record
    that ("none" in `lon_fill`), so they are rebuilt once the file is there.
    """
    if not settings.isd_history_path.exists():
        log.warning(
            "no isd-history at %s: positions are not filled from it", settings.isd_history_path
        )
        return {}
    h = load_isd_history(settings.isd_history_path).drop_nulls("isd_lon")
    return dict(zip(h["station_id"].to_list(), h["isd_lon"].to_list(), strict=True))


PROGRESS_EVERY = 500  # stations between progress lines in the log


def _collect(summary: ProcessSummary, parts: Iterator[ProcessSummary], total: int) -> None:
    """Add each station's summary to the run's summary, in station order, logging progress."""
    for i, part in enumerate(parts, 1):
        summary.built += part.built
        summary.skipped += part.skipped
        summary.missing_bronze += part.missing_bronze
        summary.failed += part.failed
        summary.no_position += part.no_position
        summary.removed += part.removed
        summary.carried += part.carried
        summary.carried_rows += part.carried_rows
        summary.rebuilt += part.rebuilt
        summary.failures += part.failures
        summary.paths += part.paths
        if i % PROGRESS_EVERY == 0 or i == total:
            log.info(
                "silver: %d/%d stations (built %d, carried %d, skipped %d, no position %d, failed %d)",
                i,
                total,
                summary.built,
                summary.carried,
                summary.skipped,
                summary.no_position,
                summary.failed,
            )


@contextmanager
def _env(**values: str) -> Iterator[None]:
    """Set environment variables for the duration of a block, then restore them."""
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _worker_logging() -> None:
    """A worker process starts with no log set-up; give it the same console format as the CLI."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


@dataclass(frozen=True)
class _StationJob:
    """Build silver for all requested years of one station. A plain picklable object, so it can
    be sent to a worker process (`run_process` with workers > 1)."""

    settings: Settings
    run_id: str
    years: tuple[int, ...]
    force: bool
    migration: Migration | None = None  # the running version's migration, if it declares one

    def __call__(
        self,
        station: str,
        isd_lon: float | None = None,
        carried: dict[int, dict[str, Any]] | None = None,
    ) -> ProcessSummary:
        """`isd_lon`: the station's isd-history longitude; `carried`: its ledger rows by year."""
        summary = ProcessSummary()
        root = self.settings.data_root
        cfg_hash = silver_config_hash(self.settings)
        # Station guards for inferred "nothing significant" (rule V3 for SYNOP; the METAR guard
        # for METARs without full text), from ALL bronze years of the station on disk: the
        # evidence (iX, full METAR text) exists only in recent years; older years rely on it.
        # The same pass tells whether the migration's change can affect this station.
        years_on_disk = sorted(
            p.name for p in safe_listdir(root / "bronze") if p.name.startswith("year=")
        )
        guard, metar_guard, affected = station_scan(
            [
                p
                for y in years_on_disk
                if (p := root / "bronze" / y / f"{station}.parquet").exists()
            ],
            self.settings,
            isd_lon,
            self.migration.affects if self.migration else None,
        )
        flags = {True: "1", False: "0", None: ""}
        for year in self.years:
            bpath = bronze_path(root, year, station)
            if not bpath.exists():
                summary.missing_bronze += 1
                continue
            prev_path = bronze_path(root, year - 1, station)
            md5, prev_md5 = _bronze_md5(bpath), _bronze_md5(prev_path)
            spath = silver_path(root, year, station)
            if not self.force and spath.exists():
                meta = read_parquet_metadata(spath)
                inputs = {
                    "config_hash": cfg_hash,
                    "source_md5": md5,
                    "prev_source_md5": prev_md5,
                    "nil_guard": flags[guard],
                    "metar_guard": flags[metar_guard],
                    "lon_fill": "none" if isd_lon is None else str(isd_lon),
                }
                row = (carried or {}).get(year)
                state = _silver_state(meta, row, inputs, self.migration, affected)
                if state == "current":
                    summary.skipped += 1
                    summary.paths.append(spath)
                    continue
                if state == "carry" and self.migration is not None:
                    summary.carried += 1
                    summary.paths.append(spath)
                    summary.carried_rows.append(
                        {
                            "station_id": station,
                            "year": year,
                            "built_version": meta.get("method_version", ""),
                            "built_run_id": meta.get("run_id", ""),
                            "valid_for": self.migration.to_version,
                            "migration": f"{self.migration.from_version} -> "
                            f"{self.migration.to_version}",
                            "carried_run_id": self.run_id,
                        }
                    )
                    continue
            try:  # one bad station-year never aborts the stage; its previous silver file stays
                previous = pl.read_parquet(prev_path) if prev_path.exists() else None
                df = build_station_year(
                    pl.read_parquet(bpath),
                    previous,
                    year,
                    self.settings,
                    nil_guard=guard,
                    metar_guard=metar_guard,
                    isd_lon=isd_lon,
                )
                write_silver(
                    df,
                    spath,
                    run_id=self.run_id,
                    config_hash=cfg_hash,
                    source_md5=md5,
                    prev_source_md5=prev_md5,
                    nil_guard=guard,
                    metar_guard=metar_guard,
                    lon_fill=fill_key(df, isd_lon),
                )
            except NoPositionError:
                # Not built, and an older file (e.g. a 0/0 year built under 2.0.0) must go too, or
                # gold would still read it.
                summary.no_position += 1
                if spath.exists():
                    spath.unlink()
                    summary.removed += 1
                    summary.rebuilt.append((station, year))
                    log.warning("silver %s %s: no position, removed the old file", station, year)
                continue
            except Exception as exc:
                log.error("silver %s %s failed: %s", station, year, exc)
                summary.failed += 1
                summary.failures.append(f"{station}/{year}: {exc!r}"[:300])
                continue
            summary.built += 1
            summary.paths.append(spath)
            summary.rebuilt.append((station, year))
        return summary


def all_silver_paths(root: Path, stations: list[str], years: list[int]) -> list[Path]:
    """Existing silver files for these stations and years (missing station-years are skipped)."""
    paths = [silver_path(root, y, s) for s in stations for y in years]
    return [p for p in paths if p.exists()]


def run_aggregate(ctx: RunContext, stations: list[str], years: list[int]) -> Path:
    """Build a new gold version from the current silver files; returns its folder."""
    return build_gold(ctx.settings, all_silver_paths(ctx.root, stations, years), ctx.run_id)


def run_rebuild_statistics(ctx: RunContext) -> Path:
    """A new gold version with only the statistics recomputed (aggregate.rebuild_statistics)."""
    return rebuild_statistics(ctx.settings, ctx.run_id)


@dataclass
class VerifySummary:
    """Result of `run_verify_silver`."""

    carried: int = 0  # ledger rows carried to the running version (the population)
    checked: int = 0  # of those, rebuilt and compared
    different: list[str] = field(default_factory=list)  # "station/year" whose data differ


def run_verify_silver(ctx: RunContext, sample: int, seed: int = 0) -> VerifySummary:
    """Rebuild a random sample of carried silver files with the running code and compare.

    Each file is rebuilt from its own bronze inputs and station guards (its metadata; they were
    checked when it was carried) and must equal the file's data exactly. Any difference means
    the migration's `affects` missed a change: remove the migration and rebuild in full.
    """
    ledger = read_ledger(ledger_path(ctx.root)).filter(pl.col("valid_for") == METHOD_VERSION)
    lons = _isd_longitudes(ctx.settings)
    rows = list(ledger.iter_rows(named=True))
    random.Random(seed).shuffle(rows)  # sample first: only the picked files are opened
    picked: list[tuple[str, int, Path]] = []
    for row in rows:
        if len(picked) == sample:
            break
        path = silver_path(ctx.root, row["year"], row["station_id"])
        if path.exists() and effective_version(read_parquet_metadata(path), row) == METHOD_VERSION:
            picked.append((row["station_id"], row["year"], path))  # a row still matching its file
    out = VerifySummary(carried=len(rows))
    flag = {"1": True, "0": False, "": None}
    for station, year, path in sorted(picked):
        meta = read_parquet_metadata(path)
        prev = bronze_path(ctx.root, year - 1, station)
        df = build_station_year(
            pl.read_parquet(bronze_path(ctx.root, year, station)),
            pl.read_parquet(prev) if prev.exists() else None,
            year,
            ctx.settings,
            nil_guard=flag[meta.get("nil_guard", "")],
            metar_guard=flag[meta.get("metar_guard", "")],
            isd_lon=lons.get(station),
        )
        out.checked += 1
        if not pl.read_parquet(path).equals(df):
            out.different.append(f"{station}/{year}")
            log.error("verify-silver: %s %s differs from a rebuild", station, year)
    return out
