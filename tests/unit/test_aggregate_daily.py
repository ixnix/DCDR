"""Gold `daily` table (aggregate.py + sql/daily.sql): one row per station per local day.

Builds small silver station-years from fake ISD rows, runs the real gold build into a temporary
data root, and checks the daily columns: slot counts, dust counts, DUP6, harmonic-mean
visibility, day-to-day changes, daily-summary dating, which day a slot counts on, the CURRENT
pointer with old-version pruning, and that gold refuses mixed or stale silver.
"""

import os
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from dcdr.aggregate import MixedSilverVersionsError, build_gold, current_gold_dir, prune_versions
from dcdr.config import Settings, load_settings, silver_config_hash
from dcdr.process.build import build_station_year, write_silver
from tests.fixtures.isd import bronze_frame, isd_row

LON0 = {"LONGITUDE": "0.0"}  # LMST == UTC keeps expectations readable


def _silver(
    settings: Settings,
    rows: list[dict],
    name: str = "S",
    cfg_hash: str | None = None,
    nil_guard: bool | None = None,
) -> Path:
    cfg_hash = cfg_hash or silver_config_hash(settings)
    df = build_station_year(bronze_frame(rows), None, 2024, settings, nil_guard=nil_guard)
    path = settings.data_root / "silver" / "year=2024" / f"{name}.parquet"
    write_silver(df, path, run_id="r", config_hash=cfg_hash, source_md5="m", prev_source_md5="")
    return path


def _daily(
    settings: Settings, paths: list[Path], run_id: str = "20260101T000000Z-aaaaaa"
) -> pl.DataFrame:
    out = build_gold(settings, paths, run_id)
    return pl.read_parquet(out / "daily" / "part-000.parquet")


def _slots(day: str, hours: range = range(0, 24, 3), **fields: str) -> list[dict]:
    return [isd_row(f"{day}T{h:02d}:00:00", **LON0, **fields) for h in hours]


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
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    assert (d["lmst_date"], d["n_reports"], d["n_slots"], d["n_slots_pw"]) == (
        date(2024, 1, 1),
        8,
        8,
        8,
    )
    assert d["valid_day"] and d["dusty_day"] and d["dusty_day_any"]
    assert d["n_dust_slots_suspended"] == 2
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


def test_current_pointer_and_pruning(tmp_path: Path) -> None:
    s = load_settings(
        overrides={
            "paths": {"data_root": str(tmp_path / "data")},
            "products": {"gold_keep_versions": 2},
        }
    )
    path = _silver(s, _slots("2024-01-01"))
    run_ids = [f"20260101T00000{i}Z-aaaaaa" for i in range(4)]
    for rid in run_ids:
        build_gold(s, [path], rid)
    assert current_gold_dir(s) == s.data_root / "gold" / "versions" / run_ids[-1]
    remaining = sorted(p.name for p in (s.data_root / "gold" / "versions").iterdir())
    assert remaining == run_ids[-2:]


def test_dusty_day_any_source_uses_past_and_daily_summaries(settings: Settings) -> None:
    rows = (
        _slots("2024-01-01", MW1="02,1")
        + [isd_row("2024-01-01T06:30:00", **LON0, REPORT_TYPE="FM-15", AY1="3,1,06,1")]
        + _slots("2024-01-02", MW1="02,1")
        + [isd_row("2024-01-02T23:59:00", **LON0, REPORT_TYPE="SOD", AX1="07,5,24,5")]
        + _slots("2024-01-03", MW1="02,1")
    )
    d = _daily(settings, [_silver(settings, rows)]).sort("lmst_date")
    assert d["dusty_day"].to_list() == [False, False, False]
    assert d["dusty_day_any_source"].to_list() == [True, True, False]


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


def test_slot_counted_on_its_own_lmst_day(settings: Settings) -> None:
    """Review I1: a 20:35Z report filling the 21Z slot must count on the slot's LMST day (Kuwait)."""
    kw = {"LONGITUDE": "47.968928"}  # LMST = UTC + 3:11:52; slot 21Z -> 00:11 LMST next day
    rows = (
        [isd_row("2024-02-29T21:00:00", **kw, MW1="02,1")]
        + [isd_row(f"2024-03-01T{h:02d}:00:00", **kw, MW1="02,1") for h in range(0, 21, 3)]
        + [isd_row("2024-03-01T20:35:00", **kw, REPORT_TYPE="FM-15", MW1="02,1")]
        + [isd_row(f"2024-03-02T{h:02d}:00:00", **kw, MW1="02,1") for h in range(0, 21, 3)]
    )
    d = _daily(settings, [_silver(settings, rows)]).sort("lmst_date")
    by_day = dict(zip(d["lmst_date"].to_list(), d["n_slots"].to_list(), strict=True))
    assert by_day[date(2024, 3, 1)] == 8 and by_day[date(2024, 3, 2)] == 8


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


def test_daily_keeps_inferred_nil_slots_visible(settings: Settings) -> None:
    """Rule V3: inferred "nothing significant" slots make a day valid, but are counted apart, and
    valid_day_explicit ignores them."""
    rows = [isd_row(f"2024-03-01T{h:02d}:00:00", **LON0, REM="SYN004BUFR") for h in range(0, 24, 3)]
    d = _daily(settings, [_silver(settings, rows, nil_guard=True)]).row(0, named=True)
    got = (d["n_slots_pw"], d["n_slots_nil_inferred"], d["valid_day"], d["valid_day_explicit"])
    assert got == (8, 8, True, False)


# --- METAR slots, dust at <= 5 km, vicinity (user decisions 2026-10-20) ---------------------
METAR_CLEAR = "MET000METAR KELP 010000Z 25010KT 10SM CLR 30/02 A3000"


def test_metar_only_day_is_valid_and_counted_apart(settings: Settings) -> None:
    """A METAR with no weather group at > 5 km fills a slot as "nothing significant, implied":
    the day is valid, but not valid from explicit observations, and its METAR slots are counted."""
    rows = _slots("2024-04-01", REPORT_TYPE="FM-15", REM=METAR_CLEAR, VIS="016093,1,N,1")
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    got = (
        d["n_slots_pw"],
        d["n_slots_nil_metar"],
        d["n_slots_metar"],
        d["valid_day"],
        d["valid_day_explicit"],
        d["valid_day_vis5"],
        d["dusty_day"],
    )
    assert got == (8, 8, 8, True, False, True, False)


@pytest.mark.parametrize(
    "dust_vis,dusty,dusty_vis5,n_pw_vis5",
    [
        ("008000,1,N,1", True, False, 8),  # dust at 8 km: dusty, but not dust at <= 5 km
        ("003000,1,N,1", True, True, 8),  # dust at 3 km: both
        ("005000,1,N,1", True, True, 8),  # 5 000 m is included ("5 000 metres or less")
        ("999999,9,9,9", True, False, 7),  # dust, visibility unknown: slot left out of vis5
    ],
)
def test_dusty_day_vis5_counts_dust_with_visibility_at_or_below_5km(
    settings: Settings, dust_vis: str, dusty: bool, dusty_vis5: bool, n_pw_vis5: int
) -> None:
    rows = [
        isd_row(
            f"2024-05-01T{h:02d}:00:00",
            **LON0,
            MW1="07,1" if h == 0 else "02,1",
            VIS=dust_vis if h == 0 else "016000,1,N,1",
        )
        for h in range(0, 24, 3)
    ]
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    got = (d["dusty_day"], d["dusty_day_vis5"], d["n_slots_pw_vis5"], d["valid_day_vis5"])
    assert got == (dusty, dusty_vis5, n_pw_vis5, True)


def test_vicinity_dust_day_is_a_separate_flag(settings: Settings) -> None:
    rows = _slots(
        "2024-04-02",
        REPORT_TYPE="FM-15",
        REM="MET000METAR KLAS 020000Z 22016KT 10SM VCBLDU FEW250 17/M08 A2967",
        AU1="4,5,00,5,0,1,1",  # VC (in the vicinity) BL DU
        VIS="016093,1,N,1",
    )
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    assert (d["vicinity_dust_day"], d["dusty_day"], d["dusty_day_any"]) == (
        True,
        False,
        False,
    )


def test_remarks_dust_day_is_a_separate_flag(settings: Settings) -> None:
    rows = _slots(
        "2024-04-02",
        REPORT_TYPE="FM-15",
        REM="MET000METAR KELP 020000Z 25020G33KT 10SM FEW250 27/M20 A2986 RMK AO2 BLDU ALQDS",
        VIS="016093,1,N,1",
    )
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    got = (d["remarks_dust_day"], d["vicinity_dust_day"], d["dusty_day"], d["dusty_day_any"])
    assert got == (True, False, False, False)


# --- SYNOP and METAR as independent sources per slot (user decision 2026-10-20) --------------
# A slot is dusty if the selected SYNOP OR the selected METAR reports dust; it is observed if
# either observed the weather. Per-source flags keep the two apart. Wind, temperature and
# visibility means come from one primary report per slot (the SYNOP if it observed the weather).
US_SYNOP_NO_GROUP = (
    "SYN07472270 31956 22330 10222 21022 38651 49940 56042 92051="  # iX 1, no 7-group
)
SYNOP_NIL = "SYN08040829 12960 03406 10062 21002="  # iX 2: nothing significant
METAR_BLDU = "MET000METAR KELP 010251Z 25030G42KT 3SM BLDU CLR 27/M07 A2978 RMK AO2"
BLDU_AU = "0,5,00,5,0,1,1"  # AU: BL DU
METAR_NIL = "MET000METAR OIZB 010300Z 34012KT 9999 NSC 30/10 Q1010"


def _slot_day(day: str, at_03: list[dict]) -> list[dict]:
    """Seven quiet observed SYNOP slots plus the given reports around 03 UTC."""
    quiet = [isd_row(f"{day}T{h:02d}:00:00", **LON0, MW1="02,1") for h in range(0, 24, 3) if h != 3]
    return quiet + at_03


def test_metar_dust_is_not_hidden_by_an_unobserved_synop(settings: Settings) -> None:
    """El Paso case: the SYNOP says iX 1 but has no weather group; the METAR 9 min earlier
    reports blowing dust. Before 1.5.0 the SYNOP filled the slot and the dust was lost."""
    rows = _slot_day(
        "2024-06-01",
        [
            isd_row(
                "2024-06-01T02:51:00",
                **LON0,
                REPORT_TYPE="FM-15",
                REM=METAR_BLDU,
                AU1=BLDU_AU,
                VIS="004828,1,N,1",
            ),
            isd_row("2024-06-01T03:00:00", **LON0, REM=US_SYNOP_NO_GROUP),
        ],
    )
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    got = (
        d["dusty_day"],
        d["dusty_day_synop"],
        d["dusty_day_metar"],
        d["dusty_day_vis5"],
        d["n_slots_pw"],
        d["n_slots_pw_synop"],
        d["n_slots_pw_metar"],
    )
    assert got == (True, False, True, True, 8, 7, 1)


@pytest.mark.parametrize(
    "synop,metar,expected",
    [
        # SYNOP observed nothing significant, METAR reports dust -> dusty (either is enough)
        ({"REM": SYNOP_NIL}, {"REM": METAR_BLDU, "AU1": BLDU_AU}, (True, False, True)),
        # SYNOP reports dust, METAR has no weather group -> dusty
        ({"MW1": "07,1"}, {"REM": METAR_NIL}, (True, True, False)),
        # both clear -> not dusty
        ({"REM": SYNOP_NIL}, {"REM": METAR_NIL}, (False, False, False)),
    ],
)
def test_slot_is_dusty_if_either_source_reports_dust(
    settings: Settings, synop: dict, metar: dict, expected: tuple
) -> None:
    rows = _slot_day(
        "2024-06-02",
        [
            isd_row("2024-06-02T03:00:00", **LON0, **synop),
            isd_row("2024-06-02T02:51:00", **LON0, REPORT_TYPE="FM-15", **metar),
        ],
    )
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    assert (d["dusty_day"], d["dusty_day_synop"], d["dusty_day_metar"]) == expected
    assert (d["n_slots"], d["n_slots_synop"], d["n_slots_metar"]) == (8, 8, 1)


def test_speci_and_extra_metars_count_only_in_dusty_day_any(settings: Settings) -> None:
    """Only the closest observed METAR per slot is used; SPECI never fill a slot."""
    rows = _slot_day(
        "2024-06-03",
        [
            isd_row("2024-06-03T03:00:00", **LON0, REPORT_TYPE="FM-15", REM=METAR_NIL),
            isd_row(
                "2024-06-03T02:35:00", **LON0, REPORT_TYPE="FM-15", REM=METAR_BLDU, AU1=BLDU_AU
            ),
            isd_row(
                "2024-06-03T03:12:00",
                **LON0,
                REPORT_TYPE="FM-16",
                REM=METAR_BLDU.replace("METAR", "SPECI"),
                AU1=BLDU_AU,
            ),
        ],
    )
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    assert (d["dusty_day"], d["dusty_day_metar"], d["dusty_day_any"]) == (False, False, True)


def test_weather_means_come_from_the_primary_report(settings: Settings) -> None:
    """Wind is averaged over one report per slot (the observed SYNOP), never double counted."""
    rows = [
        isd_row(f"2024-06-04T{h:02d}:00:00", **LON0, MW1="02,1", WND="090,1,N,0100,1")
        for h in range(0, 24, 3)
    ] + [
        isd_row(
            "2024-06-04T02:51:00", **LON0, REPORT_TYPE="FM-15", REM=METAR_NIL, WND="090,1,N,0500,1"
        )
    ]
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    assert d["wsd_mean"] == pytest.approx(10.0)


def test_weather_means_come_from_the_synop_even_when_only_the_metar_saw_dust(
    settings: Settings,
) -> None:
    """User decision: SYNOP supplies the weather variables whenever the slot has one, so a
    station's series does not switch report types; the METAR still makes the slot dusty."""
    rows = [
        isd_row(f"2024-06-05T{h:02d}:00:00", **LON0, MW1="02,1", WND="090,1,N,0100,1")
        for h in range(0, 24, 3)
        if h != 3
    ] + [
        isd_row("2024-06-05T03:00:00", **LON0, REM=US_SYNOP_NO_GROUP, WND="090,1,N,0100,1"),
        isd_row(
            "2024-06-05T02:51:00",
            **LON0,
            REPORT_TYPE="FM-15",
            REM=METAR_BLDU,
            AU1=BLDU_AU,
            WND="090,1,N,0500,1",
        ),
    ]
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    assert d["dusty_day"] and d["wsd_mean"] == pytest.approx(10.0)


def test_visibility_comes_from_the_metar_when_the_synop_has_none(settings: Settings) -> None:
    """User decision (c): only visibility, and only when the slot's SYNOP has none."""
    rows = [
        isd_row(f"2024-06-06T{h:02d}:00:00", **LON0, MW1="02,1", VIS="016000,1,N,1")
        for h in range(0, 24, 3)
        if h != 3
    ] + [
        isd_row("2024-06-06T03:00:00", **LON0, MW1="02,1", VIS="999999,9,9,9"),
        isd_row(
            "2024-06-06T02:51:00", **LON0, REPORT_TYPE="FM-15", REM=METAR_NIL, VIS="005000,1,N,1"
        ),
    ]
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    assert (d["vis_min"], d["n_vis_from_metar"]) == (5000.0, 1)
