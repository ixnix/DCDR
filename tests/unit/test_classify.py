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


# --- SYNOP iX indicator (WMO code table 1860; user decision 2026-10-19) -----------------------
ZABOL_IX2 = "SYN08040829 12960 03406 10062 21002="  # weather group omitted: no significant weather
IX3 = "SYN07040829 13960 03406 10062="  # weather group omitted: no observation
IX7 = "SYN06095322 47/// /2821 10282 7//44="  # automatic station, tables 4680/4531


def test_ix2_means_observed_with_no_significant_weather(settings: Settings) -> None:
    r = _cls(settings, REM=ZABOL_IX2)
    assert (r["has_pw"], r["dust_class_rule"], r["pw_status"]) == (True, "none", "nil_significant")


def test_ix3_means_not_observed(settings: Settings) -> None:
    r = _cls(settings, REM=IX3)
    assert (r["has_pw"], r["dust_class_rule"], r["pw_status"]) == (False, "unknown", "not_observed")


def test_reported_weather_wins_and_is_labelled(settings: Settings) -> None:
    r = _cls(settings, MW1="06,1", REM=ZABOL_IX2)  # inconsistent message: trust the code
    assert (r["dust_class_rule"], r["pw_status"]) == ("suspended", "reported")


def test_without_synop_text_missing_weather_is_still_not_observed(settings: Settings) -> None:
    for rem in ("SYN004BUFR", "MET046METAR OIZB 010000Z 34012KT"):
        r = _cls(settings, REM=rem)
        assert (r["has_pw"], r["dust_class_rule"], r["pw_status"]) == (
            False,
            "unknown",
            "not_observed",
        )


@pytest.mark.parametrize(
    "aw,rem,expected",
    [
        ("05,1", IX7, "haze_or_suspended"),  # WMO 4680: haze/smoke/dust in suspension, vis < 1 km
        ("05,1", None, "none"),  # ISD's own AW list: 05 = smoke
        ("07,1", IX7, "none"),  # WMO 4680 defines no code 07
        ("07,1", None, "blowing"),  # ISD's own AW list: 07 = dust or sand raised by wind
        ("04,1", IX7, "haze_or_suspended"),
    ],
)
def test_aw_code_table_depends_on_the_synop_ix(
    settings: Settings, aw: str, rem: str | None, expected: str
) -> None:
    fields = {"AW1": aw} | ({"REM": rem} if rem else {})
    assert _cls(settings, **fields)["dust_class_rule"] == expected


def test_synop_ix_rule_can_be_switched_off(settings: Settings) -> None:
    off = settings.model_copy(
        update={"classify": settings.classify.model_copy(update={"synop_ix": False})}
    )
    r = _cls(off, REM=ZABOL_IX2)
    assert (r["has_pw"], r["dust_class_rule"]) == (False, "unknown")


# --- Inferred "nothing significant" when iX is missing (rule V3; user decision 2026-10-20) ----
def _cls_g(settings: Settings, guard: bool | None, **fields: str) -> dict:
    df = add_time_columns(parse_reports(bronze_frame([isd_row("2024-07-22T21:00:00", **fields)])))
    df = apply_qc(df, settings.qc, settings.parse)
    return classify_reports(df, settings.classify, settings.qc, nil_guard=guard).row(0, named=True)


def test_no_code_no_ix_with_visibility_is_inferred_nil_when_station_passes_guard(
    settings: Settings,
) -> None:
    r = _cls_g(settings, True, REM="SYN004BUFR")  # FM-12, visibility reported, no code, no iX
    assert (r["has_pw"], r["dust_class_rule"], r["pw_status"]) == (True, "none", "nil_inferred")


@pytest.mark.parametrize("guard", [False, None])
def test_no_inference_when_station_fails_or_lacks_the_guard(
    settings: Settings, guard: bool | None
) -> None:
    r = _cls_g(settings, guard)
    assert (r["has_pw"], r["pw_status"]) == (False, "not_observed")


@pytest.mark.parametrize(
    "fields",
    [
        {"VIS": "999999,9,9,9"},  # visibility not reported
        {"VIS": "010000,3,N,3"},  # visibility rejected by QC
        {"REM": IX3},  # explicit "not observed" wins over inference
        {"REPORT_TYPE": "FM-15"},  # METAR: rule validated for SYNOP only
    ],
)
def test_no_inference_without_visibility_or_with_explicit_ix_or_non_synop(
    settings: Settings, fields: dict
) -> None:
    assert _cls_g(settings, True, **fields)["pw_status"] == "not_observed"


def test_explicit_nil_is_not_relabelled_inferred(settings: Settings) -> None:
    assert _cls_g(settings, True, REM=ZABOL_IX2)["pw_status"] == "nil_significant"


def test_inference_can_be_switched_off(settings: Settings) -> None:
    off = settings.model_copy(
        update={"classify": settings.classify.model_copy(update={"infer_nil": False})}
    )
    assert _cls_g(off, True)["pw_status"] == "not_observed"


# --- METAR/SPECI present weather (user decisions 2026-10-20; docs/science.md 5.2) -------------
# A METAR only fills a slot when no SYNOP does (sampling priority). Rules per report:
#   AUTO (any content) or "//"          -> not observed (machines cannot detect dust)
#   weather group, decoded by ISD       -> reported (dust at any visibility; visibility = strength)
#   weather group ISD could not decode  -> not observed
#   CAVOK                               -> nothing significant (explicit, WMO 15.10)
#   no group, visibility > 5 km         -> nothing significant, implied (pw_status "nil_metar")
#   no group, visibility <= 5 km/missing-> not observed (something reduced visibility, unnamed)
def _metar(body: str, report_type: str = "FM-15", **fields: str) -> dict:
    return _cls(load_settings(), REPORT_TYPE=report_type, REM=f"MET000METAR {body}", **fields)


NOGROUP = "OIZB 010300Z 34012KT 9999 NSC 30/10 Q1010"
SUSPENDED_AU = "0,0,00,5,0,1,1"  # AU: obscuration DU (widespread dust)
HAZE_AU = "0,0,00,7,0,1,1"  # AU: obscuration HZ


@pytest.mark.parametrize(
    "vis,status",
    [
        ("010000,1,N,1", "nil_metar"),
        ("006000,1,N,1", "nil_metar"),
        ("005000,1,N,1", "not_observed"),  # DU/HZ/BR would have had to be coded at <= 5 km
        ("004000,1,N,1", "not_observed"),
        ("999999,9,9,9", "not_observed"),  # visibility missing
        ("010000,3,N,3", "not_observed"),  # visibility rejected by QC
    ],
)
def test_metar_without_weather_group_depends_on_visibility(vis: str, status: str) -> None:
    r = _metar(NOGROUP, VIS=vis)
    assert (r["pw_status"], r["has_pw"]) == (status, status == "nil_metar")
    assert r["dust_class_rule"] == ("none" if status == "nil_metar" else "unknown")


def test_speci_follows_the_metar_rules() -> None:
    assert _metar(NOGROUP, report_type="FM-16")["pw_status"] == "nil_metar"


def test_cavok_is_explicit_nothing_significant() -> None:
    r = _metar("OIZB 010300Z 34012KT CAVOK 30/10 Q1010", VIS="999999,9,9,9")
    assert (r["pw_status"], r["dust_class_rule"]) == ("nil_significant", "none")


def test_metar_with_slashes_is_not_observed() -> None:
    r = _metar("YBMA 082130Z AUTO 00000KT 9999 // SCT085 15/09 Q1017")
    assert (r["pw_status"], r["has_pw"]) == ("not_observed", False)


@pytest.mark.parametrize("au", [SUSPENDED_AU, HAZE_AU])
def test_auto_metar_codes_are_not_used(au: str) -> None:
    """ASOS reports no dust and infers HZ from visibility and humidity (ASOS guide 3.11); AUTO
    dust codes in the data are implausible (e.g. SA with visibility 9999)."""
    r = _metar("ZWSH 021800Z AUTO 34005MPS 9999 SA 19/13 Q1016", AU1=au)
    assert (r["pw_status"], r["dust_rank"], r["dust_class_rule"], r["flag_du"]) == (
        "not_observed",
        0,
        "unknown",
        False,
    )


def test_auto_metar_without_group_is_not_observed() -> None:
    assert _metar("YPLM 270800Z AUTO 09009KT 9999 NCD 39/12 Q1010")["pw_status"] == "not_observed"


def test_metar_dust_counts_at_any_visibility() -> None:
    """User decision: the weather group says dust; visibility only tells its strength."""
    r = _metar(
        "KELP 011851Z 12012G27KT 6SM BLDU CLR 30/02 A3027", AU1="0,5,00,5,0,1,1", VIS="009656,1,N,1"
    )
    assert (r["pw_status"], r["dust_class_rule"]) == ("reported", "blowing")


def test_metar_group_isd_could_not_decode_is_not_observed() -> None:
    r = _metar("GAGO 031600Z 09034KT 9999 TSDU FEW030 44/13 Q1007")
    assert (r["pw_status"], r["has_pw"]) == ("not_observed", False)


def test_metar_haze_stays_haze_for_the_haze_rule() -> None:
    r = _metar("OIZB 010300Z 34012KT 4000 HZ 30/10 Q1010", AU1=HAZE_AU, VIS="004000,1,N,1")
    assert (r["pw_status"], r["dust_class_rule"]) == ("reported", "haze")


def test_metar_rules_apply_only_to_metar_report_types() -> None:
    r = _cls(load_settings(), REPORT_TYPE="FM-12", REM=f"MET000METAR {NOGROUP}")
    assert r["pw_status"] == "not_observed"


def test_metar_rules_can_be_switched_off() -> None:
    s = load_settings()
    off = s.model_copy(update={"classify": s.classify.model_copy(update={"metar_rules": False})})
    no_group = _cls(off, REPORT_TYPE="FM-15", REM=f"MET000METAR {NOGROUP}")
    auto = _cls(
        off,
        REPORT_TYPE="FM-15",
        REM="MET000METAR ZWSH 021800Z AUTO 34005MPS 9999 SA",
        AU1=SUSPENDED_AU,
    )
    assert (no_group["pw_status"], auto["dust_class_rule"]) == ("not_observed", "suspended")
