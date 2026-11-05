"""Migrations between method versions (process/migrations.py): which stations a change can affect."""

from dcdr.process import METHOD_VERSION
from dcdr.process.migrations import MIGRATIONS
from tests.fixtures.isd import bronze_frame, isd_row


def test_2_1_0_affects_only_stations_with_a_report_without_a_full_position() -> None:
    m = MIGRATIONS["2.1.0"]
    assert (m.from_version, m.to_version) == ("2.0.0", "2.1.0")
    full = bronze_frame([isd_row("2024-01-01T00:00:00"), isd_row("2024-01-01T03:00:00")])
    assert not m.affects(full)
    assert not m.affects(full.clear())  # a header-only file
    for fields in (
        {"LATITUDE": "0.0", "LONGITUDE": "0.0"},
        {"LONGITUDE": "999.999"},
        {"LATITUDE": "99.999"},
        {"LATITUDE": "", "LONGITUDE": "10.0"},
    ):
        mixed = bronze_frame(
            [isd_row("2024-01-01T00:00:00"), isd_row("2024-01-01T03:00:00", **fields)]
        )
        assert m.affects(mixed), fields


def test_migrations_are_keyed_by_their_target_version() -> None:
    for to, m in MIGRATIONS.items():
        assert m.to_version == to and m.from_version != to
    assert METHOD_VERSION == "2.1.0"  # update the 2.1.0 test above when the version moves on
