"""Station-position inputs (stations.py): the saved isd-history (positions, placeholders,
duplicates, a missing file) and the hand-edited override file (validation)."""

from pathlib import Path

import polars as pl
import pytest

from dcdr.config import Settings
from dcdr.stations import (
    MissingIsdHistoryError,
    NoteError,
    OverrideError,
    file_md5,
    load_isd_history,
    load_overrides,
    load_period_notes,
    note_texts,
)
from tests.fixtures.isd import write_isd_history

OVERRIDE_HEADER = "station_id,lat,lon,action,reason,decided\n"


def _overrides(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "position_overrides.csv"
    path.write_text(OVERRIDE_HEADER + "".join(r + "\n" for r in rows))
    return path


def test_isd_history_positions(settings: Settings) -> None:
    path = write_isd_history(
        settings,
        [
            {
                "USAF": "405820",
                "WBAN": "99999",
                "STATION NAME": "KUWAIT INTL",
                "LAT": "+29.227",
                "LON": "+047.969",
            },
            {"USAF": "007018", "WBAN": "99999", "STATION NAME": "NO POSITION"},
            {
                "USAF": "007026",
                "WBAN": "99999",
                "STATION NAME": "WXPOD 7026",
                "LAT": "+00.000",
                "LON": "+000.000",
            },
        ],
    )
    h = load_isd_history(path)
    assert h["station_id"].to_list() == ["40582099999", "00701899999", "00702699999"]
    assert list(zip(h["isd_lat"], h["isd_lon"], strict=True)) == [
        (29.227, 47.969),
        (None, None),
        (None, None),  # 0/0 is a placeholder, never used for verifying
    ]
    assert h["isd_name"].to_list()[0] == "KUWAIT INTL"


def test_missing_isd_history_is_fatal(tmp_path: Path) -> None:
    path = tmp_path / "catalog" / "isd-history.csv"
    with pytest.raises(MissingIsdHistoryError, match="isd-history.csv"):
        load_isd_history(path)


def test_duplicate_isd_history_id_is_fatal(settings: Settings) -> None:
    row = {"USAF": "405820", "WBAN": "99999", "LAT": "+29.227", "LON": "+047.969"}
    path = write_isd_history(settings, [row, row])
    with pytest.raises(ValueError, match="40582099999"):
        load_isd_history(path)


def test_missing_override_file_means_no_overrides(tmp_path: Path) -> None:
    o = load_overrides(tmp_path / "absent.csv")
    assert o.height == 0
    assert o.columns == ["station_id", "lat", "lon", "action", "reason", "decided"]
    assert file_md5(tmp_path / "absent.csv") == ""


@pytest.mark.parametrize(
    "rows",
    [
        ["94999900108,-34.93,138.58,use,,2026-11-03"],  # no reason
        [
            "94999900108,-34.93,138.58,use,Adelaide,2026-11-03",
            "94999900108,,,exclude,again,2026-11-03",
        ],  # duplicate station
        ["94999900108,,138.58,use,Adelaide,2026-11-03"],  # use without a latitude
        ["94999900108,340.1,138.58,use,typo,2026-11-03"],  # latitude out of range
        ["94999900108,-34.93,138.58,move,Adelaide,2026-11-03"],  # unknown action
    ],
)
def test_override_validation(tmp_path: Path, rows: list[str]) -> None:
    with pytest.raises(OverrideError, match="94999900108"):
        load_overrides(_overrides(tmp_path, *rows))


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        (",,,exclude,blank id,2026-11-03", "no station_id"),
        ("4058209999,,,exclude,leading zero lost,2026-11-03", "11 digits"),
    ],
)
def test_override_station_id_must_be_11_digits(tmp_path: Path, row: str, problem: str) -> None:
    """Review finding: a row without a usable station ID must not vanish silently (e.g. an ID
    whose leading zero a spreadsheet dropped)."""
    with pytest.raises(OverrideError, match=problem):
        load_overrides(_overrides(tmp_path, row))


NOTES_HEADER = "station_id,from_year,to_year,note,decided\n"


def _notes(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "period_notes.csv"
    path.write_text(NOTES_HEADER + "".join(r + "\n" for r in rows))
    return path


def test_missing_period_notes_file_means_no_notes(tmp_path: Path) -> None:
    n = load_period_notes(tmp_path / "absent.csv")
    assert n.height == 0
    assert n.columns == ["station_id", "from_year", "to_year", "note", "decided"]


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        ("47106099999,1957,1970,,2026-11-06", "no note"),
        ("4710609999,1957,1970,Red Cloud,2026-11-06", "11 digits"),
        ("47106099999,1970,1957,Red Cloud,2026-11-06", "from_year after to_year"),
        ("47106099999,57,1970,Red Cloud,2026-11-06", "years must be 1800-2100"),
        ("47106099999,1957,,Red Cloud,2026-11-06", "years must be 1800-2100"),
        # "; " separates the notes of a row in gold, so a note must not contain ";"
        ("47106099999,1957,1970,Red Cloud; 187 km,2026-11-06", "contains ;"),
    ],
)
def test_period_note_validation(tmp_path: Path, row: str, problem: str) -> None:
    with pytest.raises(NoteError, match=problem):
        load_period_notes(_notes(tmp_path, row))


def test_period_notes_load(tmp_path: Path) -> None:
    n = load_period_notes(_notes(tmp_path, "47106099999,1957,1970,Camp Red Cloud,2026-11-06"))
    assert n.row(0) == ("47106099999", 1957, 1970, "Camp Red Cloud", "2026-11-06")


def test_note_texts_by_year_decade_and_station() -> None:
    """A note is labelled with its years; several notes of a station join in year order. Decades
    start in a year ending in 1 (decadal.sql); a decade gets every note that overlaps it."""
    notes = pl.DataFrame(
        {
            "station_id": ["X", "X", "Y"],
            "from_year": [1974, 1957, 2000],
            "to_year": [1974, 1970, 2001],
            "note": ["B", "A", "C"],
            "decided": ["d"] * 3,
        }
    )
    by_year, by_decade, by_station = note_texts(notes)
    years = dict(
        zip(zip(by_year["station_id"], by_year["year"], strict=True), by_year["note"], strict=True)
    )
    assert len(years) == 14 + 1 + 2
    assert years[("X", 1957)] == years[("X", 1970)] == "1957-1970: A"
    assert years[("X", 1974)] == "1974: B"
    assert ("X", 1971) not in years
    decades = dict(
        zip(
            zip(by_decade["station_id"], by_decade["decade"], strict=True),
            by_decade["note"],
            strict=True,
        )
    )
    assert decades == {
        ("X", 1951): "1957-1970: A",
        ("X", 1961): "1957-1970: A",
        ("X", 1971): "1974: B",  # 1970 belongs to the decade 1961-1970
        ("Y", 1991): "2000-2001: C",
        ("Y", 2001): "2000-2001: C",
    }
    assert dict(zip(by_station["station_id"], by_station["notes"], strict=True)) == {
        "X": "1957-1970: A; 1974: B",
        "Y": "2000-2001: C",
    }


def test_valid_overrides_load(tmp_path: Path) -> None:
    path = _overrides(
        tmp_path,
        "94999900108,-34.93,138.58,use,Adelaide (isd-history and data agree),2026-11-03",
        "94999900100,,,exclude,position unknown,2026-11-03",
    )
    o = load_overrides(path)
    assert o["lat"].to_list() == [-34.93, None]
    assert o["action"].to_list() == ["use", "exclude"]
    assert len(file_md5(path)) == 32
