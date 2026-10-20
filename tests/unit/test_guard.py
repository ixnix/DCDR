"""Station guard for inferred "nothing significant" (process/guard.py; rule V3).

The guard asks: at this station, when a SYNOP leaves out the weather group but reports visibility
and carries iX, how often is it "not observed" (iX 3/6)? Rarely -> inference allowed.
"""

from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from dcdr.config import Settings
from dcdr.process.guard import guard_counts, nil_guard, station_guard
from tests.fixtures.isd import bronze_frame, isd_row

IX2 = "SYN08040829 12960 03406 10062="  # omitted: nothing significant
IX3 = "SYN07040829 13960 03406 10062="  # omitted: not observed


def _rows(n: int, start: str = "2024-01-01T00:00:00", **fields: str) -> list[dict]:
    t0 = datetime.fromisoformat(start)
    return [isd_row((t0 + timedelta(hours=3 * i)).isoformat(), **fields) for i in range(n)]


def test_counts_only_omitted_groups_with_visibility_and_known_ix(settings: Settings) -> None:
    rows = (
        _rows(5, REM=IX2)
        + _rows(2, "2024-02-01T00:00:00", REM=IX3)
        + _rows(3, "2024-03-01T00:00:00", REM=IX2, MW1="06,1")  # a code was reported: not counted
        + _rows(4, "2024-04-01T00:00:00", REM=IX2, VIS="999999,9,9,9")  # no visibility
        + _rows(6, "2024-05-01T00:00:00", REM="SYN004BUFR")  # no iX
        + _rows(2, "2024-06-01T00:00:00", REM=IX2, REPORT_TYPE="FM-15")  # not a SYNOP
    )
    assert guard_counts(bronze_frame(rows), settings) == (7, 2)


def test_guard_passes_fails_or_abstains(settings: Settings) -> None:
    cfg = settings.classify  # defaults: max share 0.02, min 50 reports
    assert nil_guard(100, 1, cfg) is True
    assert nil_guard(100, 2, cfg) is False  # share 0.02 is not below the maximum
    assert nil_guard(49, 0, cfg) is None  # too little iX evidence to judge
    assert nil_guard(0, 0, cfg) is None


def test_station_guard_reads_all_years_of_a_station(settings: Settings, tmp_path: Path) -> None:
    y2023 = bronze_frame(_rows(30, "2023-01-01T00:00:00", REM=IX2))
    y2024 = bronze_frame(_rows(30, "2024-01-01T00:00:00", REM=IX2))
    old = bronze_frame(_rows(30, "1995-01-01T00:00:00"))  # no REM column at all
    paths = []
    for year, df in (("1995", old), ("2023", y2023), ("2024", y2024)):
        p = tmp_path / f"{year}.parquet"
        df.write_parquet(p)
        paths.append(p)
    assert station_guard(paths, settings) is True  # 60 reports with iX 2: passes
    assert station_guard(paths[:2], settings) is None  # 30 reports: not enough evidence
    assert station_guard([], settings) is None


def test_station_guard_fails_for_ix3_with_visibility_habit(
    settings: Settings, tmp_path: Path
) -> None:
    df = bronze_frame(_rows(40, REM=IX2) + _rows(20, "2024-06-01T00:00:00", REM=IX3))
    p = tmp_path / "x.parquet"
    df.write_parquet(p)
    assert station_guard([p], settings) is False
    assert pl.read_parquet(p).height == 60
