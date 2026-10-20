"""Synoptic-slot candidates, deterministic ranking and selection (spec §7.2.7; revised 1.5.0).

Why sample at all
-----------------
Stations report at very different rates: a SYNOP station every 3 hours, a busy airport every 30
minutes plus extra "special" reports when the weather changes (often exactly when dust arrives).
Counting dust in ALL reports would make airports look dustier simply because they report more.
So, like duISD v1, dust is judged in 3-hourly synoptic slots (00, 03, ..., 21 UTC), 8 per day, the
same at every station and decade.

Two sources per slot (user decision 2026-10-20, method 1.5.0)
--------------------------------------------------------------
SYNOP and METAR are judged independently: a slot is dusty if EITHER reports dust. Before 1.5.0 a
slot took a single report, SYNOP first, so a SYNOP that observed nothing (e.g. US SYNOPs with iX 1
but no weather group) hid the dust in the METAR next to it. Now each source (config
`sampling.sources`) contributes at most one report per slot, and gold combines them (sql/daily.sql).
At most one per source keeps a busy airport from getting more chances to catch dust than a
SYNOP-only station. SPECI belong to no source: they are event-driven and count only in
`dusty_day_any`.

How a slot is filled (process pipeline step 6 of 6)
--------------------------------------------------
1. Each report is assigned to its nearest slot. It is a CANDIDATE if it is within +-30 minutes of
   the slot time (config `sampling.window_min`), not a daily/monthly summary, and its type belongs
   to a source (`slot_source`: "synop" or "metar").
2. Within each (station, slot, source), candidates are ranked by
     has_pw          reports that observed the weather first
     type order      the order of types in the source's list (e.g. FM-12 before SY-MT)
     slot_delta_min  closer to the slot time wins
     time_utc, source, row_key   final tie-breakers, so the result never depends on row order
   Rank 1 is that source's report for the slot (`source_rank`, `is_source_selected`).
3. Across sources, the source order (SYNOP first) BEFORE has_pw gives the slot's PRIMARY report
   (`selection_rank` 1, `is_synoptic_selected`): the slot's SYNOP whenever it has one, observed or
   not, otherwise its METAR (user decision 2026-10-20). Wind, temperature, humidity, pressure and
   visibility statistics use the primary report only, so they come from one report type per slot
   and are never double counted (a station's series does not switch between SYNOP and METAR when
   the METAR happens to observe the weather). `selection_reason` records why, for auditing.
Every candidate keeps its ranks, so you can always see which reports competed and lost.
"""

from __future__ import annotations

import polars as pl

from dcdr.config import SamplingCfg
from dcdr.process.parse import lmst_shift

# Tie-breakers after the leading keys, in order; all ascending.
_TIEBREAK = ["slot_delta_min", "time_utc", "source", "row_key"]


def _rank(
    df: pl.DataFrame, keys: list[str], group: list[str], name: str, lead: list[str] | None = None
) -> pl.DataFrame:
    """Row number per `group` after sorting by `lead`, has_pw (True first), `keys`, tie-breakers."""
    lead = lead or []
    order = ["station_id", "slot_time", *lead, "has_pw", *keys, *_TIEBREAK]
    desc = [False, False] + [False] * len(lead) + [True] + [False] * (len(keys) + len(_TIEBREAK))
    return (
        df.sort(order, descending=desc, nulls_last=True)
        .with_columns((pl.int_range(pl.len()).over(group) + 1).cast(pl.Int32).alias(name))
        .select("row_key", name)
    )


def select_synoptic(df: pl.DataFrame, cfg: SamplingCfg) -> pl.DataFrame:
    """Mark, per station and 3-hourly slot, one report per source and the primary report."""
    # Report type -> its source, the source's preference order, and the type's order in its list.
    source_of = {t: s for s, types in cfg.sources.items() for t in types}
    source_order = {s: i for i, s in enumerate(cfg.sources)}  # {"synop": 0, "metar": 1}
    type_order = {t: i for types in cfg.sources.values() for i, t in enumerate(types)}
    # Nearest slot: shift by +90 min, then round DOWN to a multiple of 3 h. E.g. 04:20 -> 05:50
    # -> 03:00, and 04:40 -> 06:10 -> 06:00. So the slot is the closest of 00, 03, ..., 21 UTC.
    df = df.with_columns(slot_time=(pl.col("time_utc") + pl.duration(minutes=90)).dt.truncate("3h"))
    # the slot belongs to the LMST day of the SLOT time, not of whichever report fills it
    df = df.with_columns(
        slot_lmst_date=(pl.col("slot_time") + lmst_shift(pl.col("lon_used"))).dt.date()
    )
    rt = pl.col("report_type")
    df = df.with_columns(
        slot_delta_min=(pl.col("time_utc") - pl.col("slot_time")).dt.total_seconds().abs() / 60,
        is_excluded_type=rt.is_in(cfg.exclude_types).fill_null(False),
        slot_source=rt.replace_strict(source_of, default=None, return_dtype=pl.String),
    )
    df = df.with_columns(
        is_candidate=(
            (pl.col("slot_delta_min") <= cfg.window_min)
            & ~pl.col("is_excluded_type")
            & pl.col("slot_source").is_not_null()
        ),
        _source_order=pl.col("slot_source").replace_strict(
            source_order, default=None, return_dtype=pl.Int32
        ),
        _type_order=rt.replace_strict(type_order, default=None, return_dtype=pl.Int32),
    )
    cand = df.filter(pl.col("is_candidate"))
    # Rank within each source, then across sources (the primary report).
    by_source = _rank(
        cand, ["_type_order"], ["station_id", "slot_time", "slot_source"], "source_rank"
    )
    overall = _rank(
        cand, ["_type_order"], ["station_id", "slot_time"], "selection_rank", lead=["_source_order"]
    )
    # Attach the ranks back to all rows; non-candidates get null ranks.
    df = df.join(by_source, on="row_key", how="left").join(overall, on="row_key", how="left")
    return df.with_columns(
        is_source_selected=(pl.col("source_rank") == 1).fill_null(False),
        is_synoptic_selected=(pl.col("selection_rank") == 1).fill_null(False),
        selection_reason=pl.when(pl.col("selection_rank").is_null())
        .then(None)
        .otherwise(
            pl.format(
                "rank={};source={}#{};type={};pw={};dt_min={}",
                pl.col("selection_rank"),
                pl.col("slot_source"),
                pl.col("source_rank"),
                pl.col("report_type"),
                pl.col("has_pw"),
                pl.col("slot_delta_min").round(1),
            )
        ),
    ).drop("_source_order", "_type_order")  # helper columns, not needed in silver
