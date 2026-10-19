"""Decoding ISD packed fields (process/parse.py) and local solar time.

Uses real-looking ISD strings: values are scaled, missing codes become null, malformed text sets
`parse_status` bits, absent columns come out null, and LMST uses each report's own longitude.
"""

from datetime import date

import pytest

from dcdr.process.parse import add_time_columns, parse_reports
from tests.fixtures.isd import bronze_frame, isd_row


def _one(**fields: str) -> dict:
    return parse_reports(bronze_frame([isd_row("2024-07-22T21:00:00", **fields)])).row(
        0, named=True
    )


def test_core_values_are_decoded_and_scaled() -> None:
    r = _one(WND="270,5,N,0031,5", VIS="016093,1,9,9", TMP="+0417,1", DEW="-0022,2", SLP="10179,5")
    assert (r["wind_dir"], r["wsd"], r["wsd_qc"]) == (270.0, 3.1, "5")
    assert (r["vis_raw"], r["vis_qc"]) == (16093.0, "1")
    assert (r["t"], r["td"], r["slp"]) == (41.7, -2.2, 1017.9)
    assert r["parse_status"] == 0


def test_missing_sentinels_become_null() -> None:
    r = _one(WND="999,9,9,9999,9", VIS="999999,9,9,9", TMP="+9999,9", DEW="+9999,9", SLP="99999,9")
    assert [r[c] for c in ("wind_dir", "wsd", "vis_raw", "t", "td", "slp")] == [None] * 6
    assert r["parse_status"] == 0


def test_malformed_value_sets_parse_bit() -> None:
    r = _one(WND="090,1,N,AB12,1")
    assert r["wsd"] is None
    assert r["parse_status"] & 1 == 1


def test_report_type_is_stripped() -> None:
    assert _one(REPORT_TYPE="SOD  ")["report_type"] == "SOD"


def test_present_weather_components() -> None:
    r = _one(MW1="07,1", AU1="0,5,00,5,0,1,5", AW1="04,1", MV1="08,6", AY1="3,1,06,1")
    assert (r["mw1_code"], r["mw1_qc"]) == ("07", "1")
    assert (
        r["au1_int"],
        r["au1_desc"],
        r["au1_prec"],
        r["au1_obsc"],
        r["au1_other"],
        r["au1_qc"],
    ) == ("0", "5", "00", "5", "0", "5")
    assert (r["aw1_code"], r["mv1_code"], r["ay1_code"], r["ay1_period_h"]) == (
        "04",
        "08",
        "3",
        6.0,
    )


def test_gust_sources_are_decoded() -> None:
    r = _one(OC1="0206,1", OD1="4,99,0211,1,350")
    assert (r["oc1_spd"], r["od1_type"], r["od1_spd"], r["od1_qc"]) == (20.6, "4", 21.1, "1")


def test_absent_columns_are_null() -> None:
    r = _one()
    assert r["mw1_code"] is None and r["au9_obsc"] is None and r["aj1_cm"] is None


def test_lmst_uses_each_reports_longitude() -> None:
    raw = bronze_frame(
        [
            isd_row("2024-01-01T01:00:00", LONGITUDE="-115.16343"),
            isd_row("2024-06-01T01:00:00", LONGITUDE="0.0"),  # station moved mid-year
        ]
    )
    df = add_time_columns(parse_reports(raw))
    first, second = df.row(0, named=True), df.row(1, named=True)
    assert first["lmst_date"] == date(2023, 12, 31)
    assert first["lmst_hour"] == pytest.approx(17.3222, abs=1e-3)
    assert second["lmst_date"] == date(2024, 6, 1) and second["lmst_hour"] == pytest.approx(1.0)


def test_daily_and_past_weather_fields() -> None:
    # Layouts per ISD format document and real SOD rows ("AU,16,RA  ,5"; "08,6,24,6").
    r = _one(REPORT_TYPE="SOD  ", AT1="AU,07,DU  ,5", AX1="07,5,24,5", AZ1="2,1,06,1")
    assert (r["at1_src"], r["at1_wea"], r["at1_qc"]) == ("AU", "07", "5")
    assert (r["ax1_code"], r["ax1_qc"], r["ax1_period_h"]) == ("07", "5", 24.0)
    assert (r["az1_code"], r["az1_qc"], r["az1_period_h"]) == ("2", "1", 6.0)
