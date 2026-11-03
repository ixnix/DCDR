"""Gold statistics tables (aggregate.py + sql/monthly.sql, yearly.sql, decadal.sql): per-station dust
day counts, coverage and bin counts per local month, year and decade.

Builds small silver station-years from fake ISD rows, runs the real gold build into a temporary
data root, and checks the counts: valid days and frequency, presence on any day, coverage of the
sub-periods, calendar lengths, and the decadal periods.
"""

from pathlib import Path

import duckdb
import polars as pl
import pytest

from dcdr.aggregate import _count_order_checks, build_gold
from dcdr.config import Settings, load_settings, silver_config_hash
from dcdr.process.build import build_station_year, write_silver
from tests.fixtures.isd import bronze_frame, isd_row

LON0 = {"LONGITUDE": "0.0"}  # LMST == UTC keeps expectations readable
STATISTICS = ("monthly", "yearly", "decadal")


def _silver(settings: Settings, rows: list[dict], year: int, name: str = "S") -> Path:
    df = build_station_year(bronze_frame(rows), None, year, settings)
    path = settings.data_root / "silver" / f"year={year}" / f"{name}.parquet"
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


def test_monthly_valid_presence_and_bin_counts(settings: Settings) -> None:
    rows = (
        _day("2024-01-01", {3: "06,1"})  # valid, dusty (suspended)
        + _day("2024-01-02")  # valid, clear
        + _day("2024-01-03", {0: "07,1"}, hours=range(0, 6, 3))  # 2 bins: dusty, not valid
    )
    m = _tables(settings, [_silver(settings, rows, 2024)])["monthly"].row(0, named=True)
    assert (m["year"], m["month"], m["n_calendar_days"]) == (2024, 1, 31)
    assert (m["n_days"], m["n_obs_days"], m["n_valid_days"]) == (3, 3, 2)
    assert (m["n_dusty_days"], m["n_dusty_days_obs"], m["dusty_freq"]) == (1, 2, 0.5)
    assert (m["n_valid_days_synop"], m["n_valid_days_metar"]) == (2, 0)
    assert (m["n_days_suspended"], m["n_days_blowing"], m["n_days_raised"]) == (1, 0, 0)
    assert (m["n_bins_dusty"], m["n_bins_dusty_valid"], m["n_bins_obs_valid"]) == (2, 1, 16)


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
    # Las Vegas: the 06 UTC bin of 2024-02-01 is 22:19 on 2024-01-31 local
    rows = [isd_row("2024-02-01T06:00:00", LONGITUDE="-115.16343", MW1="02,1")]
    m = _tables(settings, [_silver(settings, rows, 2024)])["monthly"]
    assert m.select("year", "month").rows() == [(2024, 1)]


def _dusty_day(day: str) -> list[dict]:
    """A valid day with dust (suspended) in all 8 bins."""
    return _day(day, {h: "06,1" for h in range(0, 24, 3)})


def test_monthly_lower_bound_and_validity_ratio(settings: Settings) -> None:
    rows = (
        _day("2024-01-01", {3: "06,1"})  # valid, dusty
        + _day("2024-01-02")  # valid, clear
        + _day("2024-01-03", {0: "07,1"}, hours=range(0, 6, 3))  # observed, dusty, not valid
    )
    m = _tables(settings, [_silver(settings, rows, 2024)])["monthly"].row(0, named=True)
    assert m["dusty_freq_obs"] == pytest.approx(2 / 3)
    # valid share of clear observed days 1/1, of dusty observed days 1/2
    assert m["validity_ratio"] == pytest.approx(2.0)


def test_validity_ratio_when_only_dusty_days_are_valid(settings: Settings) -> None:
    # Tidjikja's pattern: a clear day has too few observed bins to be valid, a dusty day has dust
    # coded in every bin, so every valid day is dusty
    rows = [r for d in (1, 2, 3) for r in _day(f"2024-01-0{d}", hours=range(0, 6, 3))]
    rows += _dusty_day("2024-01-04") + _dusty_day("2024-01-05")
    m = _tables(settings, [_silver(settings, rows, 2024)])["monthly"].row(0, named=True)
    assert (m["n_obs_days"], m["n_dusty_days_obs"], m["n_valid_days"]) == (5, 2, 2)
    assert (m["dusty_freq"], m["dusty_freq_obs"], m["validity_ratio"]) == (1.0, 0.4, 0.0)


def test_validity_ratio_null_when_a_share_is_undefined(settings: Settings) -> None:
    rows = (
        _day("2024-01-01")  # January: no dust seen
        + _day("2024-02-01")  # February: dust seen only on a day that is not valid
        + _day("2024-02-02", {0: "07,1"}, hours=range(0, 6, 3))
        + _dusty_day("2024-03-01")  # March: no clear observed day
    )
    m = _tables(settings, [_silver(settings, rows, 2024)])["monthly"].sort("month")
    assert m["dusty_freq_obs"].to_list() == [0.0, 0.5, 1.0]
    assert m["validity_ratio"].to_list() == [None, None, None]


def test_yearly_and_decadal_ratios_from_summed_counts(settings: Settings) -> None:
    # January's ratio is 2.0 and March's 0.0; from the summed counts (8 observed days, 4 with dust
    # seen, 4 valid of which 3 dusty) the ratio is (1/4) / (3/4) = 1/3, not their mean
    rows = (
        _day("2024-01-01", {3: "06,1"})
        + _day("2024-01-02")
        + _day("2024-01-03", {0: "07,1"}, hours=range(0, 6, 3))
        + [r for d in (1, 2, 3) for r in _day(f"2024-03-0{d}", hours=range(0, 6, 3))]
        + _dusty_day("2024-03-04")
        + _dusty_day("2024-03-05")
    )
    t = _tables(settings, [_silver(settings, rows, 2024)])
    y = t["yearly"].row(0, named=True)
    a = _period(t["decadal"], 2021, "annual")
    for row in (y, a):
        assert row["dusty_freq_obs"] == pytest.approx(0.5)
        assert row["validity_ratio"] == pytest.approx(1 / 3)


def test_freq_checks_catch_inconsistent_columns(settings: Settings) -> None:
    rows = _day("2024-01-01", {3: "06,1"}) + _day("2024-01-02") + _day("2024-02-01")
    monthly = _tables(settings, [_silver(settings, rows, 2024)])["monthly"]
    checks = _count_order_checks("monthly")
    con = duckdb.connect()

    def failing(sql_update: str | None = None) -> set[str]:
        con.sql("CREATE OR REPLACE TABLE monthly AS SELECT * FROM monthly_df")
        if sql_update:
            con.sql(sql_update)
        return {name for name, sql in checks.items() if con.sql(sql).fetchall()[0][0]}

    con.register("monthly_df", monthly.to_arrow())
    assert failing() == set()
    assert failing("UPDATE monthly SET validity_ratio = 0.5 WHERE month = 1") == {
        "validity_ratio_matches_freqs"
    }
    assert failing("UPDATE monthly SET validity_ratio = 1.0 WHERE month = 2") == {
        "validity_ratio_null_iff_undefined"
    }
    assert failing("UPDATE monthly SET dusty_freq_obs = 1.5 WHERE month = 1") >= {
        "freq_obs_in_unit_range"
    }
    assert failing("UPDATE monthly SET dusty_freq_obs = NULL WHERE month = 2") >= {
        "freq_obs_null_iff_no_observed_day"
    }


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
    vis.add("n_bins_vis_zero")
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


def test_statistics_rows_are_sorted_by_key(settings: Settings) -> None:
    # Reproducible files: the same input gives the same row order (and the same Parquet bytes).
    paths = []
    for station in ("40582099999", "10010099999"):
        for year in (2000, 2001, 2024):
            rows = [
                r for m in (1, 4, 7, 10, 12) for r in _day(f"{year}-{m:02d}-05", STATION=station)
            ]
            paths.append(_silver(settings, rows, year, name=station))
    t = _tables(settings, paths)
    keys = {
        "monthly": ["station_id", "year", "month"],
        "yearly": ["station_id", "year"],
        "decadal": ["station_id", "decade", "period"],
    }
    for name, key in keys.items():
        assert t[name].select(key).rows() == t[name].select(key).sort(key).rows(), name


def test_statistics_counts_are_int64(settings: Settings) -> None:
    t = _tables(settings, [_silver(settings, _day("2024-01-01"), 2024)])
    assert set(t) == set(STATISTICS)
    for name, df in t.items():
        bad = [c for c, dt in df.schema.items() if c.startswith("n_") and dt != pl.Int64]
        assert bad == [], name
