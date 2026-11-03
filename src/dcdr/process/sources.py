"""Source groups (method 2.0.0, user decisions 2026-11-02): which kind of report each row is.

Every report gets `source_group`: "synop" or "metar" if its type is listed in config
`sources.groups`, "other" for every other type (also a missing or unknown type). Daily and monthly
summaries (`sources.exclude_types`: SOD, SOM) are not observations at a time: they get
`is_excluded_type` = true and no group. Gold uses the groups for the per-source dust columns.
"""

from __future__ import annotations

import polars as pl

from dcdr.config import SourcesCfg


def add_source_group(df: pl.DataFrame, cfg: SourcesCfg) -> pl.DataFrame:
    """Add `is_excluded_type` (never null) and `source_group` (null exactly when excluded)."""
    group_of = {t: g for g, types in cfg.groups.items() for t in types}
    rt = pl.col("report_type")
    excluded = rt.is_in(cfg.exclude_types).fill_null(False)
    return df.with_columns(
        is_excluded_type=excluded,
        source_group=pl.when(excluded)
        .then(None)
        .otherwise(
            rt.replace_strict(group_of, default="other", return_dtype=pl.String).fill_null("other")
        ),
    )
