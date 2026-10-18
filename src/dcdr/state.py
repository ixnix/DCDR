"""Run state: atomic writes, safe directory listing, source manifest and run records."""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

MANIFEST_SCHEMA: dict[str, Any] = {
    "key": pl.String,
    "station_id": pl.String,
    "year": pl.Int32,
    "size": pl.Int64,
    "etag": pl.String,
    "last_modified": pl.Datetime("us", "UTC"),
    "md5": pl.String,
    "retrieved_at": pl.Datetime("us", "UTC"),
    "status": pl.String,
    "rows": pl.Int64,
    "bronze_path": pl.String,
    "run_id": pl.String,
    "error": pl.String,
}

RUNS_SCHEMA: dict[str, Any] = {
    "run_id": pl.String,
    "command": pl.String,
    "started_at": pl.Datetime("us", "UTC"),
    "finished_at": pl.Datetime("us", "UTC"),
    "status": pl.String,
    "git_commit": pl.String,
    "git_dirty": pl.Boolean,
    "dcdr_version": pl.String,
    "config_hash": pl.String,
    "summary": pl.String,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_run_id() -> str:
    return utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def _tmp_path(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_write_parquet(
    table: pa.Table, path: Path, metadata: dict[str, str] | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if metadata:
        merged = dict(table.schema.metadata or {})
        merged.update({k.encode(): v.encode() for k, v in metadata.items()})
        table = table.replace_schema_metadata(merged)
    tmp = _tmp_path(path)
    try:
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_parquet_metadata(path: Path) -> dict[str, str]:
    meta = pq.read_schema(path).metadata or {}
    return {k.decode(): v.decode() for k, v in meta.items() if not k.startswith(b"ARROW")}


def safe_listdir(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(
        p for p in path.iterdir() if not p.name.startswith(".") and not p.name.endswith(".tmp")
    )


class Manifest:
    """Source inventory: one row per S3 key (spec §7.1.3)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.df = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=MANIFEST_SCHEMA)

    def get(self, key: str) -> dict[str, Any] | None:
        rows = self.df.filter(pl.col("key") == key)
        return rows.row(0, named=True) if rows.height else None

    def upsert(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        rows = [{k: r.get(k) for k in MANIFEST_SCHEMA} for r in records]
        new = pl.DataFrame(rows, schema=MANIFEST_SCHEMA)
        self.df = pl.concat([self.df, new]).unique(subset="key", keep="last", maintain_order=True)

    def save(self) -> None:
        atomic_write_parquet(self.df.to_arrow(), self.path)


def append_run_record(runs_path: Path, record: dict[str, Any]) -> None:
    row = pl.DataFrame([{k: record.get(k) for k in RUNS_SCHEMA}], schema=RUNS_SCHEMA)
    if runs_path.exists():  # "diagonal" lets logs written before a new column still append
        df = pl.concat([pl.read_parquet(runs_path), row], how="diagonal").select(list(RUNS_SCHEMA))
    else:
        df = row
    atomic_write_parquet(df.to_arrow(), runs_path)


def git_revision(repo: Path) -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        porcelain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return commit, bool(porcelain)
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True
