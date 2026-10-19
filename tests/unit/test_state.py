"""Run state (state.py): atomic writes (a failed write keeps the old file), Parquet metadata,
safe folder listing, manifest upserts, run IDs and the run log (including older log files).
"""

import re
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from dcdr import state


def test_atomic_parquet_roundtrip_with_metadata(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b.parquet"
    state.atomic_write_parquet(
        pl.DataFrame({"x": [1, 2]}).to_arrow(), path, {"dcdr_layer": "bronze"}
    )
    assert pl.read_parquet(path)["x"].to_list() == [1, 2]
    assert state.read_parquet_metadata(path) == {"dcdr_layer": "bronze"}
    assert not path.with_name("b.parquet.tmp").exists()


def test_failed_replace_keeps_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.parquet"
    state.atomic_write_parquet(pl.DataFrame({"x": [1]}).to_arrow(), path)

    def boom(*_: object) -> None:
        raise OSError("disk unplugged")

    monkeypatch.setattr(state.os, "replace", boom)
    with pytest.raises(OSError):
        state.atomic_write_parquet(pl.DataFrame({"x": [99]}).to_arrow(), path)
    monkeypatch.undo()
    assert pl.read_parquet(path)["x"].to_list() == [1]
    assert not path.with_name("f.parquet.tmp").exists()


def test_safe_listdir_skips_hidden_and_tmp(tmp_path: Path) -> None:
    for name in ("part-000.parquet", "._part-000.parquet", ".DS_Store", "x.parquet.tmp"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in state.safe_listdir(tmp_path)] == ["part-000.parquet"]
    assert state.safe_listdir(tmp_path / "missing") == []


def test_manifest_upsert_latest_wins_and_reloads(tmp_path: Path) -> None:
    m = state.Manifest(tmp_path / "sources.parquet")
    when = datetime(2025, 3, 1, tzinfo=UTC)
    m.upsert(
        [
            {
                "key": "2024/A.csv",
                "station_id": "A",
                "year": 2024,
                "status": "failed",
                "last_modified": when,
            }
        ]
    )
    m.upsert(
        [
            {
                "key": "2024/A.csv",
                "station_id": "A",
                "year": 2024,
                "status": "ok",
                "last_modified": when,
            }
        ]
    )
    m.save()
    again = state.Manifest(tmp_path / "sources.parquet")
    assert again.df.height == 1
    row = again.get("2024/A.csv")
    assert row is not None and row["status"] == "ok"
    assert again.get("nope") is None


def test_run_id_format() -> None:
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", state.new_run_id())


def test_append_run_record_accumulates(tmp_path: Path) -> None:
    runs = tmp_path / "runs.parquet"
    for rid in ("r1", "r2"):
        state.append_run_record(runs, {"run_id": rid, "command": "test", "status": "ok"})
    assert pl.read_parquet(runs)["run_id"].to_list() == ["r1", "r2"]


def test_run_log_written_before_a_new_column_still_appends(tmp_path: Path) -> None:
    runs = tmp_path / "runs.parquet"
    old_schema = {k: v for k, v in state.RUNS_SCHEMA.items() if k != "dcdr_version"}
    pl.DataFrame([{"run_id": "r0"}], schema=old_schema).write_parquet(runs)
    state.append_run_record(runs, {"run_id": "r1", "dcdr_version": "0.1.0"})
    df = pl.read_parquet(runs)
    assert df.columns == list(state.RUNS_SCHEMA)
    assert df["dcdr_version"].to_list() == [None, "0.1.0"]
