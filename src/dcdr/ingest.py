"""List NOAA ISD objects on S3, download changed station-years, write bronze (spec §7.1)."""

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
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class SourceObject:
    key: str
    size: int
    etag: str
    last_modified: datetime

    @property
    def station_id(self) -> str:
        return Path(self.key).stem

    @property
    def year(self) -> int:
        return int(self.key.split("/", 1)[0])


@dataclass
class IngestSummary:
    listed: int = 0
    selected: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    missing_upstream: int = 0
    failures: list[str] = field(default_factory=list)


class IntegrityError(RuntimeError):
    """Downloaded bytes do not match the object's MD5 ETag."""


def make_store(cfg: IngestCfg) -> Any:
    return S3Store(cfg.bucket, region=cfg.region, skip_signature=True)


def list_year(store: Any, year: int) -> list[SourceObject]:
    """obstore prefixes match whole path segments, so list the year folder ("2024")."""
    out: list[SourceObject] = []
    for batch in obs.list(store, prefix=str(year), chunk_size=1000):
        for meta in batch:
            key = meta["path"]
            if not key.endswith(".csv"):
                continue
            etag = (meta.get("e_tag") or "").strip('"')
            out.append(SourceObject(key, int(meta["size"]), etag, meta["last_modified"]))
    return out


def needs_download(obj: SourceObject, previous: dict[str, Any] | None) -> bool:
    if previous is None or previous.get("status") != "ok":
        return True
    return previous.get("etag") != obj.etag or previous.get("size") != obj.size


def fetch_bytes(store: Any, key: str, retries: int) -> bytes:
    delay = 0.5
    for attempt in range(retries + 1):
        try:
            return bytes(obs.get(store, key).bytes())
        except Exception:  # obstore network errors have no stable exception type
            if attempt == retries:
                raise
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def verify_md5(data: bytes, etag: str) -> str:
    md5 = hashlib.md5(data).hexdigest()
    if _MD5_RE.match(etag) and md5 != etag:
        raise IntegrityError(f"MD5 {md5} does not match ETag {etag}")
    return md5


def bronze_path(data_root: Path, year: int, station_id: str) -> Path:
    return data_root / "bronze" / f"year={year}" / f"{station_id}.parquet"


def csv_to_bronze(data: bytes, key: str, md5: str, run_id: str) -> pl.DataFrame:
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

    with ThreadPoolExecutor(max_workers=cfg.max_concurrency) as pool:
        results = list(pool.map(work, todo))
    manifest.upsert(results)
    for r in results:
        if r["status"] == "ok":
            summary.downloaded += 1
        else:
            summary.failed += 1
            summary.failures.append(r["key"])
    return summary
