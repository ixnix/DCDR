"""Compare dusty-day counts between two gold versions, per station (read-only).

A one-off check for the method 2.0.0 pilot rebuild (not part of the pipeline): all-report bins
should find at least the dusty days the old slot sampling found, so every station whose count
drops is listed, to be explained before the result is accepted. Counts are summed over all years
of each version's `yearly` table (the column names are the same in both methods).

    uv run python scripts/compare_gold_versions.py OLD_RUN_ID NEW_RUN_ID

Run IDs are folder names under <data root>/gold/versions/ (e.g. 20261102T163756Z-05f054).
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

COUNTS = ["n_dusty_days", "n_valid_days"]


def _totals(gold_dir: Path) -> pl.DataFrame:
    """Per station: the dusty and valid days summed over all years of `yearly`."""
    yearly = pl.read_parquet(
        gold_dir / "yearly" / "part-000.parquet", columns=["station_id", *COUNTS]
    )
    return yearly.group_by("station_id").agg(pl.col(COUNTS).sum())


def compare(old_dir: Path, new_dir: Path) -> pl.DataFrame:
    """One row per station in either version: old and new counts, `diff` = new - old dusty days,
    and `decreased` (new < old). A station missing from one version counts 0 there."""
    old, new = _totals(old_dir), _totals(new_dir)
    joined = old.join(new, on="station_id", how="full", coalesce=True, suffix="_new")
    joined = joined.rename({c: f"{c}_old" for c in COUNTS}).with_columns(
        pl.col(f"{c}_{v}").fill_null(0) for c in COUNTS for v in ("old", "new")
    )
    return (
        joined.with_columns(
            diff=pl.col("n_dusty_days_new") - pl.col("n_dusty_days_old"),
        )
        .with_columns(decreased=pl.col("diff") < 0)
        .sort("station_id")
    )


def main(argv: list[str]) -> int:
    """Print the comparison and the stations whose dusty-day count dropped."""
    if len(argv) != 2:
        print(__doc__)
        return 2
    from dcdr.config import load_settings

    versions = load_settings().data_root / "gold" / "versions"
    table = compare(versions / argv[0], versions / argv[1])
    with pl.Config(tbl_rows=-1):
        print(table)
        dropped = table.filter(pl.col("decreased"))
        print(f"\n{dropped.height} of {table.height} stations have fewer dusty days:")
        print(dropped)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
