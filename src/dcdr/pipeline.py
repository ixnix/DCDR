"""Stage runner: run context, incremental silver decisions and run records (spec §11-12)."""

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
from dcdr.config import Settings, canonical_yaml, config_hash, silver_config_hash
from dcdr.gold import build_gold
from dcdr.ingest import IngestSummary, bronze_path, ingest, make_store
from dcdr.silver import METHOD_VERSION
from dcdr.silver.build import build_station_year, write_silver
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
    settings: Settings
    run_id: str
    command: str
    started_at: datetime
    git_commit: str
    git_dirty: bool

    @property
    def root(self) -> Path:
        return self.settings.data_root

    @property
    def state_dir(self) -> Path:
        return self.root / "_state"


def start_run(settings: Settings, command: str) -> RunContext:
    settings.data_root.mkdir(parents=True, exist_ok=True)
    commit, dirty = git_revision(PACKAGE_DIR)
    ctx = RunContext(settings, new_run_id(), command, utcnow(), commit, dirty)
    atomic_write_bytes(
        ctx.state_dir / "runs" / ctx.run_id / "config.resolved.yaml",
        canonical_yaml(settings).encode(),
    )
    log.info("run %s started (%s) commit=%s dirty=%s", ctx.run_id, command, commit[:8], dirty)
    return ctx


def finish_run(ctx: RunContext, status: str, summary: dict[str, Any]) -> None:
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
            "summary": json.dumps(summary, default=str),
        },
    )


def read_station_list(path: Path) -> list[str]:
    with path.open(newline="") as fh:
        return [row["station_id"].strip() for row in csv.DictReader(fh)]


def parse_years(text: str) -> list[int]:
    years: list[int] = []
    for part in text.split(","):
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            years.extend(range(lo, hi + 1))
        else:
            years.append(int(part))
    return sorted(set(years))


def run_ingest(
    ctx: RunContext, stations: list[str], years: list[int], store: Any = None
) -> IngestSummary:
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
class SilverSummary:
    built: int = 0
    skipped: int = 0
    missing_bronze: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)


def silver_path(root: Path, year: int, station: str) -> Path:
    return root / "silver" / f"year={year}" / f"{station}.parquet"


def _bronze_md5(path: Path) -> str:
    return read_parquet_metadata(path).get("source_md5", "") if path.exists() else ""


def _silver_is_current(
    path: Path, cfg_hash: str, md5: str, prev_md5: str, next_present: bool
) -> bool:
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


def run_silver(
    ctx: RunContext, stations: list[str], years: list[int], force: bool = False
) -> SilverSummary:
    summary = SilverSummary()
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
    paths = [silver_path(root, y, s) for s in stations for y in years]
    return [p for p in paths if p.exists()]


def run_gold(ctx: RunContext, stations: list[str], years: list[int]) -> Path:
    return build_gold(ctx.settings, all_silver_paths(ctx.root, stations, years), ctx.run_id)
