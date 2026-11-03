"""Build small ISD Global Hourly CSV payloads that look exactly like NOAA's files."""

from __future__ import annotations

import io
from pathlib import Path

import polars as pl

from dcdr.config import Settings

BASE_COLUMNS = [
    "STATION",
    "DATE",
    "SOURCE",
    "LATITUDE",
    "LONGITUDE",
    "ELEVATION",
    "NAME",
    "REPORT_TYPE",
    "CALL_SIGN",
    "QUALITY_CONTROL",
    "WND",
    "CIG",
    "VIS",
    "TMP",
    "DEW",
    "SLP",
]
DEFAULTS = {
    "STATION": "40582099999",
    "SOURCE": "4",
    "LATITUDE": "29.226567",
    "LONGITUDE": "47.968928",
    "ELEVATION": "62.78",
    "NAME": "KUWAIT INTL, KU",
    "REPORT_TYPE": "FM-12",
    "CALL_SIGN": "99999",
    "QUALITY_CONTROL": "V020",
    "WND": "090,1,N,0057,1",
    "CIG": "99999,9,9,N",
    "VIS": "010000,1,N,1",
    "TMP": "+0304,1",
    "DEW": "+0047,1",
    "SLP": "10058,1",
}


def isd_row(date: str, **fields: str) -> dict[str, str]:
    row = dict(DEFAULTS)
    row["DATE"] = date
    row.update(fields)
    return row


def make_isd_csv(rows: list[dict[str, str]]) -> bytes:
    """NOAA quotes every non-empty value and leaves empty values unquoted."""
    extra = sorted({k for r in rows for k in r} - set(BASE_COLUMNS))
    cols = BASE_COLUMNS + extra
    lines = [",".join(f'"{c}"' for c in cols)]
    for r in rows:
        lines.append(",".join(f'"{r[c]}"' if r.get(c, "") != "" else "" for c in cols))
    return ("\n".join(lines) + "\n").encode()


def bronze_frame(rows: list[dict[str, str]]) -> pl.DataFrame:
    return pl.read_csv(io.BytesIO(make_isd_csv(rows)), infer_schema_length=0)


ISD_HISTORY_COLUMNS = [
    "USAF",
    "WBAN",
    "STATION NAME",
    "CTRY",
    "STATE",
    "ICAO",
    "LAT",
    "LON",
    "ELEV(M)",
    "BEGIN",
    "END",
]


def write_isd_history(settings: Settings, rows: list[dict[str, str]] | None = None) -> Path:
    """Write a small isd-history.csv where the settings expect it. NOAA quotes every field,
    empty ones too (`""`); missing keys are written empty."""
    path = settings.isd_history_path
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(f'"{c}"' for c in ISD_HISTORY_COLUMNS)]
    for r in rows or []:
        lines.append(",".join(f'"{r.get(c, "")}"' for c in ISD_HISTORY_COLUMNS))
    path.write_text("\n".join(lines) + "\n")
    return path
