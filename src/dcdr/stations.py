"""Station-position inputs for the gold `stations` table (docs/rules.md, "Station positions").

Two hand-maintained files feed the decision which station position to trust:

* NOAA's `isd-history.csv`: one row per station (USAF + WBAN) with its listed position. A saved
  copy lives in the data root; the pipeline never downloads it. Empty positions, and the 0/0
  placeholders (e.g. mobile "WXPOD" units), count as "no position" and are never used for
  verifying (user decision 2026-11-03).
* The override file (`config/stations/position_overrides.csv`, versioned in git): the author's
  decision for single stations, each with a reason (user decision 2026-11-03).

Both are validated here, so a bad row stops the gold build with a clear message instead of
placing a station somewhere wrong.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import polars as pl

ISD_HISTORY_URL = "https://noaa-isd-pds.s3.amazonaws.com/isd-history.csv"
OVERRIDE_COLUMNS = ["station_id", "lat", "lon", "action", "reason", "decided"]
OVERRIDE_ACTIONS = ("use", "exclude")


class MissingIsdHistoryError(FileNotFoundError):
    """The saved isd-history.csv is not where the settings say."""


class OverrideError(ValueError):
    """A row of the station-position override file is invalid."""


def file_md5(path: Path) -> str:
    """MD5 of a file as hex, or "" if it does not exist (recorded in gold's lineage)."""
    return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else ""


def load_isd_history(path: Path) -> pl.DataFrame:
    """isd-history as `station_id, isd_name, isd_lat, isd_lon` (positions null when unknown)."""
    if not path.exists():
        raise MissingIsdHistoryError(
            f"isd-history not found at {path}; save NOAA's {ISD_HISTORY_URL} there "
            "(config stations.isd_history, relative to the data root)"
        )
    raw = pl.read_csv(path, infer_schema_length=0)
    h = raw.select(
        (pl.col("USAF").str.strip_chars() + pl.col("WBAN").str.strip_chars()).alias("station_id"),
        pl.col("STATION NAME").str.strip_chars().alias("isd_name"),
        pl.col("LAT").str.strip_chars().cast(pl.Float64, strict=False).alias("isd_lat"),
        pl.col("LON").str.strip_chars().cast(pl.Float64, strict=False).alias("isd_lon"),
    )
    dup = h.filter(pl.col("station_id").is_duplicated())["station_id"].unique().sort()
    if dup.len():
        raise ValueError(f"isd-history lists stations more than once: {dup.to_list()[:10]}")
    # no position: either field missing, or the 0/0 placeholder
    unknown = (
        pl.col("isd_lat").is_null()
        | pl.col("isd_lon").is_null()
        | ((pl.col("isd_lat") == 0) & (pl.col("isd_lon") == 0))
    )
    return h.with_columns(
        isd_lat=pl.when(unknown).then(None).otherwise(pl.col("isd_lat")),
        isd_lon=pl.when(unknown).then(None).otherwise(pl.col("isd_lon")),
    )


def load_overrides(path: Path) -> pl.DataFrame:
    """The override file, validated; an empty frame (same columns) if there is no file."""
    schema = {
        "station_id": pl.String,
        "lat": pl.Float64,
        "lon": pl.Float64,
        "action": pl.String,
        "reason": pl.String,
        "decided": pl.String,
    }
    if not path.exists():
        return pl.DataFrame(schema=schema)
    raw = pl.read_csv(path, infer_schema_length=0)
    missing = [c for c in OVERRIDE_COLUMNS if c not in raw.columns]
    if missing:
        raise OverrideError(f"{path}: missing columns {missing}")
    o = raw.select([pl.col(c).str.strip_chars() for c in OVERRIDE_COLUMNS]).with_columns(
        lat=pl.col("lat").cast(pl.Float64, strict=False),
        lon=pl.col("lon").cast(pl.Float64, strict=False),
    )
    use = pl.col("action") == "use"
    sid = pl.col("station_id").fill_null("")
    problems = {
        "no station_id": sid == "",
        # USAF + WBAN; also catches an ID whose leading zero a spreadsheet dropped
        "station_id not 11 digits": (sid != "") & ~sid.str.contains(r"^\d{11}$"),
        "no reason": pl.col("reason").fill_null("") == "",
        "listed more than once": pl.col("station_id").is_duplicated(),
        "unknown action (use or exclude)": ~pl.col("action")
        .is_in(OVERRIDE_ACTIONS)
        .fill_null(False),
        "use needs lat in -90..90": use & ~pl.col("lat").is_between(-90, 90).fill_null(False),
        "use needs lon in -180..180": use & ~pl.col("lon").is_between(-180, 180).fill_null(False),
    }
    for what, bad in problems.items():
        rows = o.with_row_index("row", offset=2).filter(bad)  # file line numbers (header = 1)
        if rows.height:
            where = [f"line {r}: {s}" for r, s in zip(rows["row"], rows["station_id"], strict=True)]
            raise OverrideError(f"{path}: {what}: {where}")
    return o.select(OVERRIDE_COLUMNS).cast(schema)  # type: ignore[arg-type]
