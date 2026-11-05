"""Targeted rebuild: after a method change, silver files the change cannot affect are carried
forward (ledger in _state/) instead of rebuilt, gold accepts them, and `verify_silver` checks a
sample by rebuilding it. Also: isd-history places station-years without a position (2.1.0).

The tests pretend the code moved on to NEW, with a migration from the current version OLD.
"""

from collections.abc import Callable

import obstore as obs
import polars as pl
import pytest
from obstore.store import MemoryStore

import dcdr.aggregate as aggregate_mod
import dcdr.pipeline as pipeline_mod
import dcdr.process.build as build_mod
from dcdr.aggregate import MixedSilverVersionsError
from dcdr.config import Settings
from dcdr.pipeline import (
    RunContext,
    run_aggregate,
    run_ingest,
    run_process,
    run_verify_silver,
    silver_path,
    start_run,
)
from dcdr.process import METHOD_VERSION
from dcdr.process.migrations import MIGRATIONS, Migration, ledger_path, read_ledger
from dcdr.state import atomic_write_parquet, read_parquet_metadata
from tests.fixtures.isd import isd_row, make_isd_csv, write_isd_history

OLD, NEW = METHOD_VERSION, "9.9.9"
A, B = "40582099999", "40583099999"  # A: unaffected by the pretend change, B: affected
YEARS = [2023, 2024]


def _day(station: str, year: int, **fields: str) -> list[dict[str, str]]:
    return [
        isd_row(
            f"{year}-03-01T{h:02d}:00:00",
            STATION=station,
            MW1="07,1" if h == 12 else "02,1",
            **fields,
        )
        for h in range(0, 24, 3)
    ]


def _put(store: MemoryStore, station: str, year: int, rows: list[dict[str, str]]) -> None:
    obs.put(store, f"{year}/{station}.csv", make_isd_csv(rows))


def _built_under_old(settings: Settings) -> tuple[RunContext, MemoryStore]:
    store = MemoryStore()
    for s in (A, B):
        for y in YEARS:
            _put(store, s, y, _day(s, y))
    ctx = start_run(settings, "test")
    run_ingest(ctx, [A, B], YEARS, store=store)
    assert run_process(ctx, [A, B], YEARS).built == 4
    return ctx, store


def _station_b(raw: pl.DataFrame) -> bool:
    return bool((raw["STATION"] == B).any())


@pytest.fixture
def new_method(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Switch the running code to NEW; with `affects`, also add a migration OLD -> NEW."""

    def install(affects: Callable[[pl.DataFrame], bool] | None = None) -> None:
        for mod in (pipeline_mod, build_mod, aggregate_mod):
            monkeypatch.setattr(mod, "METHOD_VERSION", NEW)
        if affects is not None:
            monkeypatch.setitem(MIGRATIONS, NEW, Migration(OLD, NEW, "test change", affects))

    return install


def test_unaffected_files_are_carried_forward_not_rebuilt(
    settings: Settings, new_method: Callable[..., None]
) -> None:
    ctx, _ = _built_under_old(settings)
    before = {y: silver_path(ctx.root, y, A).read_bytes() for y in YEARS}
    new_method(_station_b)

    s = run_process(ctx, [A, B], YEARS)

    assert (s.built, s.carried, s.skipped) == (2, 2, 0)
    assert {y: silver_path(ctx.root, y, A).read_bytes() for y in YEARS} == before  # untouched
    assert read_parquet_metadata(silver_path(ctx.root, 2024, B))["method_version"] == NEW
    ledger = read_ledger(ledger_path(ctx.root))
    assert sorted(ledger["station_id"] + "/" + ledger["year"].cast(pl.String)) == [
        f"{A}/2023",
        f"{A}/2024",
    ]
    assert set(ledger["built_version"]) == {OLD} and set(ledger["valid_for"]) == {NEW}
    # the next run finds everything current
    again = run_process(ctx, [A, B], YEARS)
    assert (again.built, again.carried, again.skipped) == (0, 0, 4)


def test_without_a_migration_everything_is_rebuilt(
    settings: Settings, new_method: Callable[..., None]
) -> None:
    ctx, _ = _built_under_old(settings)
    new_method()
    s = run_process(ctx, [A, B], YEARS)
    assert (s.built, s.carried) == (4, 0)


def test_a_changed_input_is_rebuilt_even_when_the_change_cannot_affect_it(
    settings: Settings, new_method: Callable[..., None]
) -> None:
    ctx, store = _built_under_old(settings)
    _put(store, A, 2024, _day(A, 2024, TMP="+0300,1"))  # NOAA revised A's 2024 file
    run_ingest(ctx, [A], [2024], store=store)
    new_method(_station_b)
    s = run_process(ctx, [A], YEARS)
    assert (s.built, s.carried) == (1, 1)  # 2024 rebuilt (new bronze); 2023 carried


def test_gold_accepts_carried_files_only_with_their_ledger_rows(
    settings: Settings, new_method: Callable[..., None]
) -> None:
    ctx, _ = _built_under_old(settings)
    new_method(_station_b)
    run_process(ctx, [A, B], YEARS)
    run_aggregate(ctx, [A, B], YEARS)  # OLD files of A count as NEW through the ledger
    ledger_path(ctx.root).unlink()
    with pytest.raises(MixedSilverVersionsError):  # the carried files count as OLD again
        run_aggregate(ctx, [A, B], YEARS)


def test_verify_silver_rebuilds_a_sample_of_carried_files(
    settings: Settings, new_method: Callable[..., None]
) -> None:
    ctx, _ = _built_under_old(settings)
    new_method(_station_b)
    run_process(ctx, [A, B], YEARS)

    ok = run_verify_silver(ctx, sample=10, seed=0)
    assert (ok.checked, ok.different) == (2, [])

    # a carried file whose data no longer match what the code builds is caught
    path = silver_path(ctx.root, 2024, A)
    table = pl.read_parquet(path).head(3).to_arrow()
    atomic_write_parquet(table, path, read_parquet_metadata(path))
    bad = run_verify_silver(ctx, sample=10, seed=0)
    assert (bad.checked, bad.different) == (2, [f"{A}/2024"])


def test_isd_history_places_a_station_year_and_a_change_rebuilds_it(settings: Settings) -> None:
    store = MemoryStore()
    nowhere = {"LATITUDE": "0.0", "LONGITUDE": "0.0"}
    _put(store, A, 2024, _day(A, 2024, **nowhere))
    _put(store, B, 2024, _day(B, 2024))  # full positions: isd-history does not matter
    history = [{"USAF": s[:6], "WBAN": s[6:], "LAT": "+34.059", "LON": "+117.556"} for s in (A, B)]
    write_isd_history(settings, history)
    ctx = start_run(settings, "test")
    run_ingest(ctx, [A, B], [2024], store=store)

    s = run_process(ctx, [A, B], [2024])
    assert (s.built, s.no_position) == (2, 0)
    a = silver_path(ctx.root, 2024, A)
    assert set(pl.read_parquet(a)["lon_used"]) == {117.556}
    assert read_parquet_metadata(a)["lon_fill"] == "117.556"

    history[0]["LON"] = "+117.600"  # NOAA corrects the station's position
    history[1]["LON"] = "+050.000"
    write_isd_history(settings, history)
    again = run_process(ctx, [A, B], [2024])
    assert (again.built, again.skipped) == (1, 1)  # only the year that relied on isd-history
    assert set(pl.read_parquet(a)["lon_used"]) == {117.6}
