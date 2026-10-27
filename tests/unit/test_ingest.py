"""Ingest (ingest.py) without the network: a fake in-memory S3 store stands in for NOAA.

Covers listing, the re-download rules (new, changed, previously failed), MD5 verification,
bronze writing, keeping the old bronze file when a download fails, and marking files that
disappear upstream as "missing_upstream" instead of deleting anything.
"""

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


# --- Checkpoints and progress (2026-10-22: a 30-min year was lost twice when a run stopped) ---
def _five_station_store() -> tuple[MemoryStore, set[str]]:
    store, ids = MemoryStore(), set()
    for i in range(5):
        sid = f"4058209999{i}"
        ids.add(sid)
        obs.put(
            store, f"2024/{sid}.csv", make_isd_csv([isd_row("2024-01-01T00:00:00", STATION=sid)])
        )
    return store, ids


def test_manifest_is_saved_at_checkpoints_so_an_interrupted_run_resumes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopped during the 5th of 5 files (checkpoint every 2): the 4 finished files are recorded
    on disk, so the next run downloads only what is left."""
    import threading

    store, ids = _five_station_store()
    cfg = settings.ingest.model_copy(update={"checkpoint_every": 2})
    real, calls, lock = ing.fetch_bytes, [0], threading.Lock()

    def stop_on_fifth(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        with lock:
            calls[0] += 1
            n = calls[0]
        if n == 5:
            raise KeyboardInterrupt  # what Ctrl-C (or a closed terminal) does to the run
        return real(*args, **kwargs)

    monkeypatch.setattr(ing, "fetch_bytes", stop_on_fifth)
    path = settings.data_root / "_state" / "sources.parquet"
    with pytest.raises(KeyboardInterrupt):
        ing.ingest(store, cfg, settings.data_root, Manifest(path), ids, [2024], "run1")
    saved = Manifest(path).df
    assert saved.filter(saved["status"] == "ok").height == 4

    monkeypatch.setattr(ing, "fetch_bytes", real)
    again = ing.ingest(store, cfg, settings.data_root, Manifest(path), ids, [2024], "run2")
    assert (again.downloaded, again.skipped) == (1, 4)


def test_progress_is_logged_at_each_checkpoint(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    store, ids = _five_station_store()
    cfg = settings.ingest.model_copy(update={"checkpoint_every": 2})
    path = settings.data_root / "_state" / "sources.parquet"
    with caplog.at_level("INFO", logger="dcdr.ingest"):
        ing.ingest(store, cfg, settings.data_root, Manifest(path), ids, [2024], "run1")
    lines = [r.getMessage() for r in caplog.records if "files" in r.getMessage()]
    assert any(line.startswith("2/5 files") for line in lines)
    assert any(line.startswith("5/5 files") for line in lines)


def test_recorded_file_missing_on_disk_is_downloaded_again(settings: Settings) -> None:
    """2026-10-27: 85 bronze files of 1990 were recorded "ok" but gone from disk (folder emptied
    after the pilot run), and ingest kept skipping them."""
    store = _store()
    manifest = Manifest(settings.data_root / "_state" / "sources.parquet")
    _run(settings, store, manifest)
    ing.bronze_path(settings.data_root, 2024, A).unlink()
    again = _run(settings, store, manifest)
    assert (again.downloaded, again.skipped) == (1, 0)
    assert ing.bronze_path(settings.data_root, 2024, A).exists()
