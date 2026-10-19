"""Run state: safe file writes, the download log (manifest) and the run log.

Where this fits
---------------
Every stage uses these helpers. They answer three questions a production pipeline must answer:

1. "If the laptop crashes mid-write, is any file left half-written?"  -> No: `atomic_write_*`
2. "Which source files have we already downloaded, and did they change?" -> `Manifest`
3. "What ran, when, with which code and settings, and did it succeed?"  -> `append_run_record`

Everything is stored under `<data_root>/_state/`:
    sources.parquet   one row per NOAA source file (the manifest)
    runs.parquet      one row per command run (the run log; `dcdr status` shows the tail)
    runs/<run_id>/    the exact settings each run used (config.resolved.yaml)

Design choices
--------------
* Atomic writes: write to `<name>.tmp`, then `os.replace()` it onto the final name. The rename is
  all-or-nothing, so readers see either the old file or the new one, never a partial file.
* State is kept in Parquet (not a database server): no extra software, readable with Polars or
  DuckDB, and small enough to rewrite in full each time at pilot scale.
"""

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

# Columns of the manifest (_state/sources.parquet): one row per S3 object, i.e. per station-year.
MANIFEST_SCHEMA: dict[str, Any] = {
    "key": pl.String,  # S3 object key, e.g. "2024/40582099999.csv"
    "station_id": pl.String,
    "year": pl.Int32,
    "size": pl.Int64,  # bytes, as listed on S3
    "etag": pl.String,  # S3's version fingerprint; for NOAA files it equals the MD5
    "last_modified": pl.Datetime("us", "UTC"),
    "md5": pl.String,  # MD5 we computed from the downloaded bytes
    "retrieved_at": pl.Datetime("us", "UTC"),
    "status": pl.String,  # "ok", "failed" or "missing_upstream"
    "rows": pl.Int64,  # rows written to bronze
    "bronze_path": pl.String,
    "run_id": pl.String,  # run that last downloaded it
    "error": pl.String,  # error message when status is "failed"
}

# Columns of the run log (_state/runs.parquet): one row per command run.
RUNS_SCHEMA: dict[str, Any] = {
    "run_id": pl.String,
    "command": pl.String,  # "ingest", "process", "aggregate" or "run"
    "started_at": pl.Datetime("us", "UTC"),
    "finished_at": pl.Datetime("us", "UTC"),
    "status": pl.String,  # "ok" or "failed"
    "git_commit": pl.String,  # code version, when run from a git checkout
    "git_dirty": pl.Boolean,  # True if there were uncommitted code changes
    "dcdr_version": pl.String,  # package version (always available, also when installed)
    "config_hash": pl.String,  # fingerprint of the settings used
    "summary": pl.String,  # JSON: counts per stage (built, skipped, failed, ...)
}


def utcnow() -> datetime:
    """Current time in UTC (timezone-aware). All pipeline timestamps are UTC."""
    return datetime.now(UTC)


def new_run_id() -> str:
    """Sortable, unique run ID: UTC start time plus 6 random hex characters.

    Example: "20261017T231114Z-2b7d69". Sorting run IDs as text sorts them by time; the random
    suffix keeps two runs started in the same second apart.
    """
    return utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def _tmp_path(path: Path) -> Path:
    """Temporary name used while writing: "x.parquet" -> "x.parquet.tmp" (same folder)."""
    return path.with_name(path.name + ".tmp")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes so the target is never left half-written (temp file, then atomic rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)  # atomic: replaces any existing file in one step
    finally:
        if tmp.exists():  # only true if writing failed: remove the partial temp file
            tmp.unlink()


def atomic_write_parquet(
    table: pa.Table, path: Path, metadata: dict[str, str] | None = None
) -> None:
    """Write a Parquet file atomically, optionally adding key-value metadata to its schema.

    Parquet files can carry small text metadata in their footer. We use it to stamp each file with
    its provenance (layer, run ID, method version, config hash, source MD5), so a file explains
    itself without any external database. Read it back with `read_parquet_metadata`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if metadata:
        merged = dict(table.schema.metadata or {})  # keep any metadata already present
        merged.update({k.encode(): v.encode() for k, v in metadata.items()})  # Arrow wants bytes
        table = table.replace_schema_metadata(merged)
    tmp = _tmp_path(path)
    try:
        pq.write_table(table, tmp, compression="zstd")  # zstd: small files, fast to read
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_parquet_metadata(path: Path) -> dict[str, str]:
    """Our key-value metadata from a Parquet file's footer, without reading the data itself."""
    meta = pq.read_schema(path).metadata or {}
    # Skip Arrow's own internal entry ("ARROW:schema"); keep only the keys we wrote.
    return {k.decode(): v.decode() for k, v in meta.items() if not k.startswith(b"ARROW")}


def safe_listdir(path: Path) -> list[Path]:
    """Folder contents, sorted, ignoring hidden files and unfinished `.tmp` writes.

    On the exFAT SSD, macOS creates hidden "._name" companion files next to every file; those and
    leftover temp files must never be mistaken for data.
    """
    if not path.exists():
        return []
    return sorted(
        p for p in path.iterdir() if not p.name.startswith(".") and not p.name.endswith(".tmp")
    )


class Manifest:
    """Source inventory: one row per S3 key (spec §7.1.3).

    Loaded into memory as a Polars DataFrame, updated with `upsert`, written back with `save`.
    Ingest uses it to download only files that are new or changed since the last run.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        # First run: no file yet, so start with an empty table that has the right columns.
        self.df = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=MANIFEST_SCHEMA)

    def get(self, key: str) -> dict[str, Any] | None:
        """The manifest row for one S3 key as a dict, or None if we have never seen it."""
        rows = self.df.filter(pl.col("key") == key)
        return rows.row(0, named=True) if rows.height else None

    def upsert(self, records: list[dict[str, Any]]) -> None:
        """Insert new rows or replace existing ones with the same key ("update or insert")."""
        if not records:
            return
        rows = [{k: r.get(k) for k in MANIFEST_SCHEMA} for r in records]  # keep schema columns
        new = pl.DataFrame(rows, schema=MANIFEST_SCHEMA)
        # Append, then keep only the LAST row per key: the new record wins over the old one.
        self.df = pl.concat([self.df, new]).unique(subset="key", keep="last", maintain_order=True)

    def save(self) -> None:
        """Write the manifest back to disk (atomically)."""
        atomic_write_parquet(self.df.to_arrow(), self.path)


def append_run_record(runs_path: Path, record: dict[str, Any]) -> None:
    """Add one row to the run log (_state/runs.parquet), creating the file on the first run."""
    row = pl.DataFrame([{k: record.get(k) for k in RUNS_SCHEMA}], schema=RUNS_SCHEMA)
    if runs_path.exists():  # "diagonal" lets logs written before a new column still append
        df = pl.concat([pl.read_parquet(runs_path), row], how="diagonal").select(list(RUNS_SCHEMA))
    else:
        df = row
    atomic_write_parquet(df.to_arrow(), runs_path)


def git_revision(repo: Path) -> tuple[str, bool]:
    """(commit hash, has uncommitted changes) of the git checkout containing `repo`.

    Recorded with every run so results can be traced to the exact code. Returns ("unknown", True)
    when git is unavailable or the code is not in a git checkout (e.g. installed from a wheel).
    """
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],  # -C: run git as if inside `repo`
            capture_output=True,
            text=True,
            check=True,  # raise CalledProcessError if git fails (e.g. not a repository)
        ).stdout.strip()
        porcelain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],  # empty output = clean tree
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return commit, bool(porcelain)
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True
