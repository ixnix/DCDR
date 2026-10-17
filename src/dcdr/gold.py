"""Gold layer: product registry, versioned full rebuilds and the CURRENT pointer (spec §6.1, §7.3)."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import duckdb

from dcdr.config import REPO_ROOT, Settings, silver_config_hash
from dcdr.silver import METHOD_VERSION
from dcdr.state import atomic_write_bytes, read_parquet_metadata, safe_listdir

SQL_DIR = REPO_ROOT / "sql"


@dataclass(frozen=True)
class Product:
    name: str
    sql_file: str
    checks: dict[str, str]  # name -> SQL returning a count that must be 0


PRODUCTS = [
    Product(
        "daily",
        "daily.sql",
        {
            "n_slots_le_8": "SELECT count(*) FROM daily WHERE n_slots > 8",
            "pw_le_slots": "SELECT count(*) FROM daily WHERE n_slots_pw > n_slots",
            "valid_day_consistent": (
                "SELECT count(*) FROM daily WHERE valid_day <> (n_slots_pw >= getvariable('min_slots'))"
            ),
            "dusty_needs_slots": "SELECT count(*) FROM daily WHERE dusty_day_synoptic AND n_slots = 0",
            "unique_station_day": (
                "SELECT count(*) FROM (SELECT station_id, lmst_date FROM daily "
                "GROUP BY ALL HAVING count(*) > 1)"
            ),
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
    out: set[tuple[str, str]] = set()
    for p in paths:
        meta = read_parquet_metadata(p)
        out.add((meta.get("method_version", ""), meta.get("config_hash", "")))
    return out


def _sql_path(path: Path) -> str:
    text = str(path)
    if "'" in text:
        raise ValueError(f"unsupported character in path: {text}")
    return text


def build_gold(settings: Settings, silver_paths: list[Path], run_id: str) -> Path:
    if not silver_paths:
        raise ValueError("no silver files to build gold from")
    versions = _silver_versions(silver_paths)
    if len(versions) != 1:
        raise MixedSilverVersionsError(f"silver files have mixed versions: {sorted(versions)}")
    expected = (METHOD_VERSION, silver_config_hash(settings))
    if versions != {expected}:
        raise StaleSilverError(
            f"silver is {versions.pop()}, code expects {expected}; rebuild silver"
        )
    gold_root = settings.data_root / "gold"
    out_dir = gold_root / "versions" / run_id
    con = duckdb.connect()
    con.read_parquet([_sql_path(p) for p in silver_paths], union_by_name=True).create_view("silver")
    con.execute(f"SET VARIABLE min_slots = {int(settings.products.min_slots_per_day)}")
    try:
        lineage = {"silver_method_version": expected[0], "silver_config_hash": expected[1]}
        atomic_write_bytes(out_dir / "silver_version.json", json.dumps(lineage, indent=2).encode())
        for product in PRODUCTS:
            target = out_dir / product.name / "part-000.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            sql = (SQL_DIR / product.sql_file).read_text()
            con.execute(f"COPY ({sql}) TO '{_sql_path(target)}' (FORMAT parquet, COMPRESSION zstd)")
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
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    finally:
        con.close()
    atomic_write_bytes(gold_root / "CURRENT", run_id.encode())
    prune_versions(gold_root, settings.products.gold_keep_versions)
    return out_dir


def current_gold_dir(settings: Settings) -> Path | None:
    pointer = settings.data_root / "gold" / "CURRENT"
    if not pointer.exists():
        return None
    return settings.data_root / "gold" / "versions" / pointer.read_text().strip()


def prune_versions(gold_root: Path, keep: int) -> None:
    active = (gold_root / "CURRENT").read_text().strip()
    versions = [p for p in safe_listdir(gold_root / "versions") if p.is_dir()]
    stale = [p for p in versions if p.name != active][: max(0, len(versions) - keep)]
    for p in stale:
        shutil.rmtree(p)
