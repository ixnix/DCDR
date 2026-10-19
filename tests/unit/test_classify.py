"""Dust classification (process/classify.py): weather codes -> dust class per report.

One test case per row of each code table (MW, AW, AU), plus the special rules: AW fog codes are
never dust, "snow or sand" codes resolved by temperature, dust in the vicinity is evidence only,
past-weather and daily-summary evidence, precedence between fields, QC rejection, and that a
report without any weather field is "unknown" (not observed), never "none".
"""

import pytest

from dcdr.config import Settings, load_settings
from dcdr.process.classify import classify_reports
from dcdr.process.parse import add_time_columns, parse_reports
from dcdr.process.qc import apply_qc
from tests.fixtures.isd import bronze_frame, isd_row


def _cls(settings: Settings, **fields: str) -> dict:
    df = add_time_columns(parse_reports(bronze_frame([isd_row("2024-07-22T21:00:00", **fields)])))
    df = apply_qc(df, settings.qc, settings.parse)
    return classify_reports(df, settings.classify, settings.qc).row(0, named=True)


TABLE = [
    ("MW1", "05,1", "haze"),
    ("MW1", "06,1", "suspended"),
    ("MW1", "07,1", "blowing"),
    ("MW1", "08,1", "whirls"),
    ("MW1", "09,1", "storm"),
    ("MW1", "30,1", "storm"),
    ("MW1", "31,1", "storm"),
    ("MW1", "32,1", "storm"),
    ("MW1", "33,1", "storm_severe"),
    ("MW1", "34,1", "storm_severe"),
    ("MW1", "35,1", "storm_severe"),
    ("MW1", "98,1", "storm_thunder"),
    ("MW1", "04,1", "none"),
    ("AW1", "04,1", "haze_or_suspended"),
    ("AW1", "07,1", "blowing"),
    ("AW1", "05,1", "none"),
    ("AW1", "30,1", "none"),
    ("AW1", "31,1", "none"),
    ("AW1", "32,1", "none"),
    ("AW1", "33,1", "none"),
    ("AW1", "34,1", "none"),
    ("AW1", "35,1", "none"),
    ("AU1", "0,0,00,0,4,1,1", "storm"),
    ("AU1", "0,0,00,0,5,1,1", "storm"),
    ("AU1", "3,0,00,0,5,1,1", "storm_severe"),
    ("AU1", "0,7,00,0,5,1,1", "storm_thunder"),
    ("AU1", "0,0,00,0,1,1,1", "whirls"),
    ("AU1", "0,5,00,5,0,1,1", "blowing"),
    ("AU1", "0,4,00,6,0,1,1", "blowing"),
    ("AU1", "0,0,00,5,0,1,1", "suspended"),
    ("AU1", "0,0,00,6,0,1,1", "suspended"),
    ("AU1", "0,0,00,7,0,1,1", "haze"),
]


@pytest.mark.parametrize("field,value,expected", TABLE)
def test_code_table_row(settings: Settings, field: str, value: str, expected: str) -> None:
    assert _cls(settings, **{field: value})["dust_class_rule"] == expected


def test_aw_fog_codes_are_never_dust(settings: Settings) -> None:
    for code in ("30", "31", "32", "33", "34", "35"):
        r = _cls(settings, AW1=f"{code},1")
        assert r["flag_du"] is False and r["flag_ds"] is False


@pytest.mark.parametrize(
    "tmp,extra,cls,ambiguous",
    [
        ("+0304,1", {}, "blowing", False),
        ("-0050,1", {}, "none", False),
        ("+0015,1", {}, "none", True),
        ("+0050,1", {"MW1": "71,1"}, "none", True),
    ],
)
def test_aw_blowing_snow_or_sand_resolved_by_temperature(
    settings: Settings, tmp: str, extra: dict, cls: str, ambiguous: bool
) -> None:
    r = _cls(settings, AW1="27,1", TMP=tmp, DEW="-0100,1", **extra)
    assert (r["dust_class_rule"], r["flag_ambiguous"]) == (cls, ambiguous)


def test_vicinity_is_evidence_not_station_dust(settings: Settings) -> None:
    r = _cls(settings, AU1="4,5,00,5,0,1,1")
    assert (r["dust_class_rule"], r["flag_vicinity"], r["flag_du"]) == ("none", True, False)
    r = _cls(settings, MW1="02,1", MV1="08,1")
    assert (r["dust_class_rule"], r["flag_vicinity"]) == ("none", True)


def test_past_weather_sandstorm_is_evidence(settings: Settings) -> None:
    r = _cls(settings, MW1="02,1", AY1="3,1,06,1")
    assert (r["flag_past"], r["flag_du"]) == (True, False)


def test_precedence_and_rule_id(settings: Settings) -> None:
    r = _cls(settings, MW1="06,1", AW1="07,1")
    assert (r["dust_class_rule"], r["dust_rule_id"]) == ("blowing", "AW:blowing")
    r = _cls(settings, MW1="07,1", AW1="07,1")
    assert r["dust_rule_id"] == "MW:blowing"
    assert sorted(r["pw_evidence"]) == ["AW1=07", "MW1=07"]


def test_fields_restriction() -> None:
    s = load_settings(overrides={"classify": {"fields": ["MW"]}})
    assert _cls(s, MW1="02,1", AW1="07,1")["dust_class_rule"] == "none"


def test_rejected_qc_and_no_present_weather(settings: Settings) -> None:
    assert _cls(settings, MW1="06,3")["dust_class_rule"] == "unknown"
    r = _cls(settings)
    assert (r["dust_class_rule"], r["has_pw"]) == ("unknown", False)


def test_flags_follow_classes(settings: Settings) -> None:
    r = _cls(settings, MW1="06,1")
    assert (r["flag_du"], r["flag_susp"], r["flag_bd"], r["flag_ds"]) == (True, True, False, False)
    r = _cls(settings, MW1="08,1")
    assert (r["flag_bd"], r["flag_ds"]) == (True, False)
    r = _cls(settings, MW1="98,1")
    assert (r["flag_ds"], r["flag_du"]) == (True, True)


@pytest.mark.parametrize(
    "fields,daily_dust",
    [
        ({"AT1": "AU,07,DU  ,5"}, True),
        ({"AX1": "07,5,24,5"}, True),
        ({"AX1": "08,5,24,5"}, False),
        ({"AT1": "AU,07,DU  ,3"}, False),
        ({"AT1": "AU,16,RA  ,5"}, False),
    ],
)
def test_daily_summary_dust_evidence(settings: Settings, fields: dict, daily_dust: bool) -> None:
    r = _cls(settings, REPORT_TYPE="SOD", **fields)
    assert r["flag_daily_dust"] is daily_dust
    assert r["has_pw"] is False  # daily summaries are not present-weather observations


@pytest.mark.parametrize(
    "tmp,past,ambiguous",
    [("+0304,1", True, False), ("-0050,1", False, False), ("+0015,1", False, True)],
)
def test_automated_past_weather_blowing_resolved_by_temperature(
    settings: Settings, tmp: str, past: bool, ambiguous: bool
) -> None:
    r = _cls(settings, MW1="02,1", AZ1="2,1,06,1", TMP=tmp, DEW="-0100,1")
    assert (r["flag_past"], r["flag_ambiguous"], r["flag_du"]) == (past, ambiguous, False)


def test_missing_present_weather_is_not_observed(settings: Settings) -> None:
    r = _cls(settings, AY1="3,1,06,1")  # past weather only: present weather NOT observed
    assert (r["has_pw"], r["dust_class_rule"], r["flag_past"]) == (False, "unknown", True)
