"""Source groups (process/sources.py): each report's group (SYNOP, METAR, other) and whether its
type is excluded (daily/monthly summaries); the silver contract on the two columns.
"""

import polars as pl
import pytest

from dcdr.config import Settings
from dcdr.process.build import build_station_year
from dcdr.process.sources import add_source_group
from dcdr.schemas import SilverContractError, validate_silver
from tests.fixtures.isd import bronze_frame, isd_row


def _groups(settings: Settings, types: list[str | None]) -> list[tuple[str | None, bool]]:
    df = add_source_group(
        pl.DataFrame({"report_type": types}, schema={"report_type": pl.String}), settings.sources
    )
    return list(zip(df["source_group"].to_list(), df["is_excluded_type"].to_list(), strict=True))


def test_groups_follow_config(settings: Settings) -> None:
    assert _groups(settings, ["FM-12", "AUST", "FM-16", "SAOSP", "AERO", "SOD"]) == [
        ("synop", False),
        ("synop", False),
        ("metar", False),
        ("metar", False),
        ("other", False),
        (None, True),
    ]


def test_unknown_or_missing_type_is_other(settings: Settings) -> None:
    assert _groups(settings, ["XYZ", None]) == [("other", False), ("other", False)]


def test_contract_rejects_group_on_excluded_type(settings: Settings) -> None:
    rows = [isd_row("2024-01-01T03:00:00"), isd_row("2024-01-01T23:59:00", REPORT_TYPE="SOD")]
    out = build_station_year(bronze_frame(rows), None, 2024, settings)
    validate_silver(out)  # the real output satisfies the contract
    broken = out.with_columns(source_group=pl.lit("other"))
    with pytest.raises(SilverContractError):
        validate_silver(broken)
