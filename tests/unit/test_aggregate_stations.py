"""Gold `stations` table (aggregate.py + sql/stations.sql): which station position to trust.

Builds small silver station-years from fake ISD rows and a fixture isd-history, runs the real
gold build into a temporary data root, and checks the status of each station (agree, moved,
conflict, the 949999 rule and its southern-hemisphere gate, unverified, overrides), the review
list, and the lineage of the inputs.
"""

import csv
import json
from pathlib import Path

import polars as pl
import pytest

from dcdr.aggregate import build_gold
from dcdr.config import Settings, load_settings, silver_config_hash
from dcdr.process.build import build_station_year, write_silver
from dcdr.stations import file_md5
from tests.fixtures.isd import bronze_frame, isd_row, write_isd_history

RUN = "20260101T000000Z-aaaaaa"
KUWAIT = "40582099999"


def _station(
    settings: Settings, station: str, lat: str, lon: str, n_dust: int = 0, name: str = ""
) -> Path:
    """One day of 3-hourly SYNOPs at the given in-file position; the first `n_dust` have MW 07."""
    rows = [
        isd_row(
            f"2024-01-01T{h:02d}:00:00",
            STATION=station,
            LATITUDE=lat,
            LONGITUDE=lon,
            MW1="07,1" if i < n_dust else "02,1",
        )
        for i, h in enumerate(range(0, 24, 3))
    ]
    df = build_station_year(bronze_frame(rows), None, 2024, settings)
    path = settings.data_root / "silver" / "year=2024" / f"{name or station}.parquet"
    write_silver(
        df,
        path,
        run_id="r",
        config_hash=silver_config_hash(settings),
        source_md5="m",
        prev_source_md5="",
    )
    return path


def _history(settings: Settings, *rows: tuple[str, str, str]) -> Path:
    """isd-history rows (station_id, LAT, LON)."""
    return write_isd_history(
        settings,
        [
            {"USAF": s[:6], "WBAN": s[6:], "STATION NAME": s, "LAT": la, "LON": lo}
            for s, la, lo in rows
        ],
    )


def _build(settings: Settings, paths: list[Path]) -> Path:
    return build_gold(settings, paths, RUN)


def _stations(out: Path) -> dict[str, dict]:
    df = pl.read_parquet(out / "stations" / "part-000.parquet")
    return {r["station_id"]: r for r in df.iter_rows(named=True)}


def _review(out: Path) -> list[dict[str, str]]:
    with (out / "stations_review.csv").open() as f:
        return list(csv.DictReader(f))


def _with_overrides(settings: Settings, tmp_path: Path, *rows: str) -> Settings:
    path = tmp_path / "position_overrides.csv"
    path.write_text("station_id,lat,lon,action,reason,decided\n" + "".join(r + "\n" for r in rows))
    s = load_settings(
        overrides={
            "paths": {"data_root": str(settings.data_root)},
            "stations": {
                "position_overrides": str(path),
                "min_dusty_days": 0,  # these stations have no dust
                "min_dusty_years": 0,
            },
        }
    )
    return s


@pytest.mark.parametrize(
    ("isd_lon", "status", "in_gold"),
    [
        ("+047.969", "agree", True),
        ("+048.200", "moved", True),  # ~22 km east
        ("+049.000", "conflict", False),  # ~100 km east
    ],
)
def test_status_from_distance(settings: Settings, isd_lon: str, status: str, in_gold: bool) -> None:
    _history(settings, (KUWAIT, "+29.227", isd_lon))
    st = _stations(_build(settings, [_station(settings, KUWAIT, "29.227", "47.969")]))[KUWAIT]
    assert (st["status"], st["in_gold"]) == (status, in_gold)
    assert (st["trusted_lon"] is None) == (not in_gold)


def test_949999_conflict_resolved_by_isd_history(settings: Settings) -> None:
    sid = "94999900108"  # in-file "SCAMMON BAY AIRPORT, AK US"; isd-history ADELAIDE
    _history(settings, (sid, "-34.930", "+138.580"))
    st = _stations(_build(settings, [_station(settings, sid, "61.78", "-165.57")]))[sid]
    assert (st["status"], st["in_gold"]) == ("resolved_isd_history", True)
    assert (st["trusted_lat"], st["trusted_lon"]) == (-34.93, 138.58)


def test_949999_northern_isd_history_is_a_conflict(settings: Settings) -> None:
    sid = "94999900100"  # BUNDALEER FOREST RESERVE: isd-history puts it in Arkansas
    _history(settings, (sid, "+34.100", "-093.066"))
    st = _stations(_build(settings, [_station(settings, sid, "34.1", "-93.066")]))[sid]
    assert (st["status"], st["in_gold"], st["max_km"]) == ("conflict", False, 0.0)


def test_unlisted_or_zero_position_is_unverified(settings: Settings) -> None:
    _history(settings, ("00702699999", "+00.000", "+000.000"))
    out = _build(
        settings,
        [
            _station(settings, KUWAIT, "29.227", "47.969"),  # not in isd-history
            _station(settings, "00702699999", "10.0", "20.0"),  # isd-history at 0/0
        ],
    )
    st = _stations(out)
    for sid in (KUWAIT, "00702699999"):
        assert (st[sid]["status"], st[sid]["in_gold"]) == ("unverified", True)
        assert st[sid]["trusted_lon"] == st[sid]["file_lon"]


def test_station_without_in_file_latitude_is_unverified(settings: Settings) -> None:
    _history(settings, (KUWAIT, "+29.227", "+047.969"))
    st = _stations(_build(settings, [_station(settings, KUWAIT, "99.999", "47.969")]))[KUWAIT]
    assert (st["status"], st["max_km"]) == ("unverified", None)
    assert st["trusted_lon"] == pytest.approx(47.969)


def test_distance_across_the_date_line(settings: Settings) -> None:
    sid = "91000099999"
    _history(settings, (sid, "+00.000", "+179.950"))
    st = _stations(_build(settings, [_station(settings, sid, "0.0", "-179.95")]))[sid]
    assert st["max_km"] == pytest.approx(11.1, abs=0.2)
    assert st["status"] == "moved"


def test_override_use_and_exclude(settings: Settings, tmp_path: Path) -> None:
    other = "40583099999"
    _history(settings, (KUWAIT, "+29.227", "+047.969"), (other, "+29.227", "+049.000"))
    s = _with_overrides(
        settings,
        tmp_path,
        f"{KUWAIT},,,exclude,test exclude,2026-11-03",
        f"{other},29.0,48.5,use,checked by hand,2026-11-03",
    )
    paths = [_station(s, KUWAIT, "29.227", "47.969"), _station(s, other, "29.227", "47.969")]
    st = _stations(_build(s, paths))
    assert (st[KUWAIT]["status"], st[KUWAIT]["computed_status"], st[KUWAIT]["in_gold"]) == (
        "override",
        "agree",
        False,
    )
    assert (st[other]["computed_status"], st[other]["trusted_lon"], st[other]["in_gold"]) == (
        "conflict",
        48.5,
        True,
    )
    assert st[other]["override_reason"] == "checked by hand"


def test_unused_override_is_listed(settings: Settings, tmp_path: Path) -> None:
    s = _with_overrides(settings, tmp_path, "99999999999,10.0,10.0,use,no data yet,2026-11-03")
    out = _build(s, [_station(s, KUWAIT, "29.227", "47.969")])
    unused = [r for r in _review(out) if r["status"] == "override_unused"]
    assert [r["station_id"] for r in unused] == ["99999999999"]


def test_review_list_sorted_by_dusty_reports(settings: Settings) -> None:
    a, b = "40583099999", "40584099999"
    _history(
        settings,
        (KUWAIT, "+29.227", "+047.969"),
        (a, "+29.227", "+049.000"),
        (b, "+29.227", "+049.000"),
    )
    out = _build(
        settings,
        [
            _station(settings, KUWAIT, "29.227", "47.969"),  # agree: not listed
            _station(settings, a, "29.227", "47.969", n_dust=1),
            _station(settings, b, "29.227", "47.969", n_dust=3),
        ],
    )
    review = _review(out)
    assert [r["station_id"] for r in review] == [b, a]
    assert [r["n_dusty_reports"] for r in review] == ["3", "1"]


def test_lineage_records_input_md5s(settings: Settings) -> None:
    hist = _history(settings, (KUWAIT, "+29.227", "+047.969"))
    out = _build(settings, [_station(settings, KUWAIT, "29.227", "47.969")])
    lineage = json.loads((out / "silver_version.json").read_text())
    assert lineage["isd_history_md5"] == file_md5(hist)
    assert lineage["overrides_md5"] == ""
    assert _stations(out)[KUWAIT]["isd_history_md5"] == file_md5(hist)


# --- gold uses the trusted positions ----------------------------------------------------------
ADELAIDE = "94999900108"  # in-file "SCAMMON BAY AIRPORT, AK US" (61.78, -165.57)


def _table(out: Path, name: str) -> pl.DataFrame:
    return pl.read_parquet(out / name / "part-000.parquet")


def test_conflict_station_left_out_of_gold(settings: Settings) -> None:
    other = "40583099999"
    _history(settings, (KUWAIT, "+29.227", "+047.969"), (other, "+29.227", "+049.000"))
    out = _build(
        settings,
        [
            _station(settings, KUWAIT, "29.227", "47.969"),
            _station(settings, other, "29.227", "47.969"),
        ],
    )
    assert other in _stations(out)
    for name in ("three_hourly", "daily", "monthly"):
        assert set(_table(out, name)["station_id"]) == {KUWAIT}


def test_resolved_949999_uses_isd_history_offset(settings: Settings) -> None:
    _history(settings, (ADELAIDE, "-34.930", "+138.580"))
    out = _build(settings, [_station(settings, ADELAIDE, "61.78", "-165.57")])
    first = _table(out, "three_hourly").sort("bin_time").row(0, named=True)
    assert first["lmst_hour"] == pytest.approx(138.58 / 15, abs=1e-6)
    assert str(first["lmst_date"]) == "2024-01-01"


def test_summary_dated_with_trusted_offset(settings: Settings) -> None:
    """At Adelaide (+9:14 LMST) an SOD stamped 13:59 UTC summarises 6 January; with the in-file
    US longitude (-11:02) it would be dated 5 January."""
    _history(settings, (ADELAIDE, "-34.930", "+138.580"))
    pos = {"STATION": ADELAIDE, "LATITUDE": "61.78", "LONGITUDE": "-165.57"}
    rows = [isd_row(f"2024-01-06T{h:02d}:00:00", **pos, MW1="02,1") for h in range(0, 24, 3)]
    rows.append(isd_row("2024-01-06T13:59:00", **pos, REPORT_TYPE="SOD", AT1="AU,07,DU  ,5"))
    df = build_station_year(bronze_frame(rows), None, 2024, settings)
    path = settings.data_root / "silver" / "year=2024" / f"{ADELAIDE}.parquet"
    write_silver(
        df,
        path,
        run_id="r",
        config_hash=silver_config_hash(settings),
        source_md5="m",
        prev_source_md5="",
    )
    daily = _table(_build(settings, [path]), "daily")
    flagged = daily.filter(pl.col("daily_summary_dust"))["lmst_date"].cast(pl.String).to_list()
    assert flagged == ["2024-01-06"]


def test_station_status_counts(settings: Settings) -> None:
    from dcdr.aggregate import station_status_counts

    other = "40583099999"
    _history(settings, (KUWAIT, "+29.227", "+047.969"), (other, "+29.227", "+049.000"))
    out = _build(
        settings,
        [
            _station(settings, KUWAIT, "29.227", "47.969"),
            _station(settings, other, "29.227", "47.969"),
        ],
    )
    assert station_status_counts(out) == {"agree": 1, "conflict": 1, "not_dusty": 0}


def test_949999_without_in_file_latitude_is_a_conflict(settings: Settings) -> None:
    """Review finding: with no in-file latitude the distance cannot be checked, and the in-file
    longitude of a 949999 station may be a US one; not verifiable -> conflict, not unverified."""
    _history(settings, (ADELAIDE, "-34.930", "+138.580"))
    st = _stations(_build(settings, [_station(settings, ADELAIDE, "99.999", "-165.57")]))[ADELAIDE]
    assert (st["status"], st["in_gold"]) == ("conflict", False)


# --- dusty-station threshold (user decision 2026-11-03: >= 10 dusty days in >= 2 years) -------


def _threshold(settings: Settings) -> Settings:
    """The test settings with the default threshold (10 dusty days, 2 years) switched back on."""
    return load_settings(
        overrides={
            "paths": {"data_root": str(settings.data_root)},
            "stations": {
                "position_overrides": settings.stations.position_overrides,
                "min_dusty_days": 10,
                "min_dusty_years": 2,
            },
        }
    )


def _dust_days(
    settings: Settings, station: str, days: list[str], extra: list[dict] | None = None
) -> list[Path]:
    """Silver for `station` (longitude 0) with one dusty SYNOP (MW 07) at 12 UTC on each day and a
    clear one at 00 UTC; one file per year."""
    rows = [
        isd_row(f"{d}T{h}:00:00", STATION=station, LONGITUDE="0.0", MW1=mw)
        for d in days
        for h, mw in (("00", "02,1"), ("12", "07,1"))
    ] + (extra or [])
    paths = []
    for year in sorted({int(r["DATE"][:4]) for r in rows}):
        part = [r for r in rows if r["DATE"].startswith(str(year))]
        df = build_station_year(bronze_frame(part), None, year, settings)
        path = settings.data_root / "silver" / f"year={year}" / f"{station}.parquet"
        write_silver(
            df,
            path,
            run_id="r",
            config_hash=silver_config_hash(settings),
            source_md5="m",
            prev_source_md5="",
        )
        paths.append(path)
    return paths


TEN_DAYS_TWO_YEARS = [f"2023-03-{d:02d}" for d in range(1, 6)] + [
    f"2024-03-{d:02d}" for d in range(1, 6)
]


def test_dusty_station_needs_10_days_in_2_years(settings: Settings) -> None:
    s = _threshold(settings)
    kept, few, one_year = "40582099999", "40583099999", "40584099999"
    paths = (
        _dust_days(s, kept, TEN_DAYS_TWO_YEARS)
        + _dust_days(s, few, TEN_DAYS_TWO_YEARS[1:])  # 9 days
        + _dust_days(s, one_year, [f"2024-03-{d:02d}" for d in range(1, 11)])  # 10 days, 1 year
    )
    out = _build(s, paths)
    st = _stations(out)
    got = {
        k: (st[k]["n_dusty_days"], st[k]["n_dusty_years"], st[k]["is_dusty_station"]) for k in st
    }
    assert got == {kept: (10, 2, True), few: (9, 2, False), one_year: (10, 1, False)}
    assert [k for k in st if st[k]["in_gold"]] == [kept]
    for name in ("three_hourly", "daily", "monthly", "yearly", "decadal"):
        assert set(_table(out, name)["station_id"]) == {kept}


def test_two_dusty_reports_on_one_local_day_count_once(settings: Settings) -> None:
    s = _threshold(settings)
    extra = [isd_row("2024-03-01T15:00:00", STATION=KUWAIT, LONGITUDE="0.0", MW1="07,1")]
    st = _stations(_build(s, _dust_days(s, KUWAIT, TEN_DAYS_TWO_YEARS, extra)))[KUWAIT]
    assert (st["n_dusty_days"], st["n_dusty_years"]) == (10, 2)


def test_review_list_only_shows_dusty_stations(settings: Settings) -> None:
    s = _threshold(settings)
    dusty, clear = "40582099999", "40583099999"
    _history(s, (dusty, "+29.227", "+049.000"), (clear, "+29.227", "+049.000"))  # both conflicts
    paths = _dust_days(s, dusty, TEN_DAYS_TWO_YEARS) + _dust_days(s, clear, ["2024-03-01"])
    out = _build(s, paths)
    assert [r["station_id"] for r in _review(out)] == [dusty]
    assert station_counts_not_dusty(out) == 1


def station_counts_not_dusty(out: Path) -> int:
    from dcdr.aggregate import station_status_counts

    return station_status_counts(out)["not_dusty"]
