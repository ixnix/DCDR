import polars as pl
import pytest

from dcdr.config import Settings
from dcdr.silver.parse import parse_reports
from dcdr.silver.qc import apply_qc, code_flag
from tests.fixtures.isd import bronze_frame, isd_row


def _qc(settings: Settings, **fields: str) -> dict:
    df = parse_reports(bronze_frame([isd_row("2024-07-22T21:00:00", **fields)]))
    return apply_qc(df, settings.qc, settings.parse).row(0, named=True)


@pytest.mark.parametrize(
    "code,flag",
    [
        ("1", "ok"),
        ("5", "ok"),
        ("A", "ok"),
        ("M", "ok"),
        ("2", "suspect"),
        ("6", "suspect"),
        ("3", "erroneous"),
        ("7", "erroneous"),
        ("Z", "suspect"),
    ],
)
def test_code_flag_mapping(settings: Settings, code: str, flag: str) -> None:
    out = pl.DataFrame({"c": [code]}).select(code_flag(pl.col("c"), settings.qc)).item()
    assert out == flag


def test_out_of_range_values_are_erroneous(settings: Settings) -> None:
    # ISD format document limits: wind speed max 90.0 m/s, air temperature min -93.2 C
    r = _qc(settings, WND="090,1,N,0950,1", TMP="-0950,1")
    assert (r["wsd_flag"], r["use_wsd"]) == ("erroneous", False)
    assert r["t_flag"] == "erroneous"


def test_document_extremes_are_accepted(settings: Settings) -> None:
    r = _qc(settings, WND="090,1,N,0800,1", TMP="-0920,1", DEW="-0950,1")
    assert (r["wsd_flag"], r["t_flag"], r["td_flag"]) == ("ok", "ok", "ok")


def test_dewpoint_above_temperature_is_erroneous(settings: Settings) -> None:
    r = _qc(settings, TMP="+0200,1", DEW="+0210,1")
    assert r["td_flag"] == "erroneous" and r["rh"] is None


def test_rh_uses_magnus_formula(settings: Settings) -> None:
    assert _qc(settings, TMP="+0300,1", DEW="+0200,1")["rh"] == pytest.approx(55.07, abs=0.05)


def test_visibility_is_capped_and_needs_good_qc(settings: Settings) -> None:
    assert _qc(settings, VIS="016093,1,N,1")["vis_capped"] == 10000
    assert _qc(settings, VIS="005000,1,N,1")["vis_capped"] == 5000
    suspect = _qc(settings, VIS="005000,2,N,1")
    assert suspect["vis_flag"] == "suspect" and suspect["vis_capped"] is None


def test_gust_uses_od_types_3_and_4_only(settings: Settings) -> None:
    r = _qc(settings, OC1="0206,1", OD1="4,99,0211,1,350")
    assert (r["gust"], r["gust_src"], r["use_gust"]) == (21.1, "OD", True)
    r = _qc(settings, OD1="1,99,0300,1,350")
    assert r["gust"] is None and r["use_gust"] is False
