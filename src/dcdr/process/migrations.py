"""Targeted rebuild: carry silver files forward across a method change they cannot be affected by.

Why
---
Every silver file records the method version that built it, and a file counts as current only if
that is the running code's METHOD_VERSION. So any rule change, however narrow, made every file
stale: 2.1.0 changes 0.65 % of station-years, but a full rebuild takes 13-16 hours.

How
---
A method change may declare a `Migration`: files built by `from_version` stay valid under
`to_version` at every station the change cannot affect. `affects` says, from one bronze
station-year, whether the change can alter the station's silver (any year affected -> the whole
station is rebuilt, because the station guards use all its years). `dcdr process` then:

    file current (built by this version, or carried to it, same inputs)  -> skipped
    file from `from_version`, same inputs, station not affected           -> carried forward
    anything else                                                         -> rebuilt

A carried file is not rewritten: it keeps the metadata of the build that made it, and a row in
the ledger (`_state/silver_carried.parquet`) says it is valid for the newer version. A ledger row
counts only while it matches the file (same building version and run ID), so a file rebuilt
later is judged by its own metadata again. Gold and `dcdr process` use `effective_version`.

`dcdr verify-silver` checks the claim: it rebuilds a random sample of carried files with the
running code and compares them with the files on disk. A difference means the migration's
`affects` is wrong; then remove the migration and rebuild in full. A sample is evidence, not
proof; before a published release, rebuild in full (`dcdr process --force`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from dcdr.state import atomic_write_parquet


@dataclass(frozen=True)
class Migration:
    """Files built by `from_version` stay valid under `to_version` where `affects` is False."""

    from_version: str
    to_version: str
    reason: str  # what changed, and why stations without `affects` cannot see it
    affects: Callable[[pl.DataFrame], bool]  # one bronze station-year -> can the change alter it?


def _report_without_full_position(raw: pl.DataFrame) -> bool:
    """2.0.0 -> 2.1.0 changed only what happens to reports without a complete in-file position
    (missing, out of range, 0/0): their longitude, whether their year is built, and the guards
    of their station. A station whose reports all have one builds exactly as before."""
    if raw.height == 0:
        return False
    lat = pl.col("LATITUDE").cast(pl.Float64, strict=False) if "LATITUDE" in raw.columns else None
    lon = pl.col("LONGITUDE").cast(pl.Float64, strict=False) if "LONGITUDE" in raw.columns else None
    if lat is None or lon is None:
        return True
    ok = lat.is_between(-90, 90) & lon.is_between(-180, 180) & ~((lat == 0) & (lon == 0))
    return not bool(raw.select(ok.fill_null(False).all()).item())


MIGRATIONS: dict[str, Migration] = {  # keyed by the version they lead to
    "2.1.0": Migration(
        "2.0.0",
        "2.1.0",
        "positions: 0/0 is missing, isd-history fills, no station-year without a position",
        _report_without_full_position,
    ),
}

LEDGER_SCHEMA = {
    "station_id": pl.String,
    "year": pl.Int64,
    "built_version": pl.String,  # the version that built the file (its own metadata)
    "built_run_id": pl.String,  # the run that built it: identifies the file
    "valid_for": pl.String,  # the version the file is carried to
    "migration": pl.String,  # e.g. "2.0.0 -> 2.1.0"
    "carried_run_id": pl.String,
}


def ledger_path(root: Path) -> Path:
    """Where the ledger of carried silver files lives, under the data root."""
    return root / "_state" / "silver_carried.parquet"


def read_ledger(path: Path) -> pl.DataFrame:
    """The ledger (one row per carried station-year), empty if there is none yet."""
    if not path.exists():
        return pl.DataFrame(schema=LEDGER_SCHEMA)
    return pl.read_parquet(path)


def ledger_by_station(ledger: pl.DataFrame) -> dict[str, dict[int, dict[str, Any]]]:
    """The ledger as {station: {year: row}}, built once per run (a station job gets its part)."""
    out: dict[str, dict[int, dict[str, Any]]] = {}
    for r in ledger.iter_rows(named=True):
        out.setdefault(r["station_id"], {})[int(r["year"])] = r
    return out


def effective_version(meta: dict[str, str], row: dict[str, Any] | None) -> str:
    """The version a silver file counts as: its ledger row's, if that row matches the file."""
    built = meta.get("method_version", "")
    if row and row["built_version"] == built and row["built_run_id"] == meta.get("run_id", ""):
        return str(row["valid_for"])
    return built


def update_ledger(
    path: Path,
    carried: Iterable[dict[str, object]],
    rebuilt: Iterable[tuple[str, int]],
) -> None:
    """Add or replace the rows of newly carried files; drop rows of files rebuilt or removed."""
    new = pl.DataFrame(list(carried), schema=LEDGER_SCHEMA)
    gone = {f"{s}/{y}" for s, y in rebuilt} | set(
        (new["station_id"] + "/" + new["year"].cast(pl.String)).to_list()
    )
    if not gone and new.height == 0:
        return
    old = read_ledger(path)
    key = pl.col("station_id") + "/" + pl.col("year").cast(pl.String)
    out = pl.concat([old.filter(~key.is_in(list(gone))), new]).sort("station_id", "year")
    atomic_write_parquet(out.to_arrow(), path)
