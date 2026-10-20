"""Synoptic sampling (process/sampling.py): per 3-hourly slot, one report per source plus a primary.

Checks slot assignment and the +-30 min window; that each source (SYNOP, METAR) gets its own
selected report (the closest one that observed the weather); that the primary report is the SYNOP
when it observed the weather and the METAR when the SYNOP did not (user decision 2026-10-20: an
unobserved SYNOP must not hide a METAR); that SPECI and summaries never fill a slot; and that row
order never changes the result.
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


def test_unobserved_synop_yields_the_primary_to_an_observed_metar(settings: Settings) -> None:
    """E.g. US SYNOPs with iX 1 but no weather group, while the METAR reports dust."""
    out = select_synoptic(
        _frame(
            [
                ("2024-01-01T02:51:00", "FM-15", True, "metar"),
                ("2024-01-01T03:00:00", "FM-12", False, "synop"),
            ]
        ),
        settings.sampling,
    )
    assert _selected(out) == ["metar"]
    assert _by_source(out) == {"synop": "synop", "metar": "metar"}


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
