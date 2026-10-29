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


@pytest.mark.parametrize(
    "rem,expected",
    [
        (
            "SYN08040829 12960 03406 10062 21002=",
            "2",
        ),  # Zabol: iR=1 iX=2 (omitted, nothing significant)
        ("SYN06095322 47/// /2821 10282 7//44=", "7"),  # Rabbit Flat: automatic, tables 4680/4531
        ("SYN07040417 32660 63513 10212=", "2"),
        ("SYN07040829 /3960 03406=", "3"),  # iR missing ("/"), iX=3 (not observed)
        ("SYN004BUFR", None),  # received as BUFR: no text, no iX
        ("MET046METAR OIZB 010000Z 34012KT", None),  # METAR remark, not SYNOP
    ],
)
def test_synop_ix_is_read_from_the_raw_message(rem: str, expected: str | None) -> None:
    assert _one(REM=rem)["syn_ix"] == expected


def test_synop_ix_is_null_without_remarks() -> None:
    assert _one()["syn_ix"] is None


# --- METAR/SPECI text (user decision 2026-10-20; rules in docs/rules.md 5.2) ---------------
# Only the observed part counts: remarks (RMK ...), trend forecasts (NOSIG, BECMG, TEMPO ...) and
# recent weather (RE..) are not present weather. Strings follow real ISD REM values.
@pytest.mark.parametrize(
    "rem,auto,wx",
    [
        (
            "MET072METAR YBMA 082130Z AUTO 00000KT 9999 // SCT085 15/09 Q1017 RF00/0/000/0=",
            True,
            "missing",
        ),  # automatic, present weather not observable (WMO 15.8.19)
        ("MET060METAR YBDV 170900Z AUTO 12006KT //// // ////// 16/M02 Q1018=", True, "missing"),
        ("MET069METAR YPLM 270800Z AUTO 09009KT 9999 NCD 39/12 Q1010=", True, "none"),
        ("MET073SPECI YPLM 070800Z AUTO 24019KT 0000 DU BKN045 26/12 Q1010=", True, "group"),
        (
            "MET072METAR LMML 211215Z 35006KT 270V050 9999 FEW012 SCT020 23/15 Q1007 NOSIG=",
            False,
            "none",
        ),
        (
            "MET080METAR UATT 291630Z 13002MPS CAVOK 00/M04 Q1035 R13/0///60 NOSIG RMK QFE757/1009=",
            False,
            "cavok",
        ),
        (
            "MET070METAR KELP 011851Z 12012G27KT 3SM BLDU CLR 30/02 A3027 RMK AO2 SLP176",
            False,
            "group",
        ),
        (
            "MET060METAR KLAS 121256Z 22016G25KT 10SM VCBLDU FEW250 17/M08 A2967 RMK AO2",
            False,
            "group",
        ),  # dust in the vicinity is still a weather group
        (
            "MET070METAR DAUI 240235Z 05025KT 1000 -TSBLSA FEW040CB 25/07 Q1010",
            False,
            "group",
        ),  # malformed (two descriptors) but clearly weather
        (
            "MET060METAR OIZB 010300Z 34012KT 9999 NSC 30/10 Q1010 RESS",
            False,
            "none",
        ),  # recent weather (past hour) is not present weather
        (
            "MET060METAR OIZB 010300Z 34012KT 9999 NSC 30/10 Q1010 TEMPO 3000 DU",
            False,
            "none",
        ),  # a trend forecast is not an observation
        (
            "MET080METAR KELP 011851Z 25030G42KT 10SM CLR 27/M07 A2978 RMK AO2 BLDU ALQDS",
            False,
            "none",
        ),  # dust only in the remarks: not in the body
        ("MET040METAR COR OIZB 010300Z 34012KT 9999 FU 30/10 Q1010", False, "group"),
        (
            "MET10012/31/19 17:51:03 METAR KELP 010051Z 02009KT 10SM FEW220 09/M08 A3002 RMK AO2",
            False,
            "none",
        ),  # US format: local date and time before the message
        (
            "MET15801/02/20 07:58:02 SPECI KELP 021458Z 27017G28KT 10SM -RA FEW012 A2970 RMK AO2",
            False,
            "group",
        ),
        # 2000s: full text without the word METAR, ending in ";" (2026-10-27: 47 % of 2000s METARs)
        ("MET046LROP 282200Z VRB01MPS CAVOK 11/11 Q1018 NOSIG;", False, "cavok"),
        ("MET058LFMN 041030Z 20010KT 9999 FEW023 BKN100 14/08 Q1013 NOSIG;", False, "none"),
        (
            "MET074KPUB 291053Z AUTO 00000KT 10SM CLR M04/M06 A3038 RMK AO2 SLP304 T10391056;",
            True,
            "none",
        ),
        ("MET060LFML 232300Z 13018G28KT 3000 BLDU SCT020 17/11 Q1007;", False, "group"),
        # before 2000: only fragments ISD did not decode (no station, no time). Only CAVOK is read
        # from them (user decision 2026-10-28), and only before a trend word: "BECMG CAVOK" is a
        # forecast.
        ("MET012CAVOK NOSIG?", None, "cavok"),
        ("MET016NEW CAVOK NOSIG;", None, "cavok"),
        ("MET020NEW BECMG CAVOK;", None, None),
        ("MET038NEW RETS NOSIG WIND THR14 27013KT BLU;", None, None),
        ("MET006NOSIG?", None, None),
        ("SYN08040829 12960 03406 10062 21002=", None, None),  # SYNOP text
        ("SYN004BUFR", None, None),
    ],
)
def test_metar_text_is_read_from_the_raw_message(
    rem: str, auto: bool | None, wx: str | None
) -> None:
    r = _one(REM=rem)
    assert (r["metar_auto"], r["metar_wx"]) == (auto, wx)


# Dust named only in the remarks (US practice, FMH-1 2005: weather away from the station; Malta:
# DU with visibility above 5 km). ISD does not decode remarks (user decision 2026-10-29).
_KELP = "MET100KELP 011851Z 12012G27KT 10SM CLR 30/02 A3027 RMK AO2 "


@pytest.mark.parametrize(
    "rem,dust",
    [
        (_KELP + "PK WND 08037/1841 SLP176 BLDU ALQDS MTNS OBSCD SW T03000017 (DJD)", True),
        (_KELP + "SLP042 BLDU DSNT SW AND NW T02001056", True),
        (_KELP + "SLP038 OCNL VCBLDU VIRGA NE ACSL SW T01610022 $", True),
        (_KELP + "BLDUB1845E1915 SLP042", True),  # begin and end times
        ("MET060LMML 121650Z 15012KT 9999 FEW020 24/18 Q1012 NOSIG RMK DU;", True),
        (_KELP + "SLP176 DSNT LTG T03000017", False),  # DSNT is not DS
        (_KELP + "SLP176 T03000017", False),
        # a forecast inside the remarks is not an observation: cut at TEMPO, BECMG, NOSIG, FMhhmm
        (
            "MET060DRRN 121200Z 18010KT 9999 FEW020 38/05 Q1008 RMK CB SECTEUR S/SW TEMPO 1500 BLDU;",
            False,
        ),
        ("MET060YPDN 120300Z 33020KT 9999 NSC 35/10 Q1004 RMK BLDU TO N FM0400 9000 DU NSC=", True),
        (
            "MET060YPDN 120300Z 33020KT 9999 NSC 35/10 Q1004 RMK FM0400 33030G50KT 9000 DU NSC=",
            False,
        ),
        (_KELP + "SLP068 SHRA DSNT SS P0001", False),  # SS here is a direction, not sandstorm
        # bare SA: at Woomera always an observer's initials in a closure note (102 of 102)
        (
            "MET060YPWR 120600Z 18010KT 9999 FEW020 25/05 Q1012 RMK STN CLOSE REOPEN 1915Z OBS SA=",
            False,
        ),
        ("MET060LFML 232300Z 13018G28KT 3000 BLDU SCT020 17/11 Q1007;", False),  # body, no RMK
        ("MET012CAVOK NOSIG?", None),  # fragment: no full text
        ("SYN08040829 12960 03406 10062 21002=", None),
    ],
)
def test_dust_in_metar_remarks_is_flagged(rem: str, dust: bool | None) -> None:
    assert _one(REM=rem)["metar_rmk_dust"] is dust


def test_metar_text_is_null_without_remarks() -> None:
    r = _one()
    assert (r["metar_auto"], r["metar_wx"]) == (None, None)


@pytest.mark.parametrize(
    "rem,kind",
    [
        ("MET072METAR YBMA 082130Z AUTO 00000KT 9999 // SCT085 15/09 Q1017 RF00/0/000/0=", "full"),
        ("MET046LROP 282200Z VRB01MPS CAVOK 11/11 Q1018 NOSIG;", "full"),
        ("MET012CAVOK NOSIG?", "fragment"),
        ("MET006NOSIG?", "fragment"),
        ("SYN08040829 12960 03406 10062 21002=", None),
    ],
)
def test_metar_text_kind(rem: str, kind: str | None) -> None:
    assert _one(REM=rem)["metar_text"] == kind
