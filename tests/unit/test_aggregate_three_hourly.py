"""Gold `three_hourly` table (aggregate.py + sql/three_hourly.sql): one row per station per 3-hour
UTC bin (00 = 00:00-02:59, 03 = 03:00-05:59, ...).

Builds small silver station-years from fake ISD rows, runs the real gold build into a temporary
data root, and checks the bins: which report gives the state values (the first regular SYNOP or
METAR), extremes and dust over all reports, counts, and local time.
"""

from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from dcdr.aggregate import build_gold
from dcdr.config import Settings, silver_config_hash
from dcdr.process.build import build_station_year, write_silver
from tests.fixtures.isd import bronze_frame, isd_row

LON0 = {"LONGITUDE": "0.0"}  # LMST == UTC keeps expectations readable


def _silver(
    settings: Settings,
    rows: list[dict],
    name: str = "S",
    year: int = 2024,
    previous: list[dict] | None = None,
    next_year_exists: bool = False,
    nil_guard: bool | None = None,
) -> Path:
    prev = bronze_frame(previous) if previous else None
    df = build_station_year(
        bronze_frame(rows),
        prev,
        year,
        settings,
        next_year_exists=next_year_exists,
        nil_guard=nil_guard,
    )
    path = settings.data_root / "silver" / f"year={year}" / f"{name}.parquet"
    cfg_hash = silver_config_hash(settings)
    write_silver(df, path, run_id="r", config_hash=cfg_hash, source_md5="m", prev_source_md5="")
    return path


def _bins(settings: Settings, paths: list[Path]) -> pl.DataFrame:
    out = build_gold(settings, paths, "20260101T000000Z-aaaaaa")
    return pl.read_parquet(out / "three_hourly" / "part-000.parquet").sort("bin_time")


def _row(t: str, **fields: str) -> dict:
    """A report on 2024-01-01 at time `t` (HH:MM) at longitude 0."""
    return isd_row(f"2024-01-01T{t}:00", **LON0, **fields)


def _one(settings: Settings, rows: list[dict], **kw: bool | None) -> dict:
    b = _bins(settings, [_silver(settings, rows, **kw)])  # type: ignore[arg-type]
    assert len(b) == 1
    return b.row(0, named=True)


# --- bins, state report, extremes, counts ----------------------------------------------------


def test_bins_follow_utc_clock_hours(settings: Settings) -> None:
    b = _bins(settings, [_silver(settings, [_row("00:00"), _row("02:59"), _row("03:00")])])
    assert b["bin_time"].to_list() == [datetime(2024, 1, 1, 0), datetime(2024, 1, 1, 3)]
    assert b["n_reports"].to_list() == [2, 1]


def test_state_values_come_from_first_regular_report(settings: Settings) -> None:
    r = _one(
        settings,
        [
            _row("00:10", REPORT_TYPE="FM-16", WND="090,1,N,0200,1"),  # SPECI first in time
            _row("00:51", REPORT_TYPE="FM-15", WND="090,1,N,0100,1", TMP="+0250,1"),
            _row("01:00", REPORT_TYPE="FM-12", WND="090,1,N,0150,1"),
        ],
    )
    assert (r["state_report_type"], r["state_time_utc"]) == ("FM-15", datetime(2024, 1, 1, 0, 51))
    assert (r["wsd"], r["t"]) == (10.0, 25.0)


def test_synop_wins_a_same_minute_tie(settings: Settings) -> None:
    r = _one(
        settings,
        [
            _row("00:00", REPORT_TYPE="FM-15", WND="090,1,N,0100,1"),
            _row("00:00", REPORT_TYPE="FM-12", WND="090,1,N,0150,1"),
        ],
    )
    assert (r["state_report_type"], r["wsd"]) == ("FM-12", 15.0)


def test_state_values_are_not_filled_from_other_reports(settings: Settings) -> None:
    r = _one(
        settings,
        [
            _row("00:00", REPORT_TYPE="FM-12", SLP="99999,9"),
            _row("00:51", REPORT_TYPE="FM-15", SLP="10100,1"),
        ],
    )
    assert r["slp"] is None


def test_qc_failed_state_value_is_null(settings: Settings) -> None:
    r = _one(settings, [_row("00:00", TMP="+0950,1")])  # 95 °C: out of range
    assert r["t"] is None and r["rh"] is None


def test_speci_only_bin_uses_first_speci(settings: Settings) -> None:
    r = _one(
        settings,
        [
            _row("04:20", REPORT_TYPE="FM-16", WND="090,1,N,0080,1"),
            _row("05:10", REPORT_TYPE="FM-16", WND="090,1,N,0120,1"),
        ],
    )
    assert (r["state_report_type"], r["wsd"]) == ("FM-16", 8.0)


def test_extremes_use_all_reports(settings: Settings) -> None:
    r = _one(
        settings,
        [
            _row("00:00", VIS="010000,1,N,1", OC1="0150,1"),
            _row("01:30", REPORT_TYPE="FM-16", VIS="002000,1,N,1", OC1="0220,1"),
        ],
    )
    assert (r["vis"], r["vis_min"], r["gust_max"]) == (10000.0, 2000.0, 22.0)


def test_counts_by_type_and_summaries_excluded(settings: Settings) -> None:
    r = _one(
        settings,
        [
            _row("00:00", REPORT_TYPE="FM-12"),
            _row("00:51", REPORT_TYPE="FM-15"),
            _row("01:10", REPORT_TYPE="FM-16"),
            _row("02:00", REPORT_TYPE="FM-13"),
            _row("23:59", REPORT_TYPE="SOD"),  # alone in bin 21: no row
        ],
    )
    got = (r["n_reports"], r["n_synop"], r["n_metar"], r["n_speci"], r["n_other"])
    assert got == (4, 1, 1, 1, 1)


def test_bin_spans_two_silver_files(settings: Settings) -> None:
    # The 23:40 METAR competes for the 1-January 00:00 slot, so it is stored in the 2024 file;
    # its bin (31 December 21:00) still gets one row with both reports.
    y2023 = [
        isd_row("2023-12-31T21:00:00", **LON0),
        isd_row("2023-12-31T23:40:00", **LON0, REPORT_TYPE="FM-15"),
    ]
    y2024 = [isd_row("2024-01-01T00:00:00", **LON0)]
    paths = [
        _silver(settings, y2023, year=2023, next_year_exists=True),
        _silver(settings, y2024, year=2024, previous=y2023),
    ]
    b = _bins(settings, paths)
    assert b.filter(pl.col("bin_time") == datetime(2023, 12, 31, 21))["n_reports"].to_list() == [2]
    assert b["bin_time"].is_unique().all()


# --- dust and local time -------------------------------------------------------------------


def test_dust_from_any_report_including_speci(settings: Settings) -> None:
    r = _one(
        settings,
        [_row("00:00", MW1="02,1"), _row("01:20", REPORT_TYPE="FM-16", MW1="07,1")],
    )
    assert (r["observed"], r["dusty"], r["c_blowing"]) == (True, True, True)
    assert r["first_dust_time_utc"] == datetime(2024, 1, 1, 1, 20)
    assert r["state_report_type"] == "FM-12"  # dust doesn't change the state report


def test_unobserved_bin_has_null_dust(settings: Settings) -> None:
    # A SYNOP without a weather code or iX, and no rule V3 for the station: not observed.
    r = _one(settings, [_row("00:00")])
    got = (r["observed"], r["dusty"], r["c_suspended"], r["n_reports_pw"])
    assert got == (False, None, None, 0)


def test_rh_screened_report_is_not_dusty(settings: Settings) -> None:
    humid = {"TMP": "+0200,1", "DEW": "+0195,1"}  # RH ~ 97 %
    rows = [_row("00:00", MW1="06,1", **humid), _row("03:00", MW1="05,1", **humid)]
    b = _bins(settings, [_silver(settings, rows)])
    assert b["dusty"].to_list() == [True, False]  # haze after dust in humid air is screened
    assert b["c_screened"].to_list() == [False, True]


def test_inferred_nil_counts_as_observed_not_explicit(settings: Settings) -> None:
    r = _one(settings, [_row("00:00", REM="SYN004BUFR")], nil_guard=True)  # rule V3
    assert (r["observed"], r["observed_explicit"], r["dusty"]) == (True, False, False)


KELP_RMK = "MET100KELP 010051Z 12012G27KT 10SM CLR 30/02 A3027 RMK AO2 SLP042 BLDU DSNT SW"


def test_vicinity_and_remarks_dust_stay_separate(settings: Settings) -> None:
    r = _one(
        settings,
        [
            _row("00:00", MW1="02,1", MV1="08,1"),  # dust in the vicinity (SYNOP MV code)
            _row("00:51", REPORT_TYPE="FM-15", REM=KELP_RMK),  # dust named only in remarks
        ],
    )
    assert (r["dusty"], r["vicinity_dust"], r["remarks_dust"]) == (False, True, True)


def test_lmst_of_bin_start(settings: Settings) -> None:
    # Las Vegas: LMST = UTC - 115.16343/15 h = UTC - 7 h 40.65 min
    rows = [isd_row("2024-01-01T06:00:00", LONGITUDE="-115.16343")]
    r = _one(settings, rows)
    assert r["lmst_date"] == date(2023, 12, 31)
    assert r["lmst_hour"] == pytest.approx(22.3226, abs=0.001)


def test_type_groups_follow_config_and_saosp_is_a_special(settings: Settings) -> None:
    # AUST is a SYNOP source and SAO a METAR source (config sampling.sources); SAOSP is the
    # pre-1996 US airways special (ISD format document p. 6), counted with the SPECIs.
    r = _one(
        settings,
        [
            _row("00:00", REPORT_TYPE="SAO"),
            _row("00:30", REPORT_TYPE="SAOSP"),
            _row("01:00", REPORT_TYPE="AUST"),
        ],
    )
    got = (r["n_synop"], r["n_metar"], r["n_speci"], r["n_other"])
    assert got == (1, 1, 1, 0)


def test_lmst_uses_the_state_reports_longitude(settings: Settings) -> None:
    # A station that moved within the bin: local time follows the state report (deterministic).
    rows = [
        isd_row("2024-01-01T06:10:00", LONGITUDE="0.0", REPORT_TYPE="FM-16"),
        isd_row("2024-01-01T07:00:00", LONGITUDE="-115.16343"),  # the state report
    ]
    r = _one(settings, rows)
    assert r["lmst_date"] == date(2023, 12, 31)
    assert r["lmst_hour"] == pytest.approx(22.3226, abs=0.001)
