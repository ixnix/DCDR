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
* Across rows (plain Polars): a source group exactly on the reports that are not summaries. Pandera's column
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
        # SYNOP / METAR / other (process/sources.py); null exactly for daily/monthly summaries
        "source_group": pa.Column(
            pl.String, pa.Check.isin(["synop", "metar", "other"]), nullable=True
        ),
        "is_excluded_type": pa.Column(pl.Boolean, nullable=False),
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
    Raises pandera's SchemaError for column problems, SilverContractError for the cross-row rule.
    """
    SILVER_SCHEMA.validate(df)
    # Summaries (SOD, SOM) have no source group; every other report has one.
    wrong = df.filter(pl.col("source_group").is_null() != pl.col("is_excluded_type")).height
    if wrong:
        raise SilverContractError(f"{wrong} reports with a source group that contradicts its type")
    return df
