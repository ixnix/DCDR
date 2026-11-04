"""Assemble one silver station-year from bronze (spec §7.2).

This is the conductor of the process step: it runs the sub-steps in order and writes the result.

    bronze rows (text) for year Y, plus the last day of year Y-1 (for the haze look-back)
      -> dedupe            collapse exact duplicate rows, give every report a stable `row_key`
      -> parse_reports     decode ISD's packed text fields into typed columns     (parse.py)
      -> add_time_columns  local mean solar time and local dates                  (parse.py)
      -> apply_qc          quality flags, RH, capped visibility, gusts            (qc.py)
      -> classify_reports  dust class per report from weather codes               (classify.py)
      -> apply_haze_and_rh haze-after-dust rule and humidity screen               (classify.py)
      -> add_source_group  SYNOP / METAR / other; summaries excluded               (sources.py)
      -> keep this year's reports (by UTC year), put core columns first
    silver rows (typed, flagged, classified) -> validated -> written with provenance metadata

Every function here works on a whole Polars DataFrame at once (vectorised), never row by row.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import polars as pl

from dcdr.config import Settings
from dcdr.process import METHOD_VERSION
from dcdr.process.classify import apply_haze_and_rh, classify_reports
from dcdr.process.derived import synop_is_derived
from dcdr.process.parse import NoPositionError, add_time_columns, has_position, parse_reports
from dcdr.process.qc import apply_qc
from dcdr.process.sources import add_source_group
from dcdr.schemas import validate_silver
from dcdr.state import atomic_write_parquet

log = logging.getLogger(__name__)
# Columns added by ingest to describe WHERE a row came from, not WHAT was observed. They are
# left out of the row fingerprint, so re-downloading the same data gives the same row keys.
PROVENANCE = {"source_key", "source_md5", "ingest_run_id"}
# Deterministic row order: time first, then tie-breakers, so output never depends on input order.
_SORT = ["time_utc", "report_type", "source", "row_key"]
# The most useful columns, placed first in every silver file (in this order); all other columns
# (raw decoded fields such as mw1_code) follow alphabetically.
CORE_COLUMNS = [
    "station_id",
    "time_utc",
    "time_lmst",
    "lmst_date",
    "lmst_hour",
    "summary_lmst_date",
    "report_type",
    "source_group",
    "source",
    "lat",
    "lon",
    "lon_used",
    "elev",
    "is_excluded_type",
    "has_pw",
    "pw_status",
    "syn_ix",
    "metar_text",
    "metar_auto",
    "metar_wx",
    "metar_rmk_dust",
    "synop_derived",
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
    """A fingerprint (SHA-1) of each row's content: identical rows get identical keys.

    All non-provenance columns are joined into one string per row (columns in sorted order, so the
    key doesn't depend on column order). Nulls become "\\x00" and the separator is "\\x1f" (unit
    separator), characters that never appear in ISD text, so different rows can't collide by
    accident, e.g. ("a,b", "c") vs ("a", "b,c").
    """
    cols = sorted(c for c in raw.columns if c not in PROVENANCE)
    joined = raw.select(
        pl.concat_str([pl.col(c).fill_null("\x00") for c in cols], separator="\x1f")
    ).to_series()
    return pl.Series(
        "row_key", [hashlib.sha1(s.encode()).hexdigest() for s in joined], dtype=pl.String
    )


def previous_year_tail(prev: pl.DataFrame, year: int) -> pl.DataFrame:
    """Rows of 31 December of the previous year (ISO date strings compare correctly as text).

    Needed so the 24-hour haze look-back works for reports in the first hours of 1 January.
    """
    return prev.filter(pl.col("DATE") >= f"{year - 1}-12-31T00:00:00")


def dedupe(raw: pl.DataFrame) -> pl.DataFrame:
    """Keep one copy of rows that are identical in every source column; `dup_count` says how many.

    NOAA's merged files sometimes contain the same report twice. Rows that share a timestamp but
    differ in any value are NOT duplicates: both stay (gold judges every report in its bin).
    """
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
    nil_guard: bool | None = None,
    metar_guard: bool | None = None,
) -> pl.DataFrame:
    """Turn one station-year of bronze into silver (in memory; `write_silver` saves it).

    current          bronze rows of `year`
    previous         bronze rows of `year - 1` (or None): its last day is processed with this
                     year (haze look-back), then dropped
    nil_guard        the station's guard for inferred "nothing significant" (process/guard.py):
                     True allows rule V3, False/None do not
    metar_guard      the station's METAR guard: True allows the inference for METARs without
                     full text (process/guard.py)
    The `*` makes the guards keyword-only, so call sites must spell out their names.

    Raises NoPositionError when no report of `year` has a usable position (latitude and
    longitude, not 0/0; user decision 2026-11-04): such a station-year is not built. The previous
    year's rows cannot supply one. A header-only file (no reports) still gives an empty silver file.
    """
    if current.height and not has_position(current):
        raise NoPositionError("no valid position in this station-year (ISD missing or 0/0)")
    frames = [current] if previous is None else [previous_year_tail(previous, year), current]
    # "diagonal_relaxed": the two years may have different column sets (ISD adds fields over
    # time); missing columns are filled with nulls instead of raising an error.
    raw = dedupe(pl.concat(frames, how="diagonal_relaxed"))
    df = add_time_columns(parse_reports(raw))
    bad = df.filter(pl.col("time_utc").is_null()).height
    if bad:
        log.warning("dropping %d rows with unparseable DATE", bad)
        df = df.filter(pl.col("time_utc").is_not_null())
    df = apply_qc(df, settings.qc, settings.parse)
    # Sort by time and tell Polars the time column is sorted: the haze rule's rolling window
    # (in classify.py) requires it.
    df = df.sort(_SORT, nulls_last=True).with_columns(pl.col("time_utc").set_sorted())
    # Are this station-year's SYNOPs copies of its METARs (US)? Then their present weather is
    # not used (process/derived.py).
    derived = synop_is_derived(df, settings.classify)
    df = classify_reports(
        df,
        settings.classify,
        settings.qc,
        nil_guard=nil_guard,
        metar_guard=metar_guard,
        synop_derived=derived,
    )
    df = apply_haze_and_rh(df, settings.classify, settings.qc)
    df = add_source_group(df, settings.sources)  # SYNOP / METAR / other; summaries excluded
    # Each report lives in the file of its own UTC year (method 2.0.0); this drops the previous
    # year's look-back rows, which belong to the previous file.
    df = df.filter(pl.col("time_utc").dt.year() == year).sort(_SORT, nulls_last=True)
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
    nil_guard: bool | None = None,
    metar_guard: bool | None = None,
) -> None:
    """Validate one silver station-year against the contract, then write it with provenance.

    The metadata stored in the file is exactly what `pipeline._silver_is_current` compares on
    the next run to decide whether this file must be rebuilt.
    """
    validate_silver(df)  # raises before writing anything if the data break the contract
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
            "nil_guard": {True: "1", False: "0", None: ""}[nil_guard],  # station guard (rule V3)
            "metar_guard": {True: "1", False: "0", None: ""}[metar_guard],  # METAR inference guard
        },
    )
