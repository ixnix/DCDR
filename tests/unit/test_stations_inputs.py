"""Station-position inputs (stations.py): the saved isd-history (positions, placeholders,
duplicates, a missing file) and the hand-edited override file (validation)."""

from pathlib import Path

import pytest

from dcdr.config import Settings
from dcdr.stations import (
    MissingIsdHistoryError,
    OverrideError,
    file_md5,
    load_isd_history,
    load_overrides,
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
