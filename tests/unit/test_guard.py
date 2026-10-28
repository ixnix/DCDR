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


# --- METAR guard (user decision 2026-10-28; notebook 06) -------------------------------------
# For METARs without full text, "no code + visibility > 5 km = nothing significant" is inferred
# only at stations whose METARs WITH full text show that such reports are rarely "not observed"
# (mostly AUTO, which cannot be seen without text).
from dcdr.process.guard import metar_guard, metar_guard_counts, station_guards  # noqa: E402

CLEAR = "MET000METAR OIZB 010300Z 34012KT 9999 NSC 30/10 Q1010"  # no group, nothing significant
AUTO = "MET000METAR YPLM 270800Z AUTO 09009KT 9999 NCD 39/12 Q1010"  # automatic: not observed


def test_metar_counts_use_full_text_no_code_and_good_visibility(settings: Settings) -> None:
    m = {"REPORT_TYPE": "FM-15"}
    rows = (
        _rows(5, REM=CLEAR, **m)
        + _rows(2, "2024-02-01T00:00:00", REM=AUTO, **m)
        + _rows(3, "2024-03-01T00:00:00", REM=CLEAR, VIS="004000,1,N,1", **m)  # not > 5 km
        + _rows(4, "2024-04-01T00:00:00", REM="MET006NOSIG?", **m)  # fragment: no truth
        + _rows(2, "2024-05-01T00:00:00", REM=CLEAR, AU1="0,5,00,5,0,1,1", **m)  # coded
        + _rows(3, "2024-06-01T00:00:00", REM=IX2)  # a SYNOP
    )
    assert metar_guard_counts(bronze_frame(rows), settings) == (7, 2)


def test_metar_guard_passes_fails_or_abstains(settings: Settings) -> None:
    cfg = settings.classify
    assert metar_guard(100, 1, cfg) is True
    assert metar_guard(100, 2, cfg) is False
    assert metar_guard(49, 0, cfg) is None


def test_station_guards_returns_both_guards(settings: Settings, tmp_path: Path) -> None:
    df = bronze_frame(
        _rows(60, REM=IX2) + _rows(60, "2024-06-01T00:00:00", REM=CLEAR, REPORT_TYPE="FM-15")
    )
    p = tmp_path / "x.parquet"
    df.write_parquet(p)
    assert station_guards([p], settings) == (True, True)
    assert station_guards([], settings) == (None, None)
