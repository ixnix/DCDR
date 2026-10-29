"""Haze rule and humidity screen (classify.apply_haze_and_rh).

Haze within the 24 h after a dust report becomes "dust_haze"; the window boundary is exclusive
at exactly 24 h; a window of 0 switches the rule off; the RH screen hits classes built from haze only.
"""

import polars as pl

from dcdr.config import Settings, load_settings
from dcdr.process.classify import apply_haze_and_rh, classify_reports
from dcdr.process.parse import add_time_columns, parse_reports
from dcdr.process.qc import apply_qc
from tests.fixtures.isd import bronze_frame, isd_row


def _run(settings: Settings, rows: list[dict]) -> pl.DataFrame:
    df = add_time_columns(parse_reports(bronze_frame(rows)))
    df = (
        apply_qc(df, settings.qc, settings.parse)
        .sort("time_utc")
        .with_columns(pl.col("time_utc").set_sorted())
    )
    df = classify_reports(df, settings.classify, settings.qc)
    return apply_haze_and_rh(df, settings.classify, settings.qc)


def test_haze_within_24h_after_dust_becomes_dust_haze(settings: Settings) -> None:
    out = _run(
        settings,
        [isd_row("2024-03-01T00:00:00", MW1="06,1"), isd_row("2024-03-01T23:59:00", MW1="05,1")],
    )
    assert out["dust_class_final"].to_list() == ["suspended", "dust_haze"]
    assert out["flag_duhz"].to_list() == [True, True]


def test_window_excludes_exactly_24h(settings: Settings) -> None:
    out = _run(
        settings,
        [isd_row("2024-03-01T00:00:00", MW1="06,1"), isd_row("2024-03-02T00:00:00", MW1="05,1")],
    )
    assert out["dust_class_final"].to_list() == ["suspended", "haze"]


def test_haze_without_prior_dust_stays_haze(settings: Settings) -> None:
    out = _run(settings, [isd_row("2024-03-01T00:00:00", MW1="05,1")])
    assert (out["dust_class_final"].item(), out["flag_duhz"].item()) == ("haze", False)


def test_zero_window_disables_rule() -> None:
    s = load_settings(overrides={"classify": {"haze_window_h": 0}})
    out = _run(
        s, [isd_row("2024-03-01T00:00:00", MW1="06,1"), isd_row("2024-03-01T01:00:00", MW1="05,1")]
    )
    assert out["dust_class_final"].to_list() == ["suspended", "haze"]


def test_rh_screen_applies_to_haze_classes_not_to_named_dust(settings: Settings) -> None:
    # Method 1.9.0 (user decision 2026-10-29): dust an observer named (06 suspended, 07 blowing)
    # is kept in humid air; only classes built from haze reports (dust_haze) are screened.
    humid = {"TMP": "+0200,1", "DEW": "+0195,1"}  # RH ~ 97 %
    out = _run(
        settings,
        [
            isd_row("2024-03-01T00:00:00", MW1="06,1", **humid),
            isd_row("2024-03-01T03:00:00", MW1="07,1", **humid),
            isd_row("2024-03-01T06:00:00", MW1="05,1", **humid),  # haze after dust
        ],
    )
    assert out["dust_class_rule"].to_list() == ["suspended", "blowing", "haze"]
    assert out["dust_class_final"].to_list() == ["suspended", "blowing", "screened"]
    assert out["rh_screened"].to_list() == [False, False, True]
