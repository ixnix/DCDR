"""Synoptic sampling (process/sampling.py): one report per 3-hourly slot.

Checks slot assignment and the +-30 min window, report-type priority before time distance, the
weather-code tie-break, excluded summary types, and that row order never changes the result.
"""

import random
from datetime import datetime

import polars as pl

from dcdr.config import Settings
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
    return sorted(df.filter(pl.col("is_synoptic_selected"))["row_key"].to_list())


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


def test_priority_beats_distance(settings: Settings) -> None:
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


def test_present_weather_breaks_equal_priority(settings: Settings) -> None:
    out = select_synoptic(
        _frame(
            [
                ("2024-01-01T03:00:00", "FM-15", False, "nopw"),
                ("2024-01-01T03:10:00", "FM-15", True, "pw"),
            ]
        ),
        settings.sampling,
    )
    assert _selected(out) == ["pw"]


def test_excluded_types_never_selected(settings: Settings) -> None:
    out = select_synoptic(_frame([("2024-01-01T03:00:00", "SOD", True, "sod")]), settings.sampling)
    row = out.row(0, named=True)
    assert row["is_excluded_type"] and not row["is_synoptic_selected"]


def test_selection_is_independent_of_row_order(settings: Settings) -> None:
    rows = [
        (f"2024-01-01T{h:02d}:{m:02d}:00", rt, pw, f"k{h}{m}{rt}")
        for h in range(0, 24)
        for m in (0, 10, 50)
        for rt, pw in (("FM-15", True), ("FM-16", False))
    ]
    base = _selected(select_synoptic(_frame(rows), settings.sampling))
    for seed in range(3):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        assert _selected(select_synoptic(_frame(shuffled), settings.sampling)) == base
