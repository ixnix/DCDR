"""Period notes in gold (stations.py + aggregate.py + the product SQL): years of a station whose
reports came from another place under the same station ID are marked in a `note` column of every
gold table (user decision 2026-11-06). Builds real gold from small silver files."""

import csv
import json
from pathlib import Path

import polars as pl

from dcdr.aggregate import build_gold
from dcdr.config import Settings, load_settings
from dcdr.stations import file_md5
from tests.unit.test_aggregate_stations import KUWAIT, RUN, _station

OTHER = "60715099999"
YEAR_TABLES = ("three_hourly", "daily", "monthly", "yearly")


def _with_notes(settings: Settings, tmp_path: Path, *rows: str) -> tuple[Settings, Path]:
    path = tmp_path / "period_notes.csv"
    path.write_text("station_id,from_year,to_year,note,decided\n" + "".join(r + "\n" for r in rows))
    s = load_settings(
        overrides={
            "paths": {"data_root": str(settings.data_root)},
            "stations": {
                "position_overrides": str(tmp_path / "no_overrides.csv"),
                "period_notes": str(path),
                "min_dusty_days": 0,
                "min_dusty_years": 0,
            },
        }
    )
    return s, path


def _notes_by_station(out: Path, table: str, column: str = "note") -> dict[str, set]:
    df = pl.read_parquet(out / table / "part-*.parquet")
    return {sid: set(g[column]) for (sid,), g in df.group_by("station_id")}


def test_period_notes_reach_every_gold_table(settings: Settings, tmp_path: Path) -> None:
    s, path = _with_notes(
        settings,
        tmp_path,
        f"{KUWAIT},2020,2024,reports from elsewhere,2026-11-06",
        f"{KUWAIT},1990,1990,an older note,2026-11-06",
        "99999999999,2024,2024,a station gold does not have,2026-11-06",
    )
    paths = [_station(s, KUWAIT, "29.2", "47.9"), _station(s, OTHER, "36.8", "10.2")]
    out = build_gold(s, paths, RUN)
    # the tables with a year (all rows are in 2024): the note covering 2024 only
    for table in YEAR_TABLES:
        notes = _notes_by_station(out, table)
        assert notes[KUWAIT] == {"2020-2024: reports from elsewhere"}, table
        assert notes[OTHER] == {None}, table
    # decadal: the decade 2021-2030 overlaps only the 2020-2024 note
    decadal = pl.read_parquet(out / "decadal" / "part-*.parquet")
    assert set(decadal["decade"]) == {2021}
    assert _notes_by_station(out, "decadal") == {
        KUWAIT: {"2020-2024: reports from elsewhere"},
        OTHER: {None},
    }
    # the station table lists every note of a station, in year order
    stations = _notes_by_station(out, "stations", "notes")
    assert stations[KUWAIT] == {"1990: an older note; 2020-2024: reports from elsewhere"}
    assert stations[OTHER] == {None}
    # a note for a station gold does not have is listed for review, not silently ignored
    with (out / "stations_review.csv").open() as f:
        review = {r["station_id"]: r["status"] for r in csv.DictReader(f)}
    assert review["99999999999"] == "note_unused"
    # lineage: which note file this version was built with
    md5 = file_md5(path)
    assert json.loads((out / "silver_version.json").read_text())["period_notes_md5"] == md5
    assert set(pl.read_parquet(out / "stations" / "part-000.parquet")["period_notes_md5"]) == {md5}


def test_without_a_notes_file_the_note_columns_are_empty(settings: Settings) -> None:
    out = build_gold(settings, [_station(settings, KUWAIT, "29.2", "47.9")], RUN)
    for table in (*YEAR_TABLES, "decadal"):
        df = pl.read_parquet(out / table / "part-*.parquet")
        assert df.schema["note"] == pl.String, table
        assert df["note"].null_count() == df.height, table
    st = pl.read_parquet(out / "stations" / "part-000.parquet")
    assert st["notes"].null_count() == st.height
    assert set(st["period_notes_md5"]) == {""}
