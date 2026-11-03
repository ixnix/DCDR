"""scripts/compare_gold_versions.py: per-station dusty-day counts of two gold versions, for the
method 2.0.0 pilot check (every station whose count drops is listed and explained)."""

from pathlib import Path

import polars as pl

from scripts.compare_gold_versions import compare


def _yearly(root: Path, rows: dict[str, tuple[int, int]]) -> Path:
    """A gold version folder whose `yearly` table has one 2020 row per station:
    {station: (n_dusty_days, n_valid_days)}."""
    out = root / "yearly"
    out.mkdir(parents=True)
    pl.DataFrame(
        {
            "station_id": list(rows),
            "year": [2020] * len(rows),
            "n_dusty_days": [d for d, _ in rows.values()],
            "n_valid_days": [v for _, v in rows.values()],
        }
    ).write_parquet(out / "part-000.parquet")
    return root


def test_compare_reports_decreases(tmp_path: Path) -> None:
    old = _yearly(tmp_path / "old", {"A": (10, 300), "B": (5, 200)})
    new = _yearly(tmp_path / "new", {"A": (12, 310), "B": (4, 200)})
    got = compare(old, new).sort("station_id")
    assert got["station_id"].to_list() == ["A", "B"]
    assert got["diff"].to_list() == [2, -1]
    assert got["decreased"].to_list() == [False, True]
    assert got["n_valid_days_new"].to_list() == [310, 200]
