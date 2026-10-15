"""Data contracts (spec §7.2, §13). Column checks via Pandera; cross-row checks in plain Polars."""

from __future__ import annotations

import pandera.polars as pa
import polars as pl

from dcdr.silver.classify import FINAL_CLASSES

SILVER_SCHEMA = pa.DataFrameSchema(
    {
        "station_id": pa.Column(pl.String, nullable=False),
        "time_utc": pa.Column(pl.Datetime("us"), nullable=False),
        "lmst_date": pa.Column(pl.Date, nullable=False),
        "lmst_hour": pa.Column(pl.Float64, pa.Check.in_range(0, 24, include_max=False)),
        "dust_class_final": pa.Column(pl.String, pa.Check.isin(FINAL_CLASSES)),
        "is_synoptic_selected": pa.Column(pl.Boolean),
        "has_pw": pa.Column(pl.Boolean),
        "wsd": pa.Column(pl.Float64, nullable=True),
        "vis_capped": pa.Column(pl.Float64, nullable=True),
        "row_key": pa.Column(pl.String, nullable=False, unique=True),
    },
    strict=False,
)


class SilverContractError(ValueError):
    """Silver data violate a cross-row rule."""


def validate_silver(df: pl.DataFrame) -> pl.DataFrame:
    SILVER_SCHEMA.validate(df)
    dup = (
        df.filter(pl.col("is_synoptic_selected"))
        .group_by(["station_id", "slot_time"])
        .len()
        .filter(pl.col("len") > 1)
    )
    if dup.height:
        raise SilverContractError(f"{dup.height} synoptic slots have more than one selected report")
    return df
