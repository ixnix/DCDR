"""End-to-end pipeline runs (ingest -> process -> aggregate) on fake S3 data in a temp folder.

Checks incremental behaviour: unchanged inputs are skipped, a changed source rebuilds only what
it affects, one failing station-year does not stop the others, and a year is rebuilt when the
next year's bronze arrives.
"""

from datetime import date

import obstore as obs
import polars as pl
from obstore.store import MemoryStore

from dcdr.config import Settings
from dcdr.pipeline import run_aggregate, run_ingest, run_process, silver_path, start_run
from tests.fixtures.isd import isd_row, make_isd_csv

STATION = "40582099999"
LON0 = {"LONGITUDE": "0.0"}


def _put(store: MemoryStore, year: int, rows: list[dict]) -> None:
    obs.put(store, f"{year}/{STATION}.csv", make_isd_csv(rows))


def _jan1(store: MemoryStore) -> None:
    rows = [
        isd_row(f"2024-01-01T{h:02d}:00:00", **LON0, MW1="05,1" if h == 0 else "02,1")
        for h in range(0, 24, 3)
    ]
    _put(store, 2024, rows)


def test_end_to_end_incremental(settings: Settings) -> None:
    store = MemoryStore()
    _put(store, 2023, [isd_row("2023-12-31T22:00:00", **LON0, MW1="06,1")])
    _jan1(store)
    ctx = start_run(settings, "test")

    ing = run_ingest(ctx, [STATION], [2024], store=store)
    assert ing.downloaded == 2  # 2024 plus 2023 for the haze look-back
    sil = run_process(ctx, [STATION], [2024])
    assert sil.built == 1
    daily = pl.read_parquet(run_aggregate(ctx, [STATION], [2024]) / "daily" / "part-000.parquet")
    jan1 = daily.filter(pl.col("lmst_date") == date(2024, 1, 1)).row(0, named=True)
    assert jan1["dusty_day_synoptic"] and jan1["n_dust_slots_dust_haze"] == 1

    # Unchanged inputs: nothing is re-downloaded or rebuilt.
    assert run_ingest(ctx, [STATION], [2024], store=store).downloaded == 0
    assert run_process(ctx, [STATION], [2024]).built == 0

    # A correction to the PREVIOUS year must rebuild this year's silver (look-back dependency).
    _put(
        store,
        2023,
        [
            isd_row("2023-12-31T22:00:00", **LON0, MW1="02,1"),
            isd_row("2023-12-31T23:00:00", **LON0),
        ],
    )
    assert run_ingest(ctx, [STATION], [2024], store=store).downloaded == 1
    assert run_process(ctx, [STATION], [2024]).built == 1
    daily = pl.read_parquet(run_aggregate(ctx, [STATION], [2024]) / "daily" / "part-000.parquet")
    jan1 = daily.filter(pl.col("lmst_date") == date(2024, 1, 1)).row(0, named=True)
    assert not jan1["dusty_day_synoptic"]


def test_one_bad_station_year_does_not_abort_silver(settings: Settings, monkeypatch) -> None:
    """Review I3: per-file failure isolation, like ingest."""
    import dcdr.pipeline as pipe

    store = MemoryStore()
    _jan1(store)
    obs.put(
        store,
        "2024/72386023169.csv",
        make_isd_csv([isd_row("2024-01-01T00:00:00", STATION="72386023169")]),
    )
    ctx = start_run(settings, "test")
    run_ingest(ctx, [STATION, "72386023169"], [2024], store=store)
    real = pipe.build_station_year

    def flaky(current, previous, year, settings_, **kw):
        if current["STATION"][0] == "72386023169":
            raise ValueError("corrupt station-year")
        return real(current, previous, year, settings_, **kw)

    monkeypatch.setattr(pipe, "build_station_year", flaky)
    s = run_process(ctx, [STATION, "72386023169"], [2024])
    assert (s.built, s.failed) == (1, 1)


def test_previous_year_rebuilt_when_next_year_bronze_arrives(settings: Settings) -> None:
    """Review I2: the Dec-31 23:56 row moves to the next year's file once that file exists."""
    store = MemoryStore()
    _put(
        store,
        2023,
        [
            isd_row("2023-12-31T21:00:00", **LON0),
            isd_row("2023-12-31T23:56:00", **LON0, REPORT_TYPE="FM-15"),
        ],
    )
    ctx = start_run(settings, "test")
    run_ingest(ctx, [STATION], [2023], store=store)  # 2024 bronze does not exist yet
    run_process(ctx, [STATION], [2023])
    assert pl.read_parquet(silver_path(ctx.root, 2023, STATION)).height == 2
    _jan1(store)
    run_ingest(ctx, [STATION], [2024], store=store)
    assert run_process(ctx, [STATION], [2023]).built == 1  # 2023 rebuilt because 2024 now exists
    assert pl.read_parquet(silver_path(ctx.root, 2023, STATION)).height == 1
