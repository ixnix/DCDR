from pathlib import Path

from dcdr.config import load_settings
from dcdr.pipeline import parse_years, read_station_list, start_run


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
