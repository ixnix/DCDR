"""Stage runner helpers (pipeline.py): --years parsing, station lists, run start and run log."""

from pathlib import Path

import polars as pl

from dcdr import __version__
from dcdr.config import load_settings
from dcdr.pipeline import finish_run, parse_years, read_station_list, start_run


def test_parse_years_forms() -> None:
    assert parse_years("2024") == [2024]
    assert parse_years("2022-2024") == [2022, 2023, 2024]
    assert parse_years("2020,2022") == [2020, 2022]


def test_station_list_reads_ids(tmp_path: Path) -> None:
    p = tmp_path / "s.csv"
    p.write_text("station_id,name\n72386023169,LAS VEGAS\n 40582099999 ,KUWAIT\n")
    assert read_station_list(p) == ["72386023169", "40582099999"]


def test_start_run_creates_missing_data_root(tmp_path: Path) -> None:
    root = tmp_path / "new" / "nested" / "data"
    s = load_settings(overrides={"paths": {"data_root": str(root)}})
    ctx = start_run(s, "test")
    assert (root / "_state" / "runs" / ctx.run_id / "config.resolved.yaml").exists()


def test_run_record_names_package_version_and_commit(tmp_path: Path) -> None:
    s = load_settings(overrides={"paths": {"data_root": str(tmp_path)}})
    ctx = start_run(s, "test")
    finish_run(ctx, "ok", {})
    row = pl.read_parquet(tmp_path / "_state" / "runs.parquet").row(0, named=True)
    assert row["dcdr_version"] == __version__
    assert len(row["git_commit"]) == 40  # tests run from a git checkout
