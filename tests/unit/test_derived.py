"""Derived SYNOPs (process/derived.py): is a station-year's SYNOP a copy of its METAR?

US SYNOPs are generated from the hourly METAR: temperature, wind and pressure equal the METAR
4-9 minutes earlier (notebooks 04 and 06). Independent reports agree only by chance.
"""

from datetime import datetime, timedelta

from dcdr.config import Settings
from dcdr.process.derived import synop_is_derived
from dcdr.process.parse import add_time_columns, parse_reports
from dcdr.process.qc import apply_qc
from tests.fixtures.isd import bronze_frame, isd_row


def _pairs(n: int, synop_tmp: str = "+0251,1", synop_wnd: str = "090,1,N,0050,1") -> list[dict]:
    t0, rows = datetime(2024, 1, 1), []
    for i in range(n):
        t = t0 + timedelta(hours=3 * i)
        rows.append(
            isd_row(
                (t - timedelta(minutes=9)).isoformat(),
                REPORT_TYPE="FM-15",
                TMP="+0251,1",
                WND="090,1,N,0050,1",
            )
        )
        rows.append(isd_row(t.isoformat(), TMP=synop_tmp, WND=synop_wnd))
    return rows


def _derived(settings: Settings, rows: list[dict]) -> bool:
    df = apply_qc(add_time_columns(parse_reports(bronze_frame(rows))), settings.qc, settings.parse)
    return synop_is_derived(df, settings.classify)


def test_copied_values_mean_derived(settings: Settings) -> None:
    assert _derived(settings, _pairs(120)) is True


def test_different_temperature_means_independent(settings: Settings) -> None:
    assert _derived(settings, _pairs(120, synop_tmp="+0263,1")) is False


def test_different_wind_means_independent(settings: Settings) -> None:
    assert _derived(settings, _pairs(120, synop_wnd="090,1,N,0062,1")) is False


def test_too_few_pairs_is_not_judged_derived(settings: Settings) -> None:
    assert _derived(settings, _pairs(50)) is False


def test_no_metar_means_not_derived(settings: Settings) -> None:
    rows = [r for r in _pairs(120) if r["REPORT_TYPE"] == "FM-12"]
    assert _derived(settings, rows) is False
