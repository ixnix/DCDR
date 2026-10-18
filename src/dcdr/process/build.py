"""Assemble one silver station-year from bronze (spec §7.2)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import polars as pl

from dcdr.config import Settings
from dcdr.process import METHOD_VERSION
from dcdr.process.classify import apply_haze_and_rh, classify_reports
from dcdr.process.parse import add_time_columns, parse_reports
from dcdr.process.qc import apply_qc
from dcdr.process.sampling import select_synoptic
from dcdr.schemas import validate_silver
from dcdr.state import atomic_write_parquet

log = logging.getLogger(__name__)
PROVENANCE = {"source_key", "source_md5", "ingest_run_id"}
_SORT = ["time_utc", "report_type", "source", "row_key"]
CORE_COLUMNS = [
    "station_id",
    "time_utc",
    "time_lmst",
    "lmst_date",
    "lmst_hour",
    "summary_lmst_date",
    "report_type",
    "source",
    "lat",
    "lon",
    "lon_used",
    "elev",
    "slot_time",
    "slot_lmst_date",
    "slot_delta_min",
    "is_candidate",
    "is_excluded_type",
    "selection_rank",
    "selection_reason",
    "is_synoptic_selected",
    "has_pw",
    "dust_rank",
    "dust_class_rule",
    "dust_rule_id",
    "dust_class_final",
    "dust_haze",
    "rh_screened",
    "flag_du",
    "flag_duhz",
    "flag_susp",
    "flag_bd",
    "flag_ds",
    "flag_vicinity",
    "flag_past",
    "flag_ambiguous",
    "flag_daily_dust",
    "pw_evidence",
    "wsd",
    "use_wsd",
    "wind_dir",
    "use_wind_dir",
    "gust",
    "use_gust",
    "gust_src",
    "vis_raw",
    "vis_capped",
    "use_vis",
    "t",
    "use_t",
    "td",
    "use_td",
    "rh",
    "slp",
    "use_slp",
    "parse_status",
    "dup_count",
    "row_key",
    "source_key",
]


def _row_keys(raw: pl.DataFrame) -> pl.Series:
    cols = sorted(c for c in raw.columns if c not in PROVENANCE)
    joined = raw.select(
        pl.concat_str([pl.col(c).fill_null("\x00") for c in cols], separator="\x1f")
    ).to_series()
    return pl.Series(
        "row_key", [hashlib.sha1(s.encode()).hexdigest() for s in joined], dtype=pl.String
    )


def previous_year_tail(prev: pl.DataFrame, year: int) -> pl.DataFrame:
    return prev.filter(pl.col("DATE") >= f"{year - 1}-12-31T00:00:00")


def dedupe(raw: pl.DataFrame) -> pl.DataFrame:
    raw = raw.with_columns(_row_keys(raw))
    counts = raw.group_by("row_key").agg(pl.len().cast(pl.Int32).alias("dup_count"))
    return raw.unique(subset="row_key", keep="first", maintain_order=True).join(
        counts, on="row_key"
    )


def build_station_year(
    current: pl.DataFrame,
    previous: pl.DataFrame | None,
    year: int,
    settings: Settings,
    *,
    next_year_exists: bool = False,
) -> pl.DataFrame:
    frames = [current] if previous is None else [previous_year_tail(previous, year), current]
    raw = dedupe(pl.concat(frames, how="diagonal_relaxed"))
    df = add_time_columns(parse_reports(raw))
    bad = df.filter(pl.col("time_utc").is_null()).height
    if bad:
        log.warning("dropping %d rows with unparseable DATE", bad)
        df = df.filter(pl.col("time_utc").is_not_null())
    df = apply_qc(df, settings.qc, settings.parse)
    df = df.sort(_SORT, nulls_last=True).with_columns(pl.col("time_utc").set_sorted())
    df = classify_reports(df, settings.classify, settings.qc)
    df = apply_haze_and_rh(df, settings.classify, settings.qc)
    df = select_synoptic(df, settings.sampling)
    # Each report lives in exactly one station-year file: slot candidates belong to their SLOT's
    # year (a 23:56 Dec-31 report competes for the Jan-1 00:00 slot), others to their own year.
    owner_year = (
        pl.when(pl.col("is_candidate"))
        .then(pl.col("slot_time").dt.year())
        .otherwise(pl.col("time_utc").dt.year())
    )
    # ...unless the next year's file will not exist: then keep them here so nothing is lost.
    keep = (owner_year == year) | (pl.lit(not next_year_exists) & (owner_year == year + 1))
    df = df.filter(keep).sort(_SORT, nulls_last=True)
    rest = sorted(c for c in df.columns if c not in CORE_COLUMNS)
    return df.select(CORE_COLUMNS + rest)


def write_silver(
    df: pl.DataFrame,
    path: Path,
    *,
    run_id: str,
    config_hash: str,
    source_md5: str,
    prev_source_md5: str,
    next_year_present: bool = False,
) -> None:
    validate_silver(df)
    atomic_write_parquet(
        df.to_arrow(),
        path,
        {
            "dcdr_layer": "silver",
            "method_version": METHOD_VERSION,
            "config_hash": config_hash,
            "run_id": run_id,
            "source_md5": source_md5,
            "prev_source_md5": prev_source_md5,
            "next_year_present": "1" if next_year_present else "0",
        },
    )
