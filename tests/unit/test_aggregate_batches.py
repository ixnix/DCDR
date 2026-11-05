"""Gold built in batches of stations (aggregate.py, 2026-11-05): the whole ISD does not fit in
memory at once, so every product except the station table is written as one part file per batch.

Checks the split into batches (whole stations, in order, within the report budget), that a
batched build gives exactly the tables of a one-batch build, the part files, a batch without any
station in gold, the station table and review written once over all batches, DuckDB's memory
limit and spill folder, and the lineage.
"""

import json
import re
from pathlib import Path

import duckdb
import polars as pl
import pyarrow.parquet as pq
import pytest

from dcdr import aggregate
from dcdr.aggregate import (
    PRODUCTS,
    _hilbert,
    _size_groups,
    _spatial_order,
    _station_batches,
    build_gold,
)
from dcdr.config import Settings, load_settings, silver_config_hash
from dcdr.process.build import build_station_year, write_silver
from tests.fixtures.isd import bronze_frame, isd_row, write_isd_history

RUN = "20260101T000000Z-aaaaaa"
STATIONS = ["40582099999", "40582199999", "40582299999"]


def _silver(
    settings: Settings, station: str, dusty: bool = True, lat: str = "10.0", lon: str = "0.0"
) -> Path:
    """One station-year of 8 reports a day for 3 days, dusty on the first day if `dusty`."""
    rows = [
        isd_row(
            f"2024-01-0{d}T{h:02d}:00:00",
            STATION=station,
            LATITUDE=lat,
            LONGITUDE=lon,
            **({"MW1": "06,1"} if dusty and d == 1 else {}),
        )
        for d in (1, 2, 3)
        for h in range(0, 24, 3)
    ]
    df = build_station_year(bronze_frame(rows), None, 2024, settings)
    path = settings.data_root / "silver" / "year=2024" / f"{station}.parquet"
    cfg = silver_config_hash(settings)
    write_silver(df, path, run_id="r", config_hash=cfg, source_md5="m", prev_source_md5="")
    return path


def _with(settings: Settings, root: Path, **products: object) -> Settings:
    """The same settings with another data root and some `products` keys changed."""
    s = load_settings(
        overrides={
            "paths": {"data_root": str(root)},
            "stations": settings.stations.model_dump(),
            "products": products,
        }
    )
    write_isd_history(s)
    return s


def _tables(out: Path) -> dict[str, pl.DataFrame]:
    return {p.name: pl.read_parquet(out / p.name / "part-*.parquet") for p in PRODUCTS}


def test_batches_keep_stations_whole_and_in_order() -> None:
    paths = [Path(f"year={y}/{s}.parquet") for s in ("B", "A", "C") for y in (2001, 2002)]
    rows = [3, 3, 2, 2, 4, 1]  # B 6 reports, A 4, C 5
    batches = _station_batches(paths, rows, budget=9)
    assert [sorted({p.stem for p in b}) for b in batches] == [["A"], ["B"], ["C"]]
    batches = _station_batches(paths, rows, budget=10)
    assert [sorted({p.stem for p in b}) for b in batches] == [["A", "B"], ["C"]]
    assert sorted(p for b in batches for p in b) == sorted(paths)


def test_batches_follow_a_given_station_order() -> None:
    paths = [Path(f"year=2001/{s}.parquet") for s in ("A", "B", "C")]
    order = [("C", 0), ("A", 1), ("B", 2)]  # (station, grid cell)
    batches = _station_batches(paths, [1, 1, 1], budget=2, order=order)
    assert [[p.stem for p in b] for b in batches] == [["C", "A"], ["B"]]
    # stations not in the order (left out of gold) are not batched
    batches = _station_batches(paths, [1, 1, 1], budget=9, order=[("B", 0)])
    assert [[p.stem for p in b] for b in batches] == [["B"]]


def test_a_grid_cell_that_fits_is_never_split() -> None:
    paths = [Path(f"year=2001/{s}.parquet") for s in ("A", "B", "C")]
    order = [("A", 0), ("B", 1), ("C", 1)]
    # A fits with B, but then cell 1 would be split: it starts a batch of its own
    batches = _station_batches(paths, [1, 1, 1], budget=2, order=order)
    assert [[p.stem for p in b] for b in batches] == [["A"], ["B", "C"]]


def test_a_grid_cell_larger_than_a_batch_is_split_by_station() -> None:
    paths = [Path(f"year=2001/{s}.parquet") for s in ("A", "B", "C", "D")]
    order = [("A", 0), ("B", 1), ("C", 1), ("D", 1)]
    batches = _station_batches(paths, [1, 1, 1, 1], budget=2, order=order)
    assert [[p.stem for p in b] for b in batches] == [["A"], ["B", "C"], ["D"]]


def test_hilbert_curve_steps_to_a_neighbouring_cell() -> None:
    n = 8
    cells = sorted(((x, y) for x in range(n) for y in range(n)), key=lambda c: _hilbert(n, *c))
    assert sorted(_hilbert(n, x, y) for x, y in cells) == list(range(n * n))
    for (x1, y1), (x2, y2) in zip(cells, cells[1:], strict=False):
        assert abs(x1 - x2) + abs(y1 - y2) == 1


def test_spatial_order_keeps_grid_cells_together() -> None:
    stations = pl.DataFrame(
        {
            "station_id": ["A", "B", "C", "D", "E"],
            "trusted_lat": [51.0, -33.0, 52.0, 51.5, None],
            "trusted_lon": [1.0, 151.0, 4.0, 9.0, 5.0],
        }
    )
    order = _spatial_order(stations, grid_deg=10.0)
    ids = [s for s, _ in order]
    assert sorted(ids) == ["A", "B", "C", "D", "E"]
    cell = dict(order)
    # A, C, D share the 10-degree cell 50-60N, 0-10E: one cell, consecutive; B another cell; a
    # station without a position goes last, in a cell of its own
    assert cell["A"] == cell["C"] == cell["D"] != cell["B"]
    i = sorted(ids.index(s) for s in "ACD")
    assert i == list(range(i[0], i[0] + 3))
    assert ids[-1] == "E" and cell["E"] not in {cell[s] for s in "ABCD"}


def test_gold_files_follow_the_spatial_order(settings: Settings, tmp_path: Path) -> None:
    s = _with(settings, tmp_path / "o", gold_batch_reports=1)
    # station IDs in one order, positions in another: 0 and 2 share a cell, 1 is far away
    places = {
        STATIONS[0]: ("51.0", "1.0"),
        STATIONS[1]: ("-33.0", "151.0"),
        STATIONS[2]: ("52.0", "4.0"),
    }
    paths = [_silver(s, st, lat=la, lon=lo) for st, (la, lo) in places.items()]
    out = build_gold(s, paths, RUN)
    seen = pl.read_parquet(out / "daily" / "part-*.parquet")["station_id"].unique(
        maintain_order=True
    )
    assert abs(seen.to_list().index(STATIONS[0]) - seen.to_list().index(STATIONS[2])) == 1
    stations = pl.read_parquet(out / "stations" / "part-*.parquet")["station_id"]
    assert stations.to_list() == sorted(STATIONS)  # the station table stays a sorted lookup


def test_second_pass_reads_only_stations_in_gold(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = load_settings(
        overrides={
            "paths": {"data_root": str(tmp_path / "g")},
            "stations": {**settings.stations.model_dump(), "min_dusty_days": 1},
        }
    )
    write_isd_history(s)
    paths = [_silver(s, STATIONS[0]), _silver(s, STATIONS[1], dusty=False)]
    read: list[str] = []
    real = aggregate._build_batch

    def spy(con: duckdb.DuckDBPyConnection, batch: list[Path], *args: object) -> None:
        read.extend(p.stem for p in batch)
        real(con, batch, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(aggregate, "_build_batch", spy)
    build_gold(s, paths, RUN)
    assert read == [STATIONS[0]]


def _mixed_silver(settings: Settings, station: str, other: str) -> Path:
    """A station-year whose file is named after `station` but whose 4th day of reports carries
    another station ID, `other`, as 46 real ISD files do (a station renumbered mid-year)."""
    rows = [
        isd_row(
            f"2024-01-0{d}T{h:02d}:00:00",
            STATION=station if d < 4 else other,
            LATITUDE="10.0",
            LONGITUDE="0.0",
            **({"MW1": "06,1"} if d == 1 else {}),
        )
        for d in (1, 2, 3, 4)
        for h in range(0, 24, 3)
    ]
    df = build_station_year(bronze_frame(rows), None, 2024, settings)
    path = settings.data_root / "silver" / "year=2024" / f"{station}.parquet"
    cfg = silver_config_hash(settings)
    write_silver(df, path, run_id="r", config_hash=cfg, source_md5="m", prev_source_md5="")
    return path


def test_silver_check_finds_files_holding_another_station(
    settings: Settings, tmp_path: Path
) -> None:
    s = _with(settings, tmp_path / "k")
    paths = [_mixed_silver(s, STATIONS[0], STATIONS[2]), _silver(s, STATIONS[1])]
    check = aggregate._check_silver(paths, pl.DataFrame(schema={"station_id": pl.String}))
    assert check.links == {(STATIONS[0], STATIONS[2])}


def test_linked_stations_share_a_batch() -> None:
    paths = [Path(f"year=2001/{s}.parquet") for s in ("A", "B", "C")]
    # A's file also holds reports of C: A and C go together, before B (A sorts first)
    batches = _station_batches(paths, [5, 5, 5], budget=5, links={("A", "C")})
    assert [sorted(p.stem for p in b) for b in batches] == [["A", "C"], ["B"]]


def test_a_linked_station_brings_its_partner_files_into_pass_two() -> None:
    paths = [Path(f"year=2001/{s}.parquet") for s in ("A", "B", "C")]
    # only C is in gold, but some of C's reports are in A's file
    batches = _station_batches(paths, [1, 1, 1], budget=9, order=[("C", 0)], links={("A", "C")})
    assert [sorted(p.stem for p in b) for b in batches] == [["A", "C"]]


def test_a_station_split_over_two_files_stays_one_station(
    settings: Settings, tmp_path: Path
) -> None:
    one = _with(settings, tmp_path / "one", gold_batch_reports=10**9)
    many = _with(settings, tmp_path / "many", gold_batch_reports=1)
    outs = []
    for s in (one, many):
        paths = [
            _mixed_silver(s, STATIONS[0], STATIONS[2]),
            _silver(s, STATIONS[1]),
            _silver(s, STATIONS[2]),
        ]
        outs.append(build_gold(s, paths, RUN))
    a, b = _tables(outs[0]), _tables(outs[1])
    assert b["stations"]["station_id"].to_list() == sorted(STATIONS)  # no station twice
    for name in a:  # the same rows; linked stations share a batch, so the order may differ
        assert a[name].sort(pl.all()).equals(b[name].sort(pl.all())), name
    # STATIONS[2]'s 4th day comes from STATIONS[0]'s file
    days = b["daily"].filter(pl.col("station_id") == STATIONS[2])["lmst_date"]
    assert days.len() == 4


def test_a_station_in_gold_found_in_no_silver_file_stops_gold(
    settings: Settings, tmp_path: Path
) -> None:
    s = _with(settings, tmp_path / "n")
    right = _silver(s, STATIONS[0])
    wrong = right.with_name("NOT_A_STATION.parquet")
    # the reports say STATIONS[0]; neither the file name nor (without statistics) the footer does
    pq.write_table(pq.read_table(right), wrong, write_statistics=False)
    right.unlink()
    with pytest.raises(aggregate.GoldCheckError, match="found in no silver file"):
        build_gold(s, [wrong], RUN)


@pytest.mark.parametrize("deg", [0, -5, 7])
def test_grid_must_divide_the_globe(deg: float) -> None:
    with pytest.raises(ValueError, match="gold_grid_deg"):
        load_settings(overrides={"products": {"gold_grid_deg": deg}})


def test_a_station_larger_than_the_budget_is_a_batch_of_its_own() -> None:
    paths = [Path("year=2001/A.parquet"), Path("year=2001/B.parquet")]
    batches = _station_batches(paths, [50, 1], budget=10)
    assert [[p.stem for p in b] for b in batches] == [["A"], ["B"]]


def test_files_group_consecutive_parts_up_to_the_target_size() -> None:
    assert _size_groups([3, 3, 3, 5, 1], target=6) == [[0, 1], [2], [3, 4]]
    assert _size_groups([9, 1], target=6) == [[0], [1]]  # a larger part is a file of its own
    assert _size_groups([0, 0], target=6) == [[0, 1]]
    assert _size_groups([], target=6) == []


def test_batched_gold_equals_one_batch(settings: Settings, tmp_path: Path) -> None:
    one = _with(settings, tmp_path / "one", gold_batch_reports=10**9)
    # one station per batch, and every batch part a file of its own
    many = _with(settings, tmp_path / "many", gold_batch_reports=1, gold_file_mb=1e-9)
    out_one = build_gold(one, [_silver(one, s) for s in STATIONS], RUN)
    out_many = build_gold(many, [_silver(many, s) for s in STATIONS], RUN)
    a, b = _tables(out_one), _tables(out_many)
    for name in a:
        assert a[name].equals(b[name]), name
    assert a["daily"]["station_id"].unique().len() == 3
    # one part per batch for the per-station products, one file for the station table
    assert sorted(f.name for f in (out_many / "daily").iterdir()) == [
        "part-000.parquet",
        "part-001.parquet",
        "part-002.parquet",
    ]
    assert [f.name for f in (out_many / "stations").iterdir()] == ["part-000.parquet"]
    review = (out_one / "stations_review.csv").read_text()
    assert (out_many / "stations_review.csv").read_text() == review
    assert not (out_many / "_batches").exists()  # the staging folder is gone


def test_small_batch_parts_are_combined_into_one_file(settings: Settings, tmp_path: Path) -> None:
    s = _with(settings, tmp_path / "c", gold_batch_reports=1)  # 3 batches, default file size
    out = build_gold(s, [_silver(s, st) for st in STATIONS], RUN)
    for product in PRODUCTS:
        assert [f.name for f in (out / product.name).iterdir()] == ["part-000.parquet"]
    assert pl.read_parquet(out / "daily" / "part-000.parquet")["station_id"].n_unique() == 3


def test_batch_without_any_station_in_gold(settings: Settings, tmp_path: Path) -> None:
    s = _with(settings, tmp_path / "d", gold_batch_reports=1)
    s = load_settings(
        overrides={
            "paths": {"data_root": str(s.data_root)},
            "stations": {**settings.stations.model_dump(), "min_dusty_days": 1},
            "products": {"gold_batch_reports": 1},
        }
    )
    paths = [_silver(s, STATIONS[0]), _silver(s, STATIONS[1], dusty=False)]
    out = build_gold(s, paths, RUN)
    t = _tables(out)
    assert t["stations"].height == 2  # the station table lists every station ...
    assert t["daily"]["station_id"].unique().to_list() == [STATIONS[0]]  # ... gold the dusty one


def test_combining_detects_lost_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parts = []
    for i in range(2):
        parts.append(tmp_path / f"batch-{i}.parquet")
        pl.DataFrame({"station_id": [f"S{i}"] * 3}).write_parquet(parts[-1])

    class LossyCon:
        """Stands in for DuckDB: writes the combined file with one row missing."""

        def execute(self, sql: str) -> None:
            out = re.search(r"TO '([^']+)'", sql)
            assert out
            pl.DataFrame({"station_id": ["S0"] * 5}).write_parquet(out.group(1))

    with pytest.raises(aggregate.GoldCheckError, match="6 rows in the batch parts, 5 combined"):
        aggregate._combine(LossyCon(), parts, tmp_path / "out", 10**9)  # type: ignore[arg-type]


def test_lineage_records_the_batches(settings: Settings, tmp_path: Path) -> None:
    s = _with(settings, tmp_path / "l", gold_batch_reports=1)
    out = build_gold(s, [_silver(s, st) for st in STATIONS], RUN)
    lineage = json.loads((out / "silver_version.json").read_text())
    assert lineage["gold_batches"] == 3
    assert lineage["gold_batch_reports"] == 1
    assert lineage["gold_grid_deg"] == 10.0


def test_duckdb_gets_the_memory_limit_and_a_spill_folder(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _with(settings, tmp_path / "m", gold_memory_limit="1GB")
    seen: dict[str, str] = {}
    real = aggregate._connect

    def spy(settings: Settings, spill: Path) -> duckdb.DuckDBPyConnection:
        con = real(settings, spill)
        for key in ("memory_limit", "temp_directory"):
            row = con.execute(f"SELECT current_setting('{key}')").fetchone()
            seen[key] = str(row[0]) if row else ""
        return con

    monkeypatch.setattr(aggregate, "_connect", spy)
    out = build_gold(s, [_silver(s, STATIONS[0])], RUN)
    assert seen["memory_limit"].replace(" ", "") in ("953.6MiB", "1.0GiB", "1GB", "1000.0MB")
    assert Path(seen["temp_directory"]).parent == s.data_root / "gold"
    assert not Path(seen["temp_directory"]).exists()  # removed after the build
    assert out.exists()
