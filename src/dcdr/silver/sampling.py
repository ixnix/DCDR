"""Synoptic-slot candidates, deterministic ranking and selection (spec §7.2.7)."""

from __future__ import annotations

import polars as pl

from dcdr.config import SamplingCfg

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
    prio = {t: i for i, t in enumerate(cfg.priority)}
    df = df.with_columns(slot_time=(pl.col("time_utc") + pl.duration(minutes=90)).dt.truncate("3h"))
    df = df.with_columns(
        slot_delta_min=(pl.col("time_utc") - pl.col("slot_time")).dt.total_seconds().abs() / 60,
        is_excluded_type=pl.col("report_type").is_in(cfg.exclude_types).fill_null(False),
        priority_rank=pl.col("report_type")
        .replace_strict(prio, default=len(prio), return_dtype=pl.Int32)
        .fill_null(len(prio)),
    )
    df = df.with_columns(
        is_candidate=(pl.col("slot_delta_min") <= cfg.window_min) & ~pl.col("is_excluded_type")
    )
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
    ).drop("priority_rank")
