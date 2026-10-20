"""Data contracts (spec §7.2, §13). Column checks via Pandera; cross-row checks in plain Polars.

What a "data contract" is
-------------------------
A list of promises the silver layer makes to everything downstream (gold, notebooks, users):
these columns exist, have these types, never contain nulls where that would be meaningless, and
take only allowed values. `validate_silver` checks the promises on every station-year BEFORE the
file is written, so a bug in processing fails loudly instead of quietly producing wrong gold.

Two kinds of checks
-------------------
* Per column (Pandera): type, nullability, value range or allowed set, uniqueness.
* Across rows (plain Polars): at most one primary report, and one report per source, per slot. Pandera's column
  checks can't express "per group" rules like this as clearly, so it is written out by hand.
"""

from __future__ import annotations

import pandera.polars as pa
import polars as pl

from dcdr.process.classify import FINAL_CLASSES

# `strict=False`: silver has many more columns; only these are guaranteed by the contract.
SILVER_SCHEMA = pa.DataFrameSchema(
    {
        "station_id": pa.Column(pl.String, nullable=False),
        "time_utc": pa.Column(pl.Datetime("us"), nullable=False),
        "lmst_date": pa.Column(pl.Date, nullable=False),  # local mean solar date
        "lmst_hour": pa.Column(pl.Float64, pa.Check.in_range(0, 24, include_max=False)),
        "dust_class_final": pa.Column(pl.String, pa.Check.isin(FINAL_CLASSES)),
        "is_synoptic_selected": pa.Column(pl.Boolean),  # the slot's primary report
        "is_source_selected": pa.Column(pl.Boolean),  # the slot's report for its source
        "slot_source": pa.Column(pl.String, pa.Check.isin(["synop", "metar"]), nullable=True),
        "has_pw": pa.Column(pl.Boolean),  # weather observed: a code reported, or iX "nothing"
        "pw_status": pa.Column(
            pl.String,
            pa.Check.isin(
                ["reported", "nil_significant", "nil_inferred", "nil_metar", "not_observed"]
            ),
        ),
        "wsd": pa.Column(pl.Float64, nullable=True),  # wind speed, m/s (null = missing)
        "vis_capped": pa.Column(pl.Float64, nullable=True),  # visibility, m, capped
        "row_key": pa.Column(pl.String, nullable=False, unique=True),  # one row per report
    },
    strict=False,
)


class SilverContractError(ValueError):
    """Silver data violate a cross-row rule."""


def validate_silver(df: pl.DataFrame) -> pl.DataFrame:
    """Check one station-year against the silver contract; raise on any violation.

    Returns the frame unchanged, so it can be used inline: `write(validate_silver(df))`.
    Raises pandera's SchemaError for column problems, SilverContractError for the slot rule.
    """
    SILVER_SCHEMA.validate(df)
    # The synoptic sampling picks at most one primary report per 3-hourly slot, and at most one
    # report per source (SYNOP, METAR) per slot.
    for col, keys, what in (
        ("is_synoptic_selected", ["station_id", "slot_time"], "primary report"),
        ("is_source_selected", ["station_id", "slot_time", "slot_source"], "report per source"),
    ):
        dup = df.filter(pl.col(col)).group_by(keys).len().filter(pl.col("len") > 1)
        if dup.height:
            raise SilverContractError(f"{dup.height} synoptic slots have more than one {what}")
    return df
