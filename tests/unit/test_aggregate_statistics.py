"""Gold statistics tables (aggregate.py + sql/monthly.sql, yearly.sql, decadal.sql): per-station dust
day counts, coverage and slot counts per local month, year and decade.

Builds small silver station-years from fake ISD rows, runs the real gold build into a temporary
data root, and checks the counts: valid days and frequency, presence on any day, coverage of the
sub-periods, calendar lengths, and the decadal periods.
"""

from pathlib import Path

import polars as pl
import pytest

from dcdr.aggregate import build_gold
from dcdr.config import Settings, load_settings, silver_config_hash
from dcdr.process.build import build_station_year, write_silver
from tests.fixtures.isd import bronze_frame, isd_row

LON0 = {"LONGITUDE": "0.0"}  # LMST == UTC keeps expectations readable
STATISTICS = ("monthly", "yearly", "decadal")


def _silver(settings: Settings, rows: list[dict], year: int) -> Path:
    df = build_station_year(bronze_frame(rows), None, year, settings)
    path = settings.data_root / "silver" / f"year={year}" / "S.parquet"
    cfg_hash = silver_config_hash(settings)
    write_silver(df, path, run_id="r", config_hash=cfg_hash, source_md5="m", prev_source_md5="")
    return path


def _tables(settings: Settings, paths: list[Path]) -> dict[str, pl.DataFrame]:
    """Build gold and read whichever statistics tables it wrote."""
    out = build_gold(settings, paths, "20260101T000000Z-aaaaaa")
    return {
        name: pl.read_parquet(out / name / "part-000.parquet")
        for name in STATISTICS
        if (out / name).exists()
    }


def _day(
    day: str, mw: dict[int, str] | None = None, hours: range = range(0, 24, 3), **fields: str
) -> list[dict]:
    """One SYNOP per hour in `hours` on `day` at longitude 0; present weather 02 unless `mw` says
    otherwise for that hour."""
    mw = mw or {}
    return [
        isd_row(f"{day}T{h:02d}:00:00", **LON0, **{"MW1": mw.get(h, "02,1"), **fields})
        for h in hours
    ]


# --- monthly ---------------------------------------------------------------------------------


def test_monthly_valid_presence_and_slot_counts(settings: Settings) -> None:
    rows = (
        _day("2024-01-01", {3: "06,1"})  # valid, dusty (suspended)
        + _day("2024-01-02")  # valid, clear
        + _day("2024-01-03", {0: "07,1"}, hours=range(0, 6, 3))  # 2 slots: dusty, not valid
    )
    m = _tables(settings, [_silver(settings, rows, 2024)])["monthly"].row(0, named=True)
    assert (m["year"], m["month"], m["n_calendar_days"]) == (2024, 1, 31)
    assert (m["n_days"], m["n_obs_days"], m["n_valid_days"]) == (3, 3, 2)
    assert (m["n_dusty_days"], m["n_dusty_days_obs"], m["dusty_freq"]) == (1, 2, 0.5)
    assert (m["n_valid_days_synop"], m["n_valid_days_metar"]) == (2, 0)
    assert (m["n_days_suspended"], m["n_days_blowing"], m["n_days_raised"]) == (1, 0, 0)
    assert (m["n_slots_dusty"], m["n_slots_dusty_valid"], m["n_slots_pw_valid"]) == (2, 1, 16)


def test_monthly_without_valid_days_has_null_rate(settings: Settings) -> None:
    rows = _day("2024-03-05", hours=range(0, 6, 3))
    m = _tables(settings, [_silver(settings, rows, 2024)])["monthly"]
    assert (m["n_valid_days"].item(), m["dusty_freq"].item()) == (0, None)


def test_monthly_extinction(settings: Settings) -> None:
    rows = _day("2024-01-01", VIS="010000,1,N,1") + _day(
        "2024-01-02", {3: "07,1"}, VIS="002000,1,N,1"
    )
    m = _tables(settings, [_silver(settings, rows, 2024)])["monthly"].row(0, named=True)
    assert m["ext_mean"] == pytest.approx(0.3) and m["ext_mean_dusty"] == pytest.approx(0.5)
    assert (m["n_days_vis"], m["n_days_vis_dusty"], m["vis_min"]) == (2, 1, 2000.0)


def test_monthly_follows_the_local_month(settings: Settings) -> None:
    # Las Vegas: the 06 UTC slot of 2024-02-01 is 22:19 on 2024-01-31 local
    rows = [isd_row("2024-02-01T06:00:00", LONGITUDE="-115.16343", MW1="02,1")]
    m = _tables(settings, [_silver(settings, rows, 2024)])["monthly"]
    assert m.select("year", "month").rows() == [(2024, 1)]


def test_february_leap_year_calendar_days(settings: Settings) -> None:
    m = _tables(settings, [_silver(settings, _day("2024-02-10"), 2024)])["monthly"]
    assert m["n_calendar_days"].item() == 29


# --- yearly ----------------------------------------------------------------------------------


def _days(month: int, n: int, **kw: str) -> list[dict]:
    """Days 1..n of `month` in 2024, each fully observed and clear."""
    return [r for d in range(1, n + 1) for r in _day(f"2024-{month:02d}-{d:02d}", **kw)]


def test_yearly_sums_months_and_counts_covered_months(settings: Settings) -> None:
    rows = _days(1, 10) + _days(2, 2)
    y = _tables(settings, [_silver(settings, rows, 2024)])["yearly"].row(0, named=True)
    assert (y["year"], y["n_calendar_days"], y["n_valid_days"]) == (2024, 366, 12)
    assert (y["n_months_obs"], y["n_months_valid"], y["n_months_covered"]) == (2, 2, 1)
    assert (y["n_valid_days_vis5"], y["n_obs_days_vis5"]) == (12, 12)


def test_yearly_covered_months_threshold_from_config(settings: Settings) -> None:
    s = load_settings(
        overrides={
            "paths": {"data_root": str(settings.data_root)},
            "products": {"min_valid_days_month": 2},
        }
    )
    rows = _days(1, 10) + _days(2, 2)
    assert _tables(s, [_silver(s, rows, 2024)])["yearly"]["n_months_covered"].item() == 2


def test_visibility_only_in_monthly(settings: Settings) -> None:
    t = _tables(settings, [_silver(settings, _day("2024-01-01", VIS="010000,1,N,1"), 2024)])
    vis = {"n_days_vis", "ext_mean", "n_days_vis_dusty", "ext_mean_dusty", "vis_min"}
    vis.add("n_slots_vis_zero")
    assert vis <= set(t["monthly"].columns)
    assert vis & set(t["yearly"].columns) == set()


# --- decadal ---------------------------------------------------------------------------------


def _period(d: pl.DataFrame, decade: int, period: str) -> dict:
    return d.filter((pl.col("decade") == decade) & (pl.col("period") == period)).row(0, named=True)


def test_decadal_periods_decades_and_calendar(settings: Settings) -> None:
    paths = [
        _silver(settings, _day("1991-01-05", {3: "06,1"}), 1991),
        _silver(settings, _day("2000-12-15", {3: "07,1"}), 2000),
        _silver(
            settings,
            _day("2001-01-10") + _day("2001-01-11", {0: "07,1"}, hours=range(0, 6, 3)),
            2001,
        ),
    ]
    d = _tables(settings, paths)["decadal"]
    # 1991-2000 has data only in January and December: no rows for the other months and seasons
    assert sorted(d.filter(pl.col("decade") == 1991)["period"]) == ["01", "12", "DJF", "annual"]
    djf = _period(d, 1991, "DJF")  # pools December 2000 with January 1991
    got = (djf["n_valid_days"], djf["n_dusty_days"], djf["n_years_valid"], djf["n_dust_years"])
    assert got == (2, 2, 2, 2)
    # full calendar lengths: DJF over 10 years with 3 leap Februaries; 10 Decembers; the decade
    cal = [_period(d, 1991, p)["n_calendar_days"] for p in ("DJF", "12", "annual")]
    assert cal == [903, 310, 3653]
    a01 = _period(d, 2001, "annual")
    assert (a01["n_calendar_days"], a01["n_dust_years"], a01["n_dust_years_obs"]) == (3652, 0, 1)
    assert djf["dusty_days_mean"] == pytest.approx(90.3) and a01["dusty_days_mean"] == 0.0


def test_decadal_means_scale_the_frequency_to_the_period(settings: Settings) -> None:
    rows = (
        _day("2024-03-01", {3: "06,1"})
        + _day("2024-03-02")
        + _day("2024-04-01", hours=range(0, 6, 3))  # not valid: April has no frequency
    )
    d = _tables(settings, [_silver(settings, rows, 2024)])["decadal"]
    mean = dict(d.select("period", "dusty_days_mean").rows())
    assert mean["03"] == pytest.approx(0.5 * 31) and mean["MAM"] == pytest.approx(0.5 * 92)
    assert mean["annual"] == pytest.approx(0.5 * 365.2) and mean["04"] is None
    assert "ext_mean" not in d.columns


# --- all statistics tables -------------------------------------------------------------------


def test_statistics_counts_are_int64(settings: Settings) -> None:
    t = _tables(settings, [_silver(settings, _day("2024-01-01"), 2024)])
    assert set(t) == set(STATISTICS)
    for name, df in t.items():
        bad = [c for c, dt in df.schema.items() if c.startswith("n_") and dt != pl.Int64]
        assert bad == [], name
