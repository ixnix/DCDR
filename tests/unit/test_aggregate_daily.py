"""Gold `daily` table (aggregate.py + sql/daily.sql): one row per station per local day, built
from the gold `three_hourly` bins (method 2.0.0).

Builds small silver station-years from fake ISD rows, runs the real gold build into a temporary
data root, and checks the daily columns: bin counts, validity, dust from any report in a bin, the
per-source counts, dust at <= 5 km, weather means from the bins' state reports and extremes from all
reports, evidence kept apart, which day a bin counts on, day-to-day changes, daily-summary dating,
the CURRENT pointer with old-version pruning, and that gold refuses mixed or stale silver.
"""

import logging
import os
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from dcdr import aggregate
from dcdr.aggregate import MixedSilverVersionsError, build_gold, current_gold_dir, prune_versions
from dcdr.config import Settings, load_settings, silver_config_hash
from dcdr.process.build import build_station_year, write_silver
from tests.fixtures.isd import bronze_frame, isd_row, write_isd_history

LON0 = {"LONGITUDE": "0.0"}  # LMST == UTC keeps expectations readable


def _silver(
    settings: Settings,
    rows: list[dict],
    name: str | None = None,  # file name; default: the station, as in real silver
    cfg_hash: str | None = None,
    nil_guard: bool | None = None,
    year: int = 2024,
    previous: list[dict] | None = None,
) -> Path:
    cfg_hash = cfg_hash or silver_config_hash(settings)
    prev = bronze_frame(previous) if previous else None
    df = build_station_year(
        bronze_frame(rows),
        prev,
        year,
        settings,
        nil_guard=nil_guard,
    )
    name = name or rows[0]["STATION"]
    path = settings.data_root / "silver" / f"year={year}" / f"{name}.parquet"
    write_silver(df, path, run_id="r", config_hash=cfg_hash, source_md5="m", prev_source_md5="")
    return path


def _daily(
    settings: Settings, paths: list[Path], run_id: str = "20260101T000000Z-aaaaaa"
) -> pl.DataFrame:
    out = build_gold(settings, paths, run_id)
    return pl.read_parquet(out / "daily" / "part-000.parquet")


def _slots(day: str, hours: range = range(0, 24, 3), **fields: str) -> list[dict]:
    """One report per 3-hour bin at longitude 0 (a SYNOP unless `fields` says otherwise)."""
    return [isd_row(f"{day}T{h:02d}:00:00", **LON0, **fields) for h in hours]


def _one_day(settings: Settings, rows: list[dict], **kw: bool | None) -> dict:
    d = _daily(settings, [_silver(settings, rows, **kw)])  # type: ignore[arg-type]
    assert len(d) == 1
    return d.row(0, named=True)


def test_daily_counts_dup6_and_visibility(settings: Settings) -> None:
    rows = [
        isd_row(
            f"2024-01-01T{h:02d}:00:00",
            **LON0,
            WND="090,1,N,0100,1",
            MW1="06,1" if h in (3, 6) else "02,1",
            VIS="000000,1,N,1" if h == 0 else "016000,1,N,1",
        )
        for h in range(0, 24, 3)
    ]
    d = _one_day(settings, rows)
    got = (d["lmst_date"], d["n_reports"], d["n_bins"], d["n_bins_obs"])
    assert got == (date(2024, 1, 1), 8, 8, 8)
    assert d["valid_day"] and d["dusty_day"]
    assert (d["n_dust_bins_suspended"], d["n_bins_dusty"]) == (2, 2)
    assert d["wsd_mean"] == pytest.approx(10.0)
    assert d["dup6_mean"] == pytest.approx(1024.0)  # 10^3 * (1 + 0.6) * (1 - 0.36)
    assert (d["vis_min"], d["n_vis_zero"]) == (0.0, 1)
    assert d["vis_hmean"] == pytest.approx(10000.0)


def test_d_t_mean_needs_consecutive_valid_days(settings: Settings) -> None:
    rows = (
        _slots("2024-01-01", TMP="+0300,1", MW1="02,1")
        + _slots("2024-01-02", range(0, 6, 3), TMP="+0310,1", MW1="02,1")
        + _slots("2024-01-03", TMP="+0320,1", MW1="02,1")
        + _slots("2024-01-04", TMP="+0330,1", MW1="02,1")
    )
    d = _daily(settings, [_silver(settings, rows)]).sort("lmst_date")
    assert d["valid_day"].to_list() == [True, False, True, True]
    assert d["d_t_mean"].to_list()[:3] == [None, None, None]
    assert d["d_t_mean"].to_list()[3] == pytest.approx(1.0)


def test_day_with_only_excluded_types_has_no_row(settings: Settings) -> None:
    rows = _slots("2024-01-01", MW1="02,1") + [
        isd_row("2024-01-02T23:59:00", **LON0, REPORT_TYPE="SOD")
    ]
    d = _daily(settings, [_silver(settings, rows)])
    assert d["lmst_date"].to_list() == [date(2024, 1, 1)]


def test_mixed_silver_versions_are_refused(settings: Settings) -> None:
    a = _silver(settings, _slots("2024-01-01"), name="A", cfg_hash="h1")
    b = _silver(settings, _slots("2024-01-01"), name="B", cfg_hash="h2")
    with pytest.raises(MixedSilverVersionsError):
        build_gold(settings, [a, b], "20260101T000000Z-aaaaaa")


STA, STB = "40582099999", "40582199999"  # two stations, one per silver file


def _silver_extra_column(settings: Settings, rows: list[dict], name: str) -> Path:
    """A valid silver file with one column more than `_silver` writes (a different schema)."""
    df = build_station_year(bronze_frame(rows), None, 2024, settings)
    df = df.with_columns(pl.lit(1).alias("extra_column"))
    path = settings.data_root / "silver" / "year=2024" / f"{name}.parquet"
    cfg = silver_config_hash(settings)
    write_silver(df, path, run_id="r", config_hash=cfg, source_md5="m", prev_source_md5="")
    return path


def test_uniform_silver_schema_is_read_without_union(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    a = _silver(settings, _slots("2024-01-01", MW1="06,1", STATION=STA))
    b = _silver(settings, _slots("2024-01-01", MW1="06,1", STATION=STB))
    with caplog.at_level(logging.INFO, logger="dcdr.aggregate"):
        d = _daily(settings, [a, b])
    assert sorted(d["station_id"].unique().to_list()) == [STA, STB]
    assert "2 files, 1 schema" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_mixed_silver_schemas_fall_back_to_union_with_a_warning(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    a = _silver(settings, _slots("2024-01-01", MW1="06,1", STATION=STA))
    b = _silver_extra_column(settings, _slots("2024-01-01", MW1="06,1", STATION=STB), name=STB)
    with caplog.at_level(logging.INFO, logger="dcdr.aggregate"):
        d = _daily(settings, [a, b])
    assert sorted(d["station_id"].unique().to_list()) == [STA, STB]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "2 schemas" in warnings[0]


def test_silver_check_reports_progress(
    settings: Settings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(aggregate, "PROGRESS_EVERY", 2)
    paths = [_silver(settings, _slots("2024-01-01", STATION=f"4058{i}099999")) for i in "012"]
    with caplog.at_level(logging.INFO, logger="dcdr.aggregate"):
        build_gold(settings, paths, "20260101T000000Z-aaaaaa")
    assert "silver check: 2/3 files" in caplog.text


def test_current_pointer_and_pruning(tmp_path: Path) -> None:
    s = load_settings(
        overrides={
            "paths": {"data_root": str(tmp_path / "data")},
            "products": {"gold_keep_versions": 2},
        }
    )
    write_isd_history(s)
    path = _silver(s, _slots("2024-01-01"))
    run_ids = [f"20260101T00000{i}Z-aaaaaa" for i in range(4)]
    for rid in run_ids:
        build_gold(s, [path], rid)
    assert current_gold_dir(s) == s.data_root / "gold" / "versions" / run_ids[-1]
    remaining = sorted(p.name for p in (s.data_root / "gold" / "versions").iterdir())
    assert remaining == run_ids[-2:]


LV = {"LONGITUDE": "-115.16343"}  # Las Vegas: LMST runs ~18 min ahead of local standard time


def test_daily_summary_dated_to_the_day_it_summarises(settings: Settings) -> None:
    """Review C1: SOD rows are stamped 23:59 local STANDARD time (07:59Z next day at Las Vegas)."""
    rows = [
        isd_row(f"2024-01-{d:02d}T{h:02d}:00:00", **LV, MW1="02,1")
        for d in (6, 7, 8)
        for h in range(0, 24, 3)
    ] + [isd_row("2024-01-07T07:59:00", **LV, REPORT_TYPE="SOD", AT1="AU,07,DU  ,5")]
    d = _daily(settings, [_silver(settings, rows)])
    flagged = d.filter(pl.col("daily_summary_dust"))["lmst_date"].to_list()
    assert flagged == [date(2024, 1, 6)]


def test_gold_refuses_silver_built_with_other_config(settings: Settings) -> None:
    """Review I4: gold must not silently use stale silver."""
    from dcdr.aggregate import StaleSilverError

    stale = _silver(settings, _slots("2024-01-01"), cfg_hash="not-the-current-hash")
    with pytest.raises(StaleSilverError):
        build_gold(settings, [stale], "20260101T000000Z-aaaaaa")


def test_pruning_survives_files_vanishing_during_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On exFAT, macOS deletes the hidden "._x" twin together with "x" (found on the SSD).

    The fake unlink below deletes both files of a pair whichever is removed first, so the
    second deletion always finds its file gone. Pruning must still succeed.
    """
    gold = tmp_path / "gold"
    for rid in ("20260101T000000Z-aaaaaa", "20260102T000000Z-bbbbbb"):
        daily = gold / "versions" / rid / "daily"
        daily.mkdir(parents=True)
        (daily / "part-000.parquet").write_text("x")
        (daily / "._part-000.parquet").write_text("x")
    (gold / "CURRENT").write_text("20260102T000000Z-bbbbbb")
    real_unlink = os.unlink

    def unlink_with_twin(path: str, *, dir_fd: int | None = None) -> None:
        real_unlink(path, dir_fd=dir_fd)
        head, base = os.path.split(os.fspath(path))
        twin = base[2:] if base.startswith("._") else "._" + base
        try:
            real_unlink(os.path.join(head, twin), dir_fd=dir_fd)
        except FileNotFoundError:
            pass

    monkeypatch.setattr(os, "unlink", unlink_with_twin)
    prune_versions(gold, keep=1)
    assert [p.name for p in (gold / "versions").iterdir()] == ["20260102T000000Z-bbbbbb"]


def test_daily_keeps_inferred_nil_bins_visible(settings: Settings) -> None:
    """Rule V3: inferred "nothing significant" bins make a day valid, but are counted apart, and
    valid_day_explicit ignores them."""
    rows = [isd_row(f"2024-03-01T{h:02d}:00:00", **LON0, REM="SYN004BUFR") for h in range(0, 24, 3)]
    d = _one_day(settings, rows, nil_guard=True)
    got = (d["n_bins_obs"], d["n_bins_nil_inferred"], d["valid_day"], d["valid_day_explicit"])
    assert got == (8, 8, True, False)


# --- all reports in a bin, by source (method 2.0.0, user decisions 2026-11-02) ----------------
METAR_CLEAR = "MET000METAR KELP 010000Z 25010KT 10SM CLR 30/02 A3000"
KELP_RMK = "MET100KELP 010051Z 12012G27KT 10SM CLR 30/02 A3027 RMK AO2 SLP042 BLDU DSNT SW"
UNKNOWN_VIS = "999999,9,N,9"


def test_dusty_hourly_metar_with_clear_synop_makes_dusty_day(settings: Settings) -> None:
    """Niamey 2020: dust reported only in an hourly METAR between the SYNOPs."""
    rows = _slots("2024-05-27", MW1="02,1") + [
        isd_row("2024-05-27T13:00:00", **LON0, REPORT_TYPE="FM-15", MW1="07,1")
    ]
    d = _one_day(settings, rows)
    assert d["dusty_day"] and d["valid_day"]
    got = (d["n_bins_dusty"], d["n_bins_dust_metar"], d["n_bins_dust_synop"])
    assert got == (1, 1, 0)


def test_day_with_3_observed_bins_is_not_valid(settings: Settings) -> None:
    rows = _slots("2024-01-01", range(0, 9, 3), MW1="02,1") + _slots(
        "2024-01-01", range(9, 24, 3)
    )  # five SYNOPs without weather code or iX: not observed
    d = _one_day(settings, rows)
    assert (d["n_bins"], d["n_bins_obs"], d["valid_day"]) == (8, 3, False)


def test_source_counts_per_day(settings: Settings) -> None:
    rows = (
        [isd_row("2024-06-01T00:00:00", **LON0, MW1="07,1")]  # SYNOP dust
        + _slots("2024-06-01", range(3, 21, 3), MW1="02,1")  # SYNOP clear, 03-18
        + [isd_row("2024-06-01T18:51:00", **LON0, REPORT_TYPE="FM-15", MW1="07,1")]  # METAR dust
        + [isd_row("2024-06-01T21:00:00", **LON0, REPORT_TYPE="AERO", MW1="07,1")]  # other dust
    )
    d = _one_day(settings, rows)
    dust = (d["n_bins_dust_synop"], d["n_bins_dust_metar"], d["n_bins_dust_other"])
    obs = (d["n_bins_obs_synop"], d["n_bins_obs_metar"], d["n_bins_obs_other"])
    assert (dust, obs, d["n_bins_dusty"]) == ((1, 1, 1), (7, 1, 1), 3)
    assert (d["valid_day_synop"], d["valid_day_metar"], d["valid_day_other"]) == (
        True,
        False,
        False,
    )
    assert (d["dusty_day_synop"], d["dusty_day_metar"], d["dusty_day_other"]) == (True, True, True)
    assert d["dusty_day"] and d["valid_day"]


def test_metar_only_day_is_valid_and_counted_apart(settings: Settings) -> None:
    """A METAR with no weather group at > 5 km is "nothing significant, implied": the day is
    valid, but not valid from explicit observations, and its bins count as METAR-observed."""
    rows = _slots("2024-04-01", REPORT_TYPE="FM-15", REM=METAR_CLEAR, VIS="016093,1,N,1")
    d = _one_day(settings, rows)
    got = (
        d["n_bins_obs"],
        d["n_bins_nil_metar"],
        d["n_bins_obs_metar"],
        d["valid_day"],
        d["valid_day_explicit"],
        d["valid_day_vis5"],
        d["dusty_day"],
    )
    assert got == (8, 8, 8, True, False, True, False)


@pytest.mark.parametrize(
    "dust_vis,dusty_vis5,n_obs_vis5",
    [
        ("008000,1,N,1", False, 8),  # dust at 8 km: dusty, but not dust at <= 5 km
        ("003000,1,N,1", True, 8),  # dust at 3 km: both
        ("005000,1,N,1", True, 8),  # 5 000 m is included ("5 000 metres or less")
        (UNKNOWN_VIS, False, 7),  # dust, visibility unknown: bin left out of vis5
    ],
)
def test_vis5_day(settings: Settings, dust_vis: str, dusty_vis5: bool, n_obs_vis5: int) -> None:
    rows = [
        isd_row(
            f"2024-05-01T{h:02d}:00:00",
            **LON0,
            MW1="07,1" if h == 0 else "02,1",
            VIS=dust_vis if h == 0 else "016000,1,N,1",
        )
        for h in range(0, 24, 3)
    ]
    d = _one_day(settings, rows)
    got = (d["dusty_day"], d["dusty_day_vis5"], d["n_bins_obs_vis5"], d["valid_day_vis5"])
    assert got == (True, dusty_vis5, n_obs_vis5, True)


def test_vis5_bin_with_dust_of_unknown_visibility_is_not_judged(settings: Settings) -> None:
    rows = _slots("2024-05-01", range(3, 24, 3), MW1="02,1") + [
        isd_row("2024-05-01T00:00:00", **LON0, MW1="07,1", VIS="008000,1,N,1"),
        isd_row("2024-05-01T00:30:00", **LON0, REPORT_TYPE="FM-15", MW1="07,1", VIS=UNKNOWN_VIS),
    ]
    d = _one_day(settings, rows)
    assert (d["dusty_day"], d["dusty_day_vis5"], d["n_bins_obs_vis5"]) == (True, False, 7)


def test_means_from_state_reports_extremes_from_all(settings: Settings) -> None:
    rows = _slots("2024-07-01", MW1="02,1", TMP="+0200,1", WND="090,1,N,0050,1") + [
        isd_row(
            "2024-07-01T00:51:00", **LON0, REPORT_TYPE="FM-15", TMP="+0300,1", WND="090,1,N,0120,1"
        ),
        isd_row("2024-07-01T04:10:00", **LON0, REPORT_TYPE="FM-16", OC1="0220,1"),
    ]
    d = _one_day(settings, rows)
    assert (d["t_mean"], d["t_max"], d["wsd_mean"]) == (20.0, 20.0, 5.0)  # the SYNOPs only
    assert (d["wsd_max"], d["gust_max"]) == (12.0, 22.0)  # over all reports


def test_no_visibility_fallback(settings: Settings) -> None:
    rows = _slots("2024-07-02", MW1="02,1", VIS=UNKNOWN_VIS) + [
        isd_row("2024-07-02T00:51:00", **LON0, REPORT_TYPE="FM-15", VIS="003000,1,N,1")
    ]
    d = _one_day(settings, rows)
    assert (d["vis_hmean"], d["vis_min"]) == (None, 3000.0)


def test_dust_first_lmst_hour_is_bin_start(settings: Settings) -> None:
    """Review Focus 5: at 15 E (LMST = UTC + 1 h), dust at 23:30 UTC is in the 21 UTC bin, which
    starts at 22:00 LMST on the same day; a report-level hour would read 0.5."""
    e15 = {"LONGITUDE": "15.0"}
    rows = [isd_row(f"2024-01-01T{h:02d}:00:00", **e15, MW1="02,1") for h in range(0, 24, 3)]
    rows.append(isd_row("2024-01-01T23:30:00", **e15, REPORT_TYPE="FM-15", MW1="07,1"))
    d = _one_day(settings, rows)
    assert (d["lmst_date"], d["dusty_day"]) == (date(2024, 1, 1), True)
    assert d["dust_first_lmst_hour"] == pytest.approx(22.0)


def test_bin_counts_on_the_lmst_date_it_starts_across_years(settings: Settings) -> None:
    """Review Focus 3: at 45 E (LMST = UTC + 3 h) the 21 UTC bin of 31 December starts at 00:00
    LMST on 1 January; that day is read from two silver year files and has 8 bins."""
    e45 = {"LONGITUDE": "45.0"}
    y2023 = [isd_row(f"2023-12-31T{h:02d}:00:00", **e45, MW1="02,1") for h in range(0, 24, 3)]
    y2024 = [isd_row(f"2024-01-01T{h:02d}:00:00", **e45, MW1="02,1") for h in range(0, 24, 3)]
    paths = [
        _silver(settings, y2023, year=2023),
        _silver(settings, y2024, year=2024, previous=y2023),
    ]
    d = _daily(settings, paths)
    n_bins = dict(zip(d["lmst_date"].to_list(), d["n_bins"].to_list(), strict=True))
    assert (n_bins[date(2023, 12, 31)], n_bins[date(2024, 1, 1)]) == (7, 8)


def test_evidence_is_never_counted(settings: Settings) -> None:
    """Past weather, a daily summary, vicinity and remarks dust are evidence, kept apart."""
    rows = _slots("2024-01-01", range(3, 24, 3), MW1="02,1") + [
        isd_row("2024-01-01T00:00:00", **LON0, MW1="02,1", MV1="08,1"),  # vicinity (SYNOP MV)
        isd_row("2024-01-01T06:30:00", **LON0, REPORT_TYPE="FM-15", AY1="3,1,06,1"),  # past
        isd_row("2024-01-01T09:51:00", **LON0, REPORT_TYPE="FM-15", REM=KELP_RMK),  # remarks
        isd_row("2024-01-01T23:59:00", **LON0, REPORT_TYPE="SOD", AX1="07,5,24,5"),  # summary
    ]
    d = _one_day(settings, rows)
    assert not d["dusty_day"]
    got = (
        d["past_weather_dust"],
        d["daily_summary_dust"],
        d["vicinity_dust_day"],
        d["remarks_dust_day"],
    )
    assert got == (True, True, True, True)


def test_vicinity_dust_day_is_a_separate_flag(settings: Settings) -> None:
    rows = _slots(
        "2024-04-02",
        REPORT_TYPE="FM-15",
        REM="MET000METAR KLAS 020000Z 22016KT 10SM VCBLDU FEW250 17/M08 A2967",
        AU1="4,5,00,5,0,1,1",  # VC (in the vicinity) BL DU
        VIS="016093,1,N,1",
    )
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    assert (d["vicinity_dust_day"], d["dusty_day"]) == (True, False)


def test_remarks_dust_day_is_a_separate_flag(settings: Settings) -> None:
    rows = _slots(
        "2024-04-02",
        REPORT_TYPE="FM-15",
        REM="MET000METAR KELP 020000Z 25020G33KT 10SM FEW250 27/M20 A2986 RMK AO2 BLDU ALQDS",
        VIS="016093,1,N,1",
    )
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    got = (d["remarks_dust_day"], d["vicinity_dust_day"], d["dusty_day"])
    assert got == (True, False, False)
