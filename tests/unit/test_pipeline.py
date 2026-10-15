from pathlib import Path

import pytest

from dcdr.config import load_settings
from dcdr.pipeline import DataRootUnavailable, parse_years, read_station_list, start_run


def test_parse_years_forms() -> None:
    assert parse_years("2024") == [2024]
    assert parse_years("2022-2024") == [2022, 2023, 2024]
    assert parse_years("2020,2022") == [2020, 2022]


def test_station_list_reads_ids(tmp_path: Path) -> None:
    p = tmp_path / "s.csv"
    p.write_text("station_id,name\n72386023169,LAS VEGAS\n 40582099999 ,KUWAIT\n")
    assert read_station_list(p) == ["72386023169", "40582099999"]


def test_unmounted_data_root_fails_clearly() -> None:
    s = load_settings(overrides={"paths": {"data_root": "/nonexistent-volume-xyz/dcdr"}})
    with pytest.raises(DataRootUnavailable, match="mounted"):
        start_run(s, "test")
    assert not Path("/nonexistent-volume-xyz").exists()
