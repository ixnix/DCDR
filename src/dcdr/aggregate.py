"""Aggregate step (silver → gold): product registry, versioned full rebuilds and the CURRENT pointer (spec §6.1, §7.3).

Where this fits
---------------
Last stage: `dcdr aggregate` (or `dcdr run`). Input is every silver file (one row per report);
output is GOLD, the analysis-ready tables (for now `daily`: one row per station per local day).

How gold is built
-----------------
1. Check that all silver files were built by the current code and settings (refuse otherwise).
2. Open DuckDB (an in-process SQL engine, no server) and expose all silver files as one view.
3. For each product: run its SQL file (src/dcdr/sql/<name>.sql), write the result to Parquet,
   then run the product's checks (each check is a query that must return 0).
4. Only if everything passed: point `gold/CURRENT` at the new version, prune old versions.

Why versions + a CURRENT pointer
--------------------------------
Each build goes into a NEW folder, `gold/versions/<run_id>/`. Readers follow `gold/CURRENT` (a tiny
text file holding the active run ID). A failed or half-finished build is never visible: the pointer
moves only after all checks pass, and the switch is a single atomic file write. The previous
version stays on disk, so rolling back is just editing CURRENT.

Why SQL for gold: aggregation (GROUP BY station and day, counts, means) is what SQL is best at, and a
separate .sql file can be read and tested on its own, also by people who don't read Python.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import duckdb

from dcdr.config import Settings, silver_config_hash
from dcdr.process import METHOD_VERSION
from dcdr.state import atomic_write_bytes, read_parquet_metadata, safe_listdir

# SQL files ship inside the package (src/dcdr/sql/), found the same way as default.yaml.
SQL_DIR = files("dcdr") / "sql"


@dataclass(frozen=True)
class Product:
    """One gold table: its name (also its folder), the SQL that builds it, and its checks."""

    name: str
    sql_file: str
    checks: dict[str, str]  # name -> SQL returning a count that must be 0


# The registry of gold products. Adding a product = adding an entry here plus a .sql file.
PRODUCTS = [
    Product(
        "daily",
        "daily.sql",
        {
            # A day has 8 synoptic slots (00, 03, ..., 21 UTC), so more than 8 means double counting.
            "n_slots_le_8": "SELECT count(*) FROM daily WHERE n_slots > 8",
            # Slots with weather codes are a subset of all slots.
            "pw_le_slots": "SELECT count(*) FROM daily WHERE n_slots_pw > n_slots",
            # valid_day must follow its definition exactly.
            "valid_day_consistent": (
                "SELECT count(*) FROM daily WHERE valid_day <> (n_slots_pw >= getvariable('min_slots'))"
            ),
            # A synoptic dusty day needs at least one synoptic slot.
            "dusty_needs_slots": "SELECT count(*) FROM daily WHERE dusty_day_synoptic AND n_slots = 0",
            # Exactly one row per station and day.
            "unique_station_day": (
                "SELECT count(*) FROM (SELECT station_id, lmst_date FROM daily "
                "GROUP BY ALL HAVING count(*) > 1)"
            ),
            # Day-to-day changes are only computed between two valid days.
            "d_t_only_valid": "SELECT count(*) FROM daily WHERE d_t_mean IS NOT NULL AND NOT valid_day",
        },
    ),
]


class MixedSilverVersionsError(RuntimeError):
    """Silver inputs were built with different method versions or configs."""


class StaleSilverError(RuntimeError):
    """Silver was built by another method version or silver config than the running code."""


class GoldCheckError(RuntimeError):
    """A gold product failed a validation check."""


def _silver_versions(paths: list[Path]) -> set[tuple[str, str]]:
    """The distinct (method_version, config_hash) pairs found in the silver files' metadata."""
    out: set[tuple[str, str]] = set()
    for p in paths:
        meta = read_parquet_metadata(p)
        out.add((meta.get("method_version", ""), meta.get("config_hash", "")))
    return out


def _sql_path(path: Path) -> str:
    """A path for use inside an SQL string literal. A quote (') would break or alter the SQL
    statement, so such paths are refused rather than escaped."""
    text = str(path)
    if "'" in text:
        raise ValueError(f"unsupported character in path: {text}")
    return text


def build_gold(settings: Settings, silver_paths: list[Path], run_id: str) -> Path:
    """Build every gold product into gold/versions/<run_id>/, check it, then activate it.

    Raises (and leaves the previous gold version active) if the silver inputs are mixed or
    stale, or if any product check fails. Returns the new version's folder.
    """
    if not silver_paths:
        raise ValueError("no silver files to build gold from")
    # Guard 1: all silver files must come from the same code and settings ...
    versions = _silver_versions(silver_paths)
    if len(versions) != 1:
        raise MixedSilverVersionsError(f"silver files have mixed versions: {sorted(versions)}")
    # Guard 2: ... and that must be the code and settings running now.
    expected = (METHOD_VERSION, silver_config_hash(settings))
    if versions != {expected}:
        raise StaleSilverError(
            f"silver is {versions.pop()}, code expects {expected}; re-run `dcdr process --force`"
        )
    gold_root = settings.data_root / "gold"
    out_dir = gold_root / "versions" / run_id
    con = duckdb.connect()  # in-memory database; reads the Parquet files directly from disk
    # One view over all silver files. union_by_name: files may differ in optional columns.
    con.read_parquet([_sql_path(p) for p in silver_paths], union_by_name=True).create_view("silver")
    # Make the valid-day threshold available to the SQL as getvariable('min_slots').
    con.execute(f"SET VARIABLE min_slots = {int(settings.products.min_slots_per_day)}")
    try:
        # Record which silver this gold version was built from (lineage).
        lineage = {"silver_method_version": expected[0], "silver_config_hash": expected[1]}
        atomic_write_bytes(out_dir / "silver_version.json", json.dumps(lineage, indent=2).encode())
        for product in PRODUCTS:
            target = out_dir / product.name / "part-000.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            sql = (SQL_DIR / product.sql_file).read_text()
            # COPY (query) TO file: DuckDB streams the query result straight into Parquet.
            con.execute(f"COPY ({sql}) TO '{_sql_path(target)}' (FORMAT parquet, COMPRESSION zstd)")
            # Re-read the written file as a view so the checks test exactly what was saved.
            con.read_parquet(_sql_path(target)).create_view(product.name)
            failures = {}
            for name, query in product.checks.items():
                row = con.execute(query).fetchone()
                count = row[0] if row else 0
                if count:
                    failures[name] = count
            if failures:
                raise GoldCheckError(f"{product.name}: {failures}")
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)  # discard the unfinished version
        raise
    finally:
        con.close()
    # Everything passed: switch readers to the new version, then delete old ones.
    atomic_write_bytes(gold_root / "CURRENT", run_id.encode())
    prune_versions(gold_root, settings.products.gold_keep_versions)
    return out_dir


def current_gold_dir(settings: Settings) -> Path | None:
    """Folder of the active gold version, or None if gold has never been built."""
    pointer = settings.data_root / "gold" / "CURRENT"
    if not pointer.exists():
        return None
    return settings.data_root / "gold" / "versions" / pointer.read_text().strip()


def prune_versions(gold_root: Path, keep: int) -> None:
    """Delete the oldest gold versions so at most `keep` remain; never delete the active one.

    Version folders are named by run ID, which starts with the UTC time, so sorting by name is
    sorting by age (safe_listdir sorts and skips hidden/temporary entries).
    """
    active = (gold_root / "CURRENT").read_text().strip()
    versions = [p for p in safe_listdir(gold_root / "versions") if p.is_dir()]
    stale = [p for p in versions if p.name != active][: max(0, len(versions) - keep)]
    for p in stale:
        shutil.rmtree(p, onexc=_ignore_vanished)


def _ignore_vanished(func: object, path: str, exc: BaseException) -> None:
    """rmtree error handler: a file that is already gone is fine; any other error is raised.

    On exFAT disks macOS stores metadata in hidden "._name" twins and deletes the twin together
    with "name". rmtree lists a folder before deleting, so it later finds the twin missing.
    """
    if not isinstance(exc, FileNotFoundError):
        raise exc
