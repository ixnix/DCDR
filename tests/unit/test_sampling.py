"""Synoptic sampling (process/sampling.py): per 3-hourly slot, one report per source plus a primary.

Checks slot assignment and the +-30 min window; that each source (SYNOP, METAR) gets its own
selected report (the closest one that observed the weather), so an unobserved SYNOP cannot hide a
METAR's dust; that the primary report (weather variables) is the SYNOP whenever the slot has one
and the METAR otherwise (user decisions 2026-10-20); that SPECI and summaries never fill a slot;
and that row order never changes the result.
"""

import random
from datetime import datetime, timedelta

import polars as pl

from dcdr.config import Settings, load_settings
from dcdr.process.sampling import select_synoptic


def _frame(rows: list[tuple[str, str, bool, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_id": ["S"] * len(rows),
            "time_utc": [datetime.fromisoformat(t) for t, _, _, _ in rows],
            "report_type": [rt for _, rt, _, _ in rows],
            "has_pw": [pw for _, _, pw, _ in rows],
            "source": ["4"] * len(rows),
            "row_key": [k for _, _, _, k in rows],
            "lon_used": [0.0] * len(rows),
        }
    )


def _selected(df: pl.DataFrame) -> list[str]:
    """The slot's primary report(s)."""
    return sorted(df.filter(pl.col("is_synoptic_selected"))["row_key"].to_list())


def _by_source(df: pl.DataFrame) -> dict[str, str]:
    """The selected report per source: {"synop": row_key, "metar": row_key}."""
    sel = df.filter(pl.col("is_source_selected"))
    return dict(zip(sel["slot_source"].to_list(), sel["row_key"].to_list(), strict=True))


def test_slot_and_window(settings: Settings) -> None:
    out = select_synoptic(
        _frame(
            [
                ("2024-01-01T02:40:00", "FM-15", True, "a"),
                ("2024-01-01T01:20:00", "FM-15", True, "b"),
            ]
        ),
        settings.sampling,
    )
    rows = {r["row_key"]: r for r in out.iter_rows(named=True)}
    assert rows["a"]["slot_time"] == datetime(2024, 1, 1, 3) and rows["a"]["is_candidate"]
    assert rows["b"]["is_candidate"] is False and rows["b"]["selection_rank"] is None


def test_observed_synop_is_primary_over_a_closer_metar(settings: Settings) -> None:
    out = select_synoptic(
        _frame(
            [
                ("2024-01-01T03:00:00", "FM-15", True, "metar"),
                ("2024-01-01T03:20:00", "FM-12", True, "synop"),
            ]
        ),
        settings.sampling,
    )
    assert _selected(out) == ["synop"]
    assert _by_source(out) == {"synop": "synop", "metar": "metar"}  # both sources keep a report


def test_synop_is_primary_whenever_present_even_if_unobserved(settings: Settings) -> None:
    """User decision: weather variables come from the SYNOP whenever the slot has one, to avoid
    mixing report types; the METAR still counts for dust (in gold)."""
    out = select_synoptic(
        _frame(
            [
                ("2024-01-01T02:51:00", "FM-15", True, "metar"),
                ("2024-01-01T03:00:00", "FM-12", False, "synop"),
            ]
        ),
        settings.sampling,
    )
    assert _selected(out) == ["synop"]
    assert _by_source(out) == {"synop": "synop", "metar": "metar"}


def test_metar_is_primary_when_the_slot_has_no_synop(settings: Settings) -> None:
    out = select_synoptic(
        _frame([("2024-01-01T02:51:00", "FM-15", True, "metar")]), settings.sampling
    )
    assert _selected(out) == ["metar"]


def test_within_a_source_observed_beats_closer(settings: Settings) -> None:
    out = select_synoptic(
        _frame(
            [
                ("2024-01-01T03:00:00", "FM-15", False, "nopw"),
                ("2024-01-01T03:10:00", "FM-15", True, "pw"),
            ]
        ),
        settings.sampling,
    )
    assert _selected(out) == ["pw"] and _by_source(out) == {"metar": "pw"}


def test_within_a_source_the_closest_observed_report_is_the_only_one(settings: Settings) -> None:
    """One METAR per slot, however many the airport sends (keeps busy airports comparable)."""
    out = select_synoptic(
        _frame(
            [
                ("2024-01-01T02:30:00", "FM-15", True, "early"),
                ("2024-01-01T03:00:00", "FM-15", True, "on_time"),
                ("2024-01-01T03:30:00", "FM-15", True, "late"),
            ]
        ),
        settings.sampling,
    )
    assert _by_source(out) == {"metar": "on_time"}


def test_speci_never_fills_a_slot(settings: Settings) -> None:
    """SPECI are sent because conditions changed (often when dust arrives): event-driven."""
    out = select_synoptic(
        _frame([("2024-01-01T03:00:00", "FM-16", True, "speci")]), settings.sampling
    )
    row = out.row(0, named=True)
    assert (row["is_candidate"], row["slot_source"], row["is_source_selected"]) == (
        False,
        None,
        False,
    )


def test_excluded_types_never_selected(settings: Settings) -> None:
    out = select_synoptic(_frame([("2024-01-01T03:00:00", "SOD", True, "sod")]), settings.sampling)
    row = out.row(0, named=True)
    assert row["is_excluded_type"] and not row["is_synoptic_selected"]
    assert not row["is_source_selected"]


def test_selection_is_independent_of_row_order(settings: Settings) -> None:
    rows = [
        (f"2024-01-01T{h:02d}:{m:02d}:00", rt, pw, f"k{h}{m}{rt}")
        for h in range(0, 24)
        for m in (0, 10, 50)
        for rt, pw in (("FM-15", True), ("FM-12", False), ("FM-16", False))
    ]

    def picks(df: pl.DataFrame) -> tuple[list[str], list[str]]:
        return _selected(df), sorted(df.filter(pl.col("is_source_selected"))["row_key"].to_list())

    base = picks(select_synoptic(_frame(rows), settings.sampling))
    for seed in range(3):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        assert picks(select_synoptic(_frame(shuffled), settings.sampling)) == base


# --- Off-grid SYNOP schedules (method 1.10.0, user decision 2026-10-29) ----------------------
# Australian SYNOPs are often made on a 3-hourly schedule one hour off the synoptic hours (e.g.
# 01, 04, ..., 22 UTC in Western Australia; 02, 05, ..., 23 UTC further east), by local clock time,
# so the offset changes with daylight saving time. Where a station-month's observed SYNOPs keep
# such a schedule, its slot grid moves with them.


def _schedule(hours: range, rt: str = "FM-12", pw: bool = True, days: int = 60, tag: str = "s"):
    """Reports at the given UTC hours (minute 0) on `days` consecutive days."""
    start = datetime(2024, 1, 1)
    return [
        ((start + timedelta(days=d, hours=h)).isoformat(), rt, pw, f"{tag}{d}-{h}")
        for d in range(days)
        for h in hours
    ]


def test_a_schedule_one_hour_after_the_grid_moves_the_slots(settings: Settings) -> None:
    out = select_synoptic(_frame(_schedule(range(1, 24, 3))), settings.sampling)
    assert out["slot_offset_min"].unique().to_list() == [60]
    assert out["is_source_selected"].all()  # every report fills its own slot
    assert set(out["slot_time"].dt.hour().unique()) == set(range(1, 24, 3))


def test_a_schedule_one_hour_before_the_grid_moves_the_slots(settings: Settings) -> None:
    out = select_synoptic(_frame(_schedule(range(2, 24, 3))), settings.sampling)
    assert out["slot_offset_min"].unique().to_list() == [-60]
    assert out["is_source_selected"].all()


def test_unobserved_on_grid_synops_do_not_hide_the_schedule(settings: Settings) -> None:
    # Since 2000 Australian stations also send automatic on-grid SYNOPs that rarely observe the
    # weather (iX 6): the schedule is read from the reports that observed it.
    rows = _schedule(range(2, 24, 3), tag="obs") + _schedule(range(0, 24, 3), pw=False, tag="auto")
    out = select_synoptic(_frame(rows), settings.sampling)
    assert out["slot_offset_min"].unique().to_list() == [-60]
    sel = out.filter(pl.col("is_source_selected"))["row_key"]
    assert sel.str.starts_with("obs").all() and sel.len() == 60 * 8


def test_a_station_on_the_grid_is_not_shifted(settings: Settings) -> None:
    out = select_synoptic(_frame(_schedule(range(0, 24, 3))), settings.sampling)
    assert out["slot_offset_min"].unique().to_list() == [0]
    assert set(out["slot_time"].dt.hour().unique()) == set(range(0, 24, 3))


def test_a_mixed_schedule_is_not_shifted(settings: Settings) -> None:
    # half on the grid, half one hour after: no single schedule, so the UTC grid stays
    rows = _schedule(range(0, 24, 6), tag="a") + _schedule(range(4, 24, 6), tag="b")
    out = select_synoptic(_frame(rows), settings.sampling)
    assert out["slot_offset_min"].unique().to_list() == [0]


def test_too_few_observed_synops_are_not_shifted(settings: Settings) -> None:
    out = select_synoptic(_frame(_schedule(range(1, 24, 12), days=10)), settings.sampling)  # 20
    assert out["slot_offset_min"].unique().to_list() == [0]


def test_the_offset_is_found_per_month(settings: Settings) -> None:
    # Woomera 2005: +60 in summer (daylight saving), -60 in winter; Meekatharra 1975: on the
    # grid in January, +60 from February.
    jan = [r for r in _schedule(range(1, 24, 3), days=31, tag="jan")]
    feb = [
        (
            (datetime(2024, 2, 1) + timedelta(days=d, hours=h)).isoformat(),
            "FM-12",
            True,
            f"f{d}-{h}",
        )
        for d in range(29)
        for h in range(2, 24, 3)
    ]
    out = select_synoptic(_frame(jan + feb), settings.sampling)
    by_month = out.group_by(pl.col("time_utc").dt.month()).agg(pl.col("slot_offset_min").unique())
    assert dict(by_month.iter_rows()) == {1: [60], 2: [-60]}
    assert out["is_source_selected"].all()


def test_the_shift_can_be_switched_off() -> None:
    s = load_settings(overrides={"sampling": {"schedule_shift": False}})
    out = select_synoptic(_frame(_schedule(range(1, 24, 3))), s.sampling)
    assert out["slot_offset_min"].unique().to_list() == [0]
    assert not out["is_candidate"].any()


def test_metars_fill_the_shifted_slots(settings: Settings) -> None:
    rows = _schedule(range(1, 24, 3), tag="syn") + [
        ("2024-01-01T01:00:00", "FM-15", True, "met0100"),
        ("2024-01-01T00:00:00", "FM-15", True, "met0000"),  # 60 min from the 01:00 slot
    ]
    out = select_synoptic(_frame(rows), settings.sampling)
    rows_ = {r["row_key"]: r for r in out.iter_rows(named=True)}
    assert rows_["met0100"]["is_source_selected"] and rows_["met0100"]["slot_source"] == "metar"
    assert not rows_["met0000"]["is_candidate"]


def test_aust_reports_are_a_synop_source(settings: Settings) -> None:
    # "AUST" = Dataset from Australia (ISD format document p. 6), 1945-1984
    out = select_synoptic(_frame([("2024-01-01T03:00:00", "AUST", True, "a")]), settings.sampling)
    assert out.row(0, named=True)["slot_source"] == "synop"
    assert out.row(0, named=True)["is_source_selected"]
