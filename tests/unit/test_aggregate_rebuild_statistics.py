"""Statistics-only rebuild (aggregate.rebuild_statistics, `dcdr aggregate --statistics-only`): a
new gold version made from the current one, with `stations`, `daily` and `three_hourly` copied
and `monthly`, `yearly`, `decadal` recomputed from that `daily` by the same SQL and checks
(user decision 2026-11-06: a statistics rule change must not need a 3.5-hour full rebuild).

The reference is always a full build with the same settings: the rebuilt tables must equal it,
row for row and in the same order."""

import json
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from dcdr.aggregate import ArchiveEndError, GoldCheckError, build_gold, rebuild_statistics
from dcdr.cli import app
from dcdr.config import Settings, load_settings
from tests.unit.test_aggregate_statistics import _day, _silver

RUN1, RUN2, RUN3 = "20260101T000000Z-aaaaaa", "20260102T000000Z-bbbbbb", "20260103T000000Z-cccccc"
STATS = ("monthly", "yearly", "decadal")
COPIED = ("stations", "daily", "three_hourly")


def _settings(settings: Settings, archive_end: str | None, **products: object) -> Settings:
    """The fixture's settings with another archive end (and other `products` keys)."""
    return load_settings(
        overrides={
            "paths": {"data_root": str(settings.data_root)},
            "products": {"archive_end": archive_end, "gold_keep_versions": 5, **products},
            "stations": settings.stations.model_dump(mode="json"),
        }
    )


def _read(out: Path, name: str) -> pl.DataFrame:
    return pl.concat([pl.read_parquet(p) for p in sorted((out / name).glob("part-*.parquet"))])


def _silver_two_stations(s: Settings) -> list[Path]:
    """Two stations, January-March and July 2011 and January-February 2024 (last local day
    2024-02-11); the one with the higher ID lies first in gold's spatial order, so an order by
    station ID would differ from a full build's."""
    paths = []
    for sid, lat, lon in (("40582099999", "29.2", "47.9"), ("10010099999", "70.9", "-8.7")):
        place = {"STATION": sid, "LATITUDE": lat}
        for year, months in ((2011, (1, 2, 3, 7)), (2024, (1, 2))):
            rows = [
                {**r, **place, "LONGITUDE": lon}
                for m in months
                for r in _day(f"{year}-{m:02d}-10", {3: "06,1"} if m in (2, 7) else None)
            ]
            paths.append(_silver(s, rows, year, name=sid))
    return paths


def test_rebuild_equals_a_full_build(settings: Settings) -> None:
    # gold built without an archive end, then the statistics rebuilt with one: they must equal a
    # full build with that archive end, and the other tables are the first version's, unchanged
    s1 = _settings(settings, None, gold_batch_reports=1)  # one station per batch
    paths = _silver_two_stations(s1)
    first = build_gold(s1, paths, RUN1)
    s2 = _settings(settings, "2024-02-20", gold_batch_reports=1)
    rebuilt = rebuild_statistics(s2, RUN2)
    assert (s2.data_root / "gold" / "CURRENT").read_text() == RUN2
    full = build_gold(s2, paths, RUN3)
    order = _read(full, "monthly")["station_id"].unique(maintain_order=True).to_list()
    assert order != sorted(order)  # the test means something: spatial order is not ID order
    for name in STATS:
        assert _read(rebuilt, name).equals(_read(full, name)), name
    for name in COPIED:
        assert _read(rebuilt, name).equals(_read(first, name)), name
    assert (rebuilt / "stations_review.csv").read_bytes() == (
        first / "stations_review.csv"
    ).read_bytes()
    assert _read(rebuilt, "yearly")["year"].unique().to_list() == [2011]


def test_rebuild_records_where_it_came_from(settings: Settings) -> None:
    s1 = _settings(settings, None)
    first = build_gold(s1, _silver_two_stations(s1), RUN1)
    rebuilt = rebuild_statistics(_settings(settings, "2024-02-20"), RUN2)
    before = json.loads((first / "silver_version.json").read_text())
    after = json.loads((rebuilt / "silver_version.json").read_text())
    assert after.pop("statistics_rebuilt_from") == RUN1
    assert after.pop("archive_end") == "2024-02-20" and before.pop("archive_end") is None
    assert after == before  # the silver it rests on, the station inputs, the batches


def test_rebuild_needs_a_gold_version(settings: Settings) -> None:
    with pytest.raises(GoldCheckError, match="no gold version"):
        rebuild_statistics(settings, RUN2)


def test_rebuild_refuses_changed_period_notes(settings: Settings) -> None:
    # decadal reads the notes file, daily already holds the old notes: a full rebuild is needed
    s1 = _settings(settings, None)
    build_gold(s1, _silver_two_stations(s1), RUN1)
    s1.period_notes_path.write_text(
        "station_id,from_year,to_year,note,decided\n40582099999,2011,2011,elsewhere,2026-11-06\n"
    )
    with pytest.raises(GoldCheckError, match="period notes"):
        rebuild_statistics(s1, RUN2)
    assert (s1.data_root / "gold" / "CURRENT").read_text() == RUN1
    assert not (s1.data_root / "gold" / "versions" / RUN2).exists()


def test_rebuild_refuses_daily_after_the_archive_end(settings: Settings) -> None:
    s1 = _settings(settings, None)
    build_gold(s1, _silver_two_stations(s1), RUN1)  # local days up to 2024-02-11
    with pytest.raises(ArchiveEndError, match="2024-02-11"):
        rebuild_statistics(_settings(settings, "2024-02-05"), RUN2)
    assert (s1.data_root / "gold" / "CURRENT").read_text() == RUN1


def test_rebuild_refuses_gold_of_another_silver(settings: Settings) -> None:
    # gold built from other silver settings: its daily follows other rules; rebuild in full
    s1 = _settings(settings, None)
    first = build_gold(s1, _silver_two_stations(s1), RUN1)
    lineage = json.loads((first / "silver_version.json").read_text())
    lineage["silver_config_hash"] = "0000000000000000"
    (first / "silver_version.json").write_text(json.dumps(lineage))
    with pytest.raises(GoldCheckError, match="silver"):
        rebuild_statistics(s1, RUN2)


def test_cli_aggregate_statistics_only(settings: Settings) -> None:
    s1 = _settings(settings, None)
    build_gold(s1, _silver_two_stations(s1), RUN1)
    profile = settings.data_root.parent / "here.yaml"
    profile.write_text(
        f"paths:\n  data_root: {settings.data_root}\n"
        f"stations:\n  period_notes: {s1.period_notes_path}\n"
        f"  position_overrides: {s1.position_overrides_path}\n"
        "products:\n  archive_end: 2024-02-20\n"
    )
    result = CliRunner().invoke(app, ["aggregate", "--statistics-only", "--profile", str(profile)])
    assert result.exit_code == 0, result.output
    current = (settings.data_root / "gold" / "CURRENT").read_text()
    assert current != RUN1 and f"'statistics_rebuilt_from': '{RUN1}'" in result.output


def test_cli_aggregate_needs_years_for_a_full_build() -> None:
    result = CliRunner().invoke(app, ["aggregate"])
    assert result.exit_code != 0
    assert "--years" in result.output
