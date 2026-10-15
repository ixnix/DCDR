import random
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from dcdr.config import Settings
from dcdr.schemas import SilverContractError, validate_silver
from dcdr.silver import METHOD_VERSION
from dcdr.silver.build import build_station_year, write_silver
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
