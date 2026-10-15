from datetime import UTC, datetime
from pathlib import Path

import obstore as obs
import pytest
from obstore.store import MemoryStore

from dcdr import ingest as ing
from dcdr.config import Settings
from dcdr.state import Manifest, read_parquet_metadata
from tests.fixtures.isd import isd_row, make_isd_csv

A, B = "40582099999", "72386023169"


def _store() -> MemoryStore:
    store = MemoryStore()
    obs.put(store, f"2024/{A}.csv", make_isd_csv([isd_row("2024-01-01T00:00:00")]))
    obs.put(store, f"2024/{B}.csv", make_isd_csv([isd_row("2024-01-01T00:00:00", STATION=B)]))
    obs.put(store, "2024/readme.txt", b"not a station")
    return store


def _run(settings: Settings, store: MemoryStore, manifest: Manifest) -> ing.IngestSummary:
    return ing.ingest(store, settings.ingest, settings.data_root, manifest, {A}, [2024], "run1")


def test_list_year_keeps_csv_only() -> None:
    keys = sorted(o.key for o in ing.list_year(_store(), 2024))
    assert keys == [f"2024/{A}.csv", f"2024/{B}.csv"]


def test_needs_download_rules() -> None:
    obj = ing.SourceObject("2024/A.csv", 10, "abc", datetime(2025, 1, 1, tzinfo=UTC))
    assert ing.needs_download(obj, None)
    assert not ing.needs_download(obj, {"status": "ok", "etag": "abc", "size": 10})
    assert ing.needs_download(obj, {"status": "ok", "etag": "old", "size": 10})
    assert ing.needs_download(obj, {"status": "failed", "etag": "abc", "size": 10})


def test_verify_md5_checks_only_md5_like_etags() -> None:
    data = b"hello"
    good = "5d41402abc4b2a76b9719d911017c592"
    assert ing.verify_md5(data, good) == good
    assert ing.verify_md5(data, "abc-3") == good  # multipart ETag: not comparable
    with pytest.raises(ing.IntegrityError):
        ing.verify_md5(data, "0" * 32)


def test_ingest_writes_bronze_and_skips_unchanged(settings: Settings) -> None:
    store = _store()
    manifest = Manifest(settings.data_root / "_state" / "sources.parquet")
    first = _run(settings, store, manifest)
    assert (first.selected, first.downloaded, first.failed) == (1, 1, 0)
    path = ing.bronze_path(settings.data_root, 2024, A)
    meta = read_parquet_metadata(path)
    assert meta["dcdr_layer"] == "bronze" and len(meta["source_md5"]) == 32
    row = manifest.get(f"2024/{A}.csv")
    assert row is not None and row["status"] == "ok" and row["rows"] == 1
    second = _run(settings, store, manifest)
    assert (second.downloaded, second.skipped) == (0, 1)


def test_changed_object_is_downloaded_again(settings: Settings) -> None:
    store = _store()
    manifest = Manifest(settings.data_root / "_state" / "sources.parquet")
    _run(settings, store, manifest)
    two_rows = [isd_row("2024-01-01T00:00:00"), isd_row("2024-01-01T03:00:00")]
    obs.put(store, f"2024/{A}.csv", make_isd_csv(two_rows))
    assert _run(settings, store, manifest).downloaded == 1
    row = manifest.get(f"2024/{A}.csv")
    assert row is not None and row["rows"] == 2


def test_failed_download_keeps_previous_bronze(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store()
    manifest = Manifest(settings.data_root / "_state" / "sources.parquet")
    _run(settings, store, manifest)
    path = ing.bronze_path(settings.data_root, 2024, A)
    md5_before = read_parquet_metadata(path)["source_md5"]
    obs.put(store, f"2024/{A}.csv", make_isd_csv([isd_row("2024-02-02T00:00:00")] * 3))

    def boom(*_: object, **__: object) -> bytes:
        raise RuntimeError("network down")

    monkeypatch.setattr(ing, "fetch_bytes", boom)
    summary = _run(settings, store, manifest)
    assert summary.failed == 1
    assert read_parquet_metadata(path)["source_md5"] == md5_before
    row = manifest.get(f"2024/{A}.csv")
    assert row is not None and row["status"] == "failed" and row["md5"] == md5_before


def test_object_removed_upstream_is_marked_not_deleted(settings: Settings) -> None:
    store = _store()
    manifest = Manifest(settings.data_root / "_state" / "sources.parquet")
    _run(settings, store, manifest)
    obs.delete(store, f"2024/{A}.csv")
    summary = _run(settings, store, manifest)
    assert summary.missing_upstream == 1
    row = manifest.get(f"2024/{A}.csv")
    assert row is not None and row["status"] == "missing_upstream"
    assert ing.bronze_path(settings.data_root, 2024, A).exists()


def test_bronze_path_layout(tmp_path: Path) -> None:
    assert ing.bronze_path(tmp_path, 2024, A) == tmp_path / "bronze" / "year=2024" / f"{A}.parquet"
