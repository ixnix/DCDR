"""Ingest step (NOAA S3 -> bronze): list ISD files, download new or changed ones, save as Parquet.

Where this fits
---------------
First stage of the pipeline: `dcdr ingest` (or `dcdr run`). Input is NOAA's public S3 bucket
`noaa-global-hourly-pds`, which holds one CSV per station per year:

    s3://noaa-global-hourly-pds/2024/40582099999.csv   (year folder / station ID .csv)

Output is the BRONZE layer: one Parquet file per station-year, with every column kept as text,
exactly as received, plus three provenance columns (source key, MD5, run ID):

    <data_root>/bronze/year=2024/40582099999.parquet

Why bronze keeps everything as text: decoding ISD's packed fields ("0350,1,N,0046,1") is the job
of the process step. If decoding has a bug, we fix it and re-run processing WITHOUT downloading
anything again; bronze is our faithful local copy of the source.

Incremental by design
---------------------
Each run lists the year folders on S3 and compares each file's ETag/size with the manifest (see
state.Manifest). Only new or changed files are downloaded, so re-running is cheap and safe.
Files that disappear upstream are marked "missing_upstream" (their bronze copy is kept).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import obstore as obs
import polars as pl
from obstore.store import S3Store

from dcdr.config import IngestCfg
from dcdr.state import MANIFEST_SCHEMA, Manifest, atomic_write_parquet, utcnow

log = logging.getLogger(__name__)
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")  # a plain MD5 hex string (multipart ETags look different)


@dataclass(frozen=True)
class SourceObject:
    """One file listed on S3 (one station-year)."""

    key: str  # e.g. "2024/40582099999.csv"
    size: int
    etag: str
    last_modified: datetime

    @property
    def station_id(self) -> str:
        """Station ID from the key: 2024/40582099999.csv -> 40582099999 (file name stem)."""
        return Path(self.key).stem

    @property
    def year(self) -> int:
        """Year from the key: 2024/40582099999.csv -> 2024 (the folder name)."""
        return int(self.key.split("/", 1)[0])


@dataclass
class IngestSummary:
    """Counts reported at the end of a run (also saved in the run log)."""

    listed: int = 0  # files listed on S3 for the requested years (all stations)
    selected: int = 0  # ... of which belong to the requested stations
    downloaded: int = 0  # downloaded and written to bronze in this run
    skipped: int = 0  # unchanged since the last run, not downloaded again
    failed: int = 0  # download or write failed (previous bronze, if any, is kept)
    missing_upstream: int = 0  # in our manifest but no longer on S3
    failures: list[str] = field(default_factory=list)  # keys (or "list:<year>") that failed


class IntegrityError(RuntimeError):
    """Downloaded bytes do not match the object's MD5 ETag."""


def make_store(cfg: IngestCfg) -> Any:
    """Connection to the public NOAA bucket. `skip_signature`: anonymous access, no AWS account."""
    return S3Store(cfg.bucket, region=cfg.region, skip_signature=True)


def list_year(store: Any, year: int) -> list[SourceObject]:
    """obstore prefixes match whole path segments, so list the year folder ("2024")."""
    out: list[SourceObject] = []
    # obs.list yields batches (lists) of object descriptions, up to 1000 at a time.
    for batch in obs.list(store, prefix=str(year), chunk_size=1000):
        for meta in batch:
            key = meta["path"]
            if not key.endswith(".csv"):
                continue
            etag = (meta.get("e_tag") or "").strip('"')  # S3 returns the ETag wrapped in quotes
            out.append(SourceObject(key, int(meta["size"]), etag, meta["last_modified"]))
    return out


def needs_download(obj: SourceObject, previous: dict[str, Any] | None) -> bool:
    """True if the file is new, failed last time, or changed on S3 (different ETag or size)."""
    if previous is None or previous.get("status") != "ok":
        return True
    return previous.get("etag") != obj.etag or previous.get("size") != obj.size


def fetch_bytes(store: Any, key: str, retries: int) -> bytes:
    """Download one object, retrying with exponential backoff (0.5 s, 1 s, 2 s, ...)."""
    delay = 0.5
    for attempt in range(retries + 1):
        try:
            return bytes(obs.get(store, key).bytes())
        except Exception:  # obstore network errors have no stable exception type
            if attempt == retries:
                raise  # out of retries: let the caller record the failure
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def verify_md5(data: bytes, etag: str) -> str:
    """Return the MD5 of the downloaded bytes; raise IntegrityError if it contradicts the ETag.

    For files uploaded in one piece, S3's ETag IS the MD5, so a mismatch means a corrupted or
    truncated download. Other ETag formats (e.g. multipart uploads) can't be checked this way.
    """
    md5 = hashlib.md5(data).hexdigest()
    if _MD5_RE.match(etag) and md5 != etag:
        raise IntegrityError(f"MD5 {md5} does not match ETag {etag}")
    return md5


def bronze_path(data_root: Path, year: int, station_id: str) -> Path:
    """Where one station-year lives in bronze. "year=2024" folders (Hive-style partitions) let
    DuckDB and Polars filter by year from the folder name without opening the files."""
    return data_root / "bronze" / f"year={year}" / f"{station_id}.parquet"


def csv_to_bronze(data: bytes, key: str, md5: str, run_id: str) -> pl.DataFrame:
    """Parse the CSV bytes into a table, every column as text, plus provenance columns.

    `infer_schema_length=0` tells Polars not to guess types: everything stays a string, so no
    value is altered (e.g. "0350" keeps its leading zero, "+9999" is not turned into a number).
    """
    df = pl.read_csv(io.BytesIO(data), infer_schema_length=0)
    return df.with_columns(
        source_key=pl.lit(key), source_md5=pl.lit(md5), ingest_run_id=pl.lit(run_id)
    )


def ingest(
    store: Any,
    cfg: IngestCfg,
    data_root: Path,
    manifest: Manifest,
    stations: set[str],
    years: Iterable[int],
    run_id: str,
) -> IngestSummary:
    """Bring bronze up to date for the given stations and years.

    Step 1 (sequential): list each year on S3, pick our stations, mark vanished files, and decide
    which files need downloading. Step 2 (parallel): download, verify and write those files.
    A failure never aborts the run: it is recorded in the manifest and the summary instead.
    The caller saves the manifest afterwards.
    """
    summary = IngestSummary()
    todo: list[SourceObject] = []
    for year in years:
        try:
            listed = list_year(store, year)
        except Exception as exc:  # a failed listing never marks anything removed
            log.error("listing year %s failed: %s", year, exc)
            summary.failures.append(f"list:{year}")
            continue
        summary.listed += len(listed)
        chosen = [o for o in listed if o.station_id in stations]
        summary.selected += len(chosen)
        present = [o.key for o in chosen]
        # Files we have downloaded before for this year that are no longer listed on S3.
        stale = manifest.df.filter(
            (pl.col("year") == year)
            & pl.col("station_id").is_in(sorted(stations))
            & ~pl.col("key").is_in(present)
            & (pl.col("status") != "missing_upstream")
        )
        if stale.height:
            summary.missing_upstream += stale.height
            manifest.upsert(
                [{**r, "status": "missing_upstream"} for r in stale.iter_rows(named=True)]
            )
        for o in chosen:
            if needs_download(o, manifest.get(o.key)):
                todo.append(o)
            else:
                summary.skipped += 1

    def work(o: SourceObject) -> dict[str, Any]:
        """Download, verify and write ONE file; return its new manifest row (never raises)."""
        base = {
            "key": o.key,
            "station_id": o.station_id,
            "year": o.year,
            "size": o.size,
            "etag": o.etag,
            "last_modified": o.last_modified,
            "retrieved_at": utcnow(),
            "run_id": run_id,
        }
        try:
            data = fetch_bytes(store, o.key, cfg.retries)
            md5 = verify_md5(data, o.etag)
            df = csv_to_bronze(data, o.key, md5, run_id)
            path = bronze_path(data_root, o.year, o.station_id)
            atomic_write_parquet(
                df.to_arrow(),
                path,
                {"dcdr_layer": "bronze", "source_md5": md5, "source_key": o.key, "run_id": run_id},
            )
            return {
                **base,
                "md5": md5,
                "status": "ok",
                "rows": df.height,
                "bronze_path": str(path),
                "error": None,
            }
        except Exception as exc:
            # Keep what we knew about the last GOOD copy (its MD5, rows, path, run) so the existing
            # bronze file stays described correctly; only status and error change.
            prev = manifest.get(o.key) or {}
            log.error("download %s failed: %s", o.key, exc)
            return {
                **{k: prev.get(k) for k in MANIFEST_SCHEMA},
                **base,
                "md5": prev.get("md5"),
                "rows": prev.get("rows"),
                "bronze_path": prev.get("bronze_path"),
                "run_id": prev.get("run_id"),
                "status": "failed",
                "error": repr(exc)[:500],
            }

    # Downloads wait on the network, not the CPU, so threads give a big speed-up here.
    with ThreadPoolExecutor(max_workers=cfg.max_concurrency) as pool:
        results = list(pool.map(work, todo))
    manifest.upsert(results)  # update the manifest once, in the main thread (not thread-safe)
    for r in results:
        if r["status"] == "ok":
            summary.downloaded += 1
        else:
            summary.failed += 1
            summary.failures.append(r["key"])
    return summary
