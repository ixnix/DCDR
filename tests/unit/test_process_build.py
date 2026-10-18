import random
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from dcdr.config import Settings
from dcdr.process import METHOD_VERSION
from dcdr.process.build import build_station_year, write_silver
from dcdr.schemas import SilverContractError, validate_silver
from dcdr.state import read_parquet_metadata
from tests.fixtures.isd import bronze_frame, isd_row


def _day(date: str, **fields: str) -> list[dict]:
    return [isd_row(f"{date}T{h:02d}:00:00", **fields) for h in range(0, 24, 3)]


def test_haze_rule_reaches_back_across_new_year(settings: Settings) -> None:
    prev = bronze_frame([isd_row("2023-12-31T22:00:00", MW1="06,1")])
    cur = bronze_frame([isd_row("2024-01-01T00:00:00", MW1="05,1")])
    out = build_station_year(cur, prev, 2024, settings)
    assert out.height == 1  # look-back rows are not kept
    assert out["dust_class_final"].item() == "dust_haze"
    assert build_station_year(cur, None, 2024, settings)["dust_class_final"].item() == "haze"


def test_exact_duplicates_collapse_with_count(settings: Settings) -> None:
    row = isd_row("2024-01-01T00:00:00", MW1="06,1")
    out = build_station_year(bronze_frame([row, row]), None, 2024, settings)
    assert out.height == 1 and out["dup_count"].item() == 2


def test_output_independent_of_input_row_order(settings: Settings) -> None:
    rows = _day("2024-01-01", MW1="02,1") + [
        isd_row("2024-01-01T03:10:00", REPORT_TYPE="FM-15", MW1="06,1")
    ]
    base = build_station_year(bronze_frame(rows), None, 2024, settings)
    shuffled = rows[:]
    random.Random(1).shuffle(shuffled)
    assert_frame_equal(build_station_year(bronze_frame(shuffled), None, 2024, settings), base)


def test_bad_dates_are_dropped_others_kept(settings: Settings) -> None:
    rows = [isd_row("2024-01-01T00:00:00"), isd_row("not-a-date")]
    assert build_station_year(bronze_frame(rows), None, 2024, settings).height == 1


def test_header_only_bronze_builds_empty_silver(settings: Settings, tmp_path: Path) -> None:
    empty = bronze_frame([isd_row("2024-01-01T00:00:00")]).clear()
    out = build_station_year(empty, None, 2024, settings)
    assert out.height == 0
    write_silver(
        out, tmp_path / "s.parquet", run_id="r", config_hash="h", source_md5="m", prev_source_md5=""
    )
    assert pl.read_parquet(tmp_path / "s.parquet").height == 0


def test_write_silver_records_metadata(settings: Settings, tmp_path: Path) -> None:
    out = build_station_year(bronze_frame(_day("2024-01-01")), None, 2024, settings)
    path = tmp_path / "silver.parquet"
    write_silver(out, path, run_id="r1", config_hash="abc", source_md5="m1", prev_source_md5="m0")
    meta = read_parquet_metadata(path)
    assert meta == {
        "dcdr_layer": "silver",
        "method_version": METHOD_VERSION,
        "config_hash": "abc",
        "run_id": "r1",
        "source_md5": "m1",
        "prev_source_md5": "m0",
        "next_year_present": "0",
    }


def test_contract_rejects_two_selected_reports_in_one_slot(settings: Settings) -> None:
    rows = _day("2024-01-01") + [
        isd_row("2024-01-01T03:10:00", REPORT_TYPE="FM-15")
    ]  # 2 reports in slot 03
    out = build_station_year(bronze_frame(rows), None, 2024, settings)
    validate_silver(out)  # the real output satisfies the contract
    broken = out.with_columns(is_synoptic_selected=pl.lit(True))
    with pytest.raises(SilverContractError):
        validate_silver(broken)


def test_year_boundary_slot_is_selected_once_across_files(settings: Settings) -> None:
    """A 23:56 Dec-31 report is a candidate for the Jan-1 00:00 slot: it must live in ONE file."""
    from datetime import datetime

    prev_rows = [
        isd_row("2023-12-31T21:00:00", REPORT_TYPE="FM-15"),
        isd_row("2023-12-31T23:56:00", REPORT_TYPE="FM-15"),
    ]
    cur_rows = [isd_row("2024-01-01T00:00:00", REPORT_TYPE="FM-15")]
    s23 = build_station_year(bronze_frame(prev_rows), None, 2023, settings, next_year_exists=True)
    s24 = build_station_year(bronze_frame(cur_rows), bronze_frame(prev_rows), 2024, settings)
    both = pl.concat([s23, s24], how="diagonal_relaxed")
    assert both["row_key"].n_unique() == both.height  # every report stored exactly once
    jan1 = both.filter(
        pl.col("is_synoptic_selected") & (pl.col("slot_time") == datetime(2024, 1, 1))
    )
    assert jan1.height == 1
    assert jan1["time_utc"].item() == datetime(2024, 1, 1)  # closest report wins the slot


def test_last_year_keeps_dec31_candidates_when_next_year_absent(settings: Settings) -> None:
    """Review I2: a 23:56 Dec-31 row must not vanish when the next year's file will not exist."""
    rows = [
        isd_row("2024-12-31T21:00:00", REPORT_TYPE="FM-15"),
        isd_row("2024-12-31T23:56:00", REPORT_TYPE="FM-15"),
    ]
    alone = build_station_year(bronze_frame(rows), None, 2024, settings, next_year_exists=False)
    assert alone.height == 2
    handed_on = build_station_year(bronze_frame(rows), None, 2024, settings, next_year_exists=True)
    assert handed_on.height == 1  # the 23:56 row belongs to the 2025 file


def test_longitude_sentinel_is_treated_as_missing(settings: Settings) -> None:
    """Review I5: ISD longitude +999999 (999.999) must not shift local time by days."""
    from datetime import date

    rows = [
        isd_row("2024-03-01T00:00:00", LONGITUDE="0.0"),
        isd_row("2024-03-01T03:00:00", LONGITUDE="999.999"),
    ]
    out = build_station_year(bronze_frame(rows), None, 2024, settings)
    assert out["lon"].to_list() == [0.0, None]
    assert out["lmst_date"].to_list() == [date(2024, 3, 1), date(2024, 3, 1)]


def test_no_valid_longitude_fails_clearly(settings: Settings) -> None:
    from dcdr.process.parse import MissingLongitudeError

    rows = [isd_row("2024-03-01T00:00:00", LONGITUDE="999.999")]
    with pytest.raises(MissingLongitudeError):
        build_station_year(bronze_frame(rows), None, 2024, settings)
