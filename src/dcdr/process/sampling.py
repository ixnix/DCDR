"""Synoptic-slot candidates, deterministic ranking and selection (spec §7.2.7).

Why sample at all
-----------------
Stations report at very different rates: a SYNOP station every 3 hours, a busy airport every 30
minutes plus extra "special" reports when the weather changes (often exactly when dust arrives).
Counting dust in ALL reports would make airports look dustier simply because they report more.
So, like duISD v1, we pick ONE report per 3-hourly synoptic slot (00, 03, ..., 21 UTC) and base the
main dusty-day statistic on those 8 slots per day: the same sampling at every station and decade.

How a slot is filled (process pipeline step 6 of 6)
--------------------------------------------------
1. Each report is assigned to its nearest slot. It is a CANDIDATE if it is within +-30 minutes of
   the slot time (config `sampling.window_min`) and not a daily/monthly summary.
2. Candidates for the same station and slot are ranked by, in order:
     report type priority (SYNOP FM-12 first, see config `sampling.priority`)
     has_pw         reports that carry a weather code beat those that don't
     slot_delta_min closer to the slot time wins
     time_utc, source, row_key   final tie-breakers, so the result never depends on row order
3. Rank 1 is selected (`is_synoptic_selected`); `selection_reason` records why, for auditing.
Every candidate keeps its rank, so you can always see which reports competed and lost.
"""

from __future__ import annotations

import polars as pl

from dcdr.config import SamplingCfg
from dcdr.process.parse import lmst_shift

# Sort order used for ranking candidates (see the module docstring), and the direction of each
# column: only `has_pw` is descending (True before False); everything else ascending.
_ORDER = [
    "station_id",
    "slot_time",
    "priority_rank",
    "has_pw",
    "slot_delta_min",
    "time_utc",
    "source",
    "row_key",
]
_DESC = [False, False, False, True, False, False, False, False]


def select_synoptic(df: pl.DataFrame, cfg: SamplingCfg) -> pl.DataFrame:
    """Mark the one selected report per station and 3-hourly slot; keep every row."""
    prio = {t: i for i, t in enumerate(cfg.priority)}  # {"FM-12": 0, "SY-MT": 1, ...}
    # Nearest slot: shift by +90 min, then round DOWN to a multiple of 3 h. E.g. 04:20 -> 05:50
    # -> 03:00, and 04:40 -> 06:10 -> 06:00. So the slot is the closest of 00, 03, ..., 21 UTC.
    df = df.with_columns(slot_time=(pl.col("time_utc") + pl.duration(minutes=90)).dt.truncate("3h"))
    # the slot belongs to the LMST day of the SLOT time, not of whichever report fills it
    df = df.with_columns(
        slot_lmst_date=(pl.col("slot_time") + lmst_shift(pl.col("lon_used"))).dt.date()
    )
    df = df.with_columns(
        slot_delta_min=(pl.col("time_utc") - pl.col("slot_time")).dt.total_seconds().abs() / 60,
        is_excluded_type=pl.col("report_type").is_in(cfg.exclude_types).fill_null(False),
        # Report types not in the priority list get the lowest priority (after all listed ones).
        priority_rank=pl.col("report_type")
        .replace_strict(prio, default=len(prio), return_dtype=pl.Int32)
        .fill_null(len(prio)),
    )
    df = df.with_columns(
        is_candidate=(pl.col("slot_delta_min") <= cfg.window_min) & ~pl.col("is_excluded_type")
    )
    # Rank candidates within each (station, slot): sort by the tie-break order, then number
    # them 1, 2, 3, ... per group (`int_range(...).over(...)` = a row number per group).
    ranked = (
        df.filter(pl.col("is_candidate"))
        .sort(_ORDER, descending=_DESC, nulls_last=True)
        .with_columns(
            selection_rank=(pl.int_range(pl.len()).over(["station_id", "slot_time"]) + 1).cast(
                pl.Int32
            )
        )
        .select("row_key", "selection_rank")
    )
    # Attach the ranks back to all rows; non-candidates get a null rank.
    df = df.join(ranked, on="row_key", how="left")
    return df.with_columns(
        is_synoptic_selected=(pl.col("selection_rank") == 1).fill_null(False),
        selection_reason=pl.when(pl.col("selection_rank").is_null())
        .then(None)
        .otherwise(
            pl.format(
                "rank={};type={};pw={};dt_min={}",
                pl.col("selection_rank"),
                pl.col("report_type"),
                pl.col("has_pw"),
                pl.col("slot_delta_min").round(1),
            )
        ),
    ).drop("priority_rank")  # helper column, not needed in silver
