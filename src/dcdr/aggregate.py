"""Aggregate step (silver → gold): product registry, versioned full rebuilds and the CURRENT pointer (spec §6.1, §7.3).

Where this fits
---------------
Last stage: `dcdr aggregate` (or `dcdr run`). Input is every silver file (one row per report);
output is GOLD, the analysis-ready tables: `daily` (one row per station per local day) and
`three_hourly` (one row per station per 3-hour UTC bin), both built from silver, and the
statistics `monthly` (from `daily`), `yearly` and `decadal` (from `monthly`): a product reads the
products before it in the list as views.

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
from dcdr.stations import file_md5, load_isd_history, load_overrides

# SQL files ship inside the package (src/dcdr/sql/), found the same way as default.yaml.
SQL_DIR = files("dcdr") / "sql"


@dataclass(frozen=True)
class Product:
    """One gold table: its name (also its folder), the SQL that builds it, and its checks."""

    name: str
    sql_file: str
    checks: dict[str, str]  # name -> SQL returning a count that must be 0


# The dusty-day definitions counted in the statistics tables: the column suffix of each (primary =
# all sources; each source group judged alone: SYNOP, METAR, other; visibility <= 5 km; explicit
# observations only).
STAT_VARIANTS = ("", "_synop", "_metar", "_other", "_vis5", "_explicit")


def _count_order_checks(table: str) -> dict[str, str]:
    """Checks shared by the statistics tables: for each definition, dusty valid days <= valid days
    <= observed days <= days <= calendar days, and dusty valid days <= dusty days on any day; and
    for the three rates: null exactly where undefined, the lower bound within 0-1, and
    dusty_freq = f / (f + validity_ratio * (1 - f)) with f = dusty_freq_obs (docs/rules.md 11.1),
    which ties the three rates to each other."""
    checks = {}
    for v in STAT_VARIANTS:
        checks[f"count_order{v or '_primary'}"] = (
            f"SELECT count(*) FROM {table} WHERE NOT (n_dusty_days{v} <= n_valid_days{v} "
            f"AND n_valid_days{v} <= n_obs_days{v} AND n_obs_days{v} <= n_days "
            f"AND n_days <= n_calendar_days AND n_dusty_days{v} <= n_dusty_days_obs{v})"
        )
    checks["freq_null_iff_no_valid_day"] = (
        f"SELECT count(*) FROM {table} WHERE (dusty_freq IS NULL) <> (n_valid_days = 0)"
    )
    checks["freq_obs_null_iff_no_observed_day"] = (
        f"SELECT count(*) FROM {table} WHERE (dusty_freq_obs IS NULL) <> (n_obs_days = 0)"
    )
    checks["freq_obs_in_unit_range"] = (
        f"SELECT count(*) FROM {table} WHERE dusty_freq_obs < 0 OR dusty_freq_obs > 1"
    )
    checks["validity_ratio_null_iff_undefined"] = (
        f"SELECT count(*) FROM {table} WHERE (validity_ratio IS NULL) <> "
        "(n_dusty_days_obs = 0 OR n_obs_days = n_dusty_days_obs OR n_dusty_days = 0)"
    )
    checks["validity_ratio_matches_freqs"] = (
        f"SELECT count(*) FROM {table} WHERE validity_ratio IS NOT NULL AND abs(dusty_freq - "
        "dusty_freq_obs / (dusty_freq_obs + validity_ratio * (1 - dusty_freq_obs))) > 1e-9"
    )
    return checks


# The main counts compared when one statistics table must add up to another.
_SUM_CHECK_COLS = ("n_days", "n_valid_days", "n_dusty_days", "n_dusty_days_obs")


def _decadal_parts_check(periods: list[str]) -> str:
    """A check that the given decadal periods (the 12 months, or the 4 seasons) add up to the
    `annual` row of each station and decade."""
    sums = ", ".join(f"sum({c}) AS {c}" for c in _SUM_CHECK_COLS)
    differ = " OR ".join(f"p.{c} IS DISTINCT FROM a.{c}" for c in _SUM_CHECK_COLS)
    return (
        f"SELECT count(*) FROM (SELECT station_id, decade, {sums} FROM decadal "
        f"WHERE period IN {tuple(periods)} GROUP BY ALL) p "
        "FULL JOIN (SELECT * FROM decadal WHERE period = 'annual') a USING (station_id, decade) "
        f"WHERE {differ}"
    )


# The registry of gold products. Adding a product = adding an entry here plus a .sql file.
# Products run in this order, and each can read the products before it (as a view of its name).
PRODUCTS = [
    Product(
        # Station positions (docs/rules.md, "Station positions"): built first, from every silver
        # report; build_gold then narrows the `silver` view to the stations in gold.
        "stations",
        "stations.sql",
        {
            "unique_station": (
                "SELECT count(*) FROM (SELECT station_id FROM stations "
                "GROUP BY ALL HAVING count(*) > 1)"
            ),
            "status_known": (
                "SELECT count(*) FROM stations WHERE status NOT IN ('agree', 'moved', "
                "'conflict', 'resolved_isd_history', 'unverified', 'override') "
                "OR computed_status NOT IN ('agree', 'moved', 'conflict', "
                "'resolved_isd_history', 'unverified')"
            ),
            # A station in gold has a longitude for its local time; a conflict is never in gold.
            "in_gold_has_trusted_lon": (
                "SELECT count(*) FROM stations WHERE in_gold AND trusted_lon IS NULL"
            ),
            "conflict_not_in_gold": (
                "SELECT count(*) FROM stations WHERE status = 'conflict' AND in_gold"
            ),
            # In gold exactly when the position is usable and the station is dusty.
            "in_gold_definition": (
                "SELECT count(*) FROM stations WHERE in_gold <> (position_ok AND is_dusty_station)"
            ),
        },
    ),
    Product(
        # Named without a leading digit: the name is also the DuckDB view the checks query.
        "three_hourly",
        "three_hourly.sql",
        {
            # Exactly one row per station and bin.
            "unique_station_bin": (
                "SELECT count(*) FROM (SELECT station_id, bin_time FROM three_hourly "
                "GROUP BY ALL HAVING count(*) > 1)"
            ),
            # Bins start at 00, 03, ..., 21 UTC on the full hour.
            "bin_aligned": (
                "SELECT count(*) FROM three_hourly WHERE hour(bin_time) % 3 <> 0 "
                "OR minute(bin_time) <> 0 OR second(bin_time) <> 0"
            ),
            # The state report lies inside its bin.
            "state_in_bin": (
                "SELECT count(*) FROM three_hourly WHERE state_time_utc < bin_time "
                "OR state_time_utc >= bin_time + INTERVAL 3 HOUR"
            ),
            # Observed reports are a subset of all reports; the type counts add up.
            "counts_consistent": (
                "SELECT count(*) FROM three_hourly WHERE n_reports_pw > n_reports "
                "OR n_synop + n_metar + n_speci + n_other <> n_reports"
            ),
            # No dust verdict without an observation, and always one with an observation.
            "dust_null_iff_unobserved": (
                "SELECT count(*) FROM three_hourly WHERE (dusty IS NULL) <> (NOT observed)"
            ),
            # A source's verdict needs that source's observation; the bin is dusty exactly when a
            # source saw dust; dust at <= 5 km is dust.
            "source_dust_needs_observation": (
                "SELECT count(*) FROM three_hourly WHERE NOT observed AND (dust_synop IS NOT NULL "
                "OR dust_metar IS NOT NULL OR dust_other IS NOT NULL OR dust_vis5 IS NOT NULL)"
            ),
            "dusty_is_any_source": (
                "SELECT count(*) FROM three_hourly WHERE observed AND dusty <> (coalesce("
                "dust_synop, false) OR coalesce(dust_metar, false) OR coalesce(dust_other, false))"
            ),
            "vis5_implies_dusty": (
                "SELECT count(*) FROM three_hourly WHERE dust_vis5 AND NOT dusty"
            ),
            # Only stations in gold (docs/rules.md 4.3).
            "stations_in_gold": (
                "SELECT count(*) FROM three_hourly WHERE station_id NOT IN "
                "(SELECT station_id FROM stations WHERE in_gold)"
            ),
            # A first-dust time exactly when the bin is dusty.
            "first_dust_iff_dusty": (
                "SELECT count(*) FROM three_hourly "
                "WHERE (first_dust_time_utc IS NOT NULL) <> coalesce(dusty, false)"
            ),
        },
    ),
    Product(
        # Built from the `three_hourly` bins (method 2.0.0), so it runs after them.
        "daily",
        "daily.sql",
        {
            # A day has 8 bins (one LMST offset per station), so more than 8 means double counting.
            "n_bins_le_8": "SELECT count(*) FROM daily WHERE n_bins > 8",
            # Observed bins are a subset of all bins; explicit, per-source, inferred, implied and
            # vis5-judgeable bins are subsets of observed bins.
            "obs_le_bins": "SELECT count(*) FROM daily WHERE n_bins_obs > n_bins",
            "subsets_le_obs": (
                "SELECT count(*) FROM daily WHERE n_bins_obs_explicit > n_bins_obs "
                "OR n_bins_obs_synop > n_bins_obs OR n_bins_obs_metar > n_bins_obs "
                "OR n_bins_obs_other > n_bins_obs OR n_bins_nil_inferred > n_bins_obs "
                "OR n_bins_nil_metar > n_bins_obs OR n_bins_obs_vis5 > n_bins_obs"
            ),
            # A source sees dust only in bins it observed.
            "source_dust_le_source_obs": (
                "SELECT count(*) FROM daily WHERE n_bins_dust_synop > n_bins_obs_synop "
                "OR n_bins_dust_metar > n_bins_obs_metar OR n_bins_dust_other > n_bins_obs_other"
            ),
            # A day is dusty exactly when one source alone calls it dusty; every dusty bin was
            # seen by at least one source, and each source's dusty bins are dusty bins.
            "dusty_is_any_source": (
                "SELECT count(*) FROM daily WHERE dusty_day "
                "<> (dusty_day_synop OR dusty_day_metar OR dusty_day_other)"
            ),
            "bins_dusty_bounds": (
                "SELECT count(*) FROM daily WHERE n_bins_dust_synop > n_bins_dusty "
                "OR n_bins_dust_metar > n_bins_dusty OR n_bins_dust_other > n_bins_dusty "
                "OR n_bins_dusty > n_bins_dust_synop + n_bins_dust_metar + n_bins_dust_other"
            ),
            "vis5_implies_dusty": "SELECT count(*) FROM daily WHERE dusty_day_vis5 AND NOT dusty_day",
            # Dusty bins are observed bins, and a day has some exactly when it is dusty.
            "bins_dusty_le_obs": "SELECT count(*) FROM daily WHERE n_bins_dusty > n_bins_obs",
            "bins_dusty_iff_dusty": (
                "SELECT count(*) FROM daily WHERE (n_bins_dusty > 0) <> dusty_day"
            ),
            # Every validity follows its definition.
            "validity_consistent": (
                "SELECT count(*) FROM daily WHERE "
                "valid_day <> (n_bins_obs >= getvariable('min_bins')) "
                "OR valid_day_explicit <> (n_bins_obs_explicit >= getvariable('min_bins')) "
                "OR valid_day_synop <> (n_bins_obs_synop >= getvariable('min_bins')) "
                "OR valid_day_metar <> (n_bins_obs_metar >= getvariable('min_bins')) "
                "OR valid_day_other <> (n_bins_obs_other >= getvariable('min_bins')) "
                "OR valid_day_vis5 <> (n_bins_obs_vis5 >= getvariable('min_bins'))"
            ),
            # Exactly one row per station and day.
            "unique_station_day": (
                "SELECT count(*) FROM (SELECT station_id, lmst_date FROM daily "
                "GROUP BY ALL HAVING count(*) > 1)"
            ),
            # Only stations in gold (docs/rules.md 4.3).
            "stations_in_gold": (
                "SELECT count(*) FROM daily WHERE station_id NOT IN "
                "(SELECT station_id FROM stations WHERE in_gold)"
            ),
            # Day-to-day changes are only computed between two valid days.
            "d_t_only_valid": "SELECT count(*) FROM daily WHERE d_t_mean IS NOT NULL AND NOT valid_day",
        },
    ),
    Product(
        # Statistics from `daily` (docs/rules.md section 11).
        "monthly",
        "monthly.sql",
        {
            # Exactly one row per station and month.
            "unique_station_month": (
                "SELECT count(*) FROM (SELECT station_id, year, month FROM monthly "
                "GROUP BY ALL HAVING count(*) > 1)"
            ),
            # The main counts recomputed from daily match (no day lost or counted twice).
            "matches_daily": (
                "SELECT count(*) FROM (SELECT station_id, year(lmst_date) AS year, "
                "month(lmst_date) AS month, count(*) AS n_days, "
                "count(*) FILTER (WHERE valid_day) AS n_valid_days, "
                "count(*) FILTER (WHERE valid_day AND dusty_day) AS n_dusty_days "
                "FROM daily GROUP BY ALL) d FULL JOIN monthly m USING (station_id, year, month) "
                "WHERE d.n_days IS DISTINCT FROM m.n_days "
                "OR d.n_valid_days IS DISTINCT FROM m.n_valid_days "
                "OR d.n_dusty_days IS DISTINCT FROM m.n_dusty_days"
            ),
            **_count_order_checks("monthly"),
        },
    ),
    Product(
        "yearly",
        "yearly.sql",
        {
            # Exactly one row per station and year.
            "unique_station_year": (
                "SELECT count(*) FROM (SELECT station_id, year FROM yearly "
                "GROUP BY ALL HAVING count(*) > 1)"
            ),
            # The main counts equal the sums of the monthly counts.
            "matches_monthly": (
                "SELECT count(*) FROM (SELECT station_id, year, sum(n_days) AS n_days, "
                "sum(n_obs_days) AS n_obs_days, sum(n_valid_days) AS n_valid_days, "
                "sum(n_dusty_days) AS n_dusty_days, sum(n_dusty_days_obs) AS n_dusty_days_obs, "
                "sum(n_bins_obs) AS n_bins_obs FROM monthly GROUP BY ALL) m "
                "FULL JOIN yearly y USING (station_id, year) "
                "WHERE m.n_days IS DISTINCT FROM y.n_days "
                "OR m.n_obs_days IS DISTINCT FROM y.n_obs_days "
                "OR m.n_valid_days IS DISTINCT FROM y.n_valid_days "
                "OR m.n_dusty_days IS DISTINCT FROM y.n_dusty_days "
                "OR m.n_dusty_days_obs IS DISTINCT FROM y.n_dusty_days_obs "
                "OR m.n_bins_obs IS DISTINCT FROM y.n_bins_obs"
            ),
            # Covered months are valid months, valid months are observed months, at most 12.
            "months_le_12": (
                "SELECT count(*) FROM yearly WHERE NOT (n_months_covered <= n_months_valid "
                "AND n_months_valid <= n_months_obs AND n_months_obs <= 12)"
            ),
            **_count_order_checks("yearly"),
        },
    ),
    Product(
        "decadal",
        "decadal.sql",
        {
            # Exactly one row per station, decade and period.
            "unique_station_decade_period": (
                "SELECT count(*) FROM (SELECT station_id, decade, period FROM decadal "
                "GROUP BY ALL HAVING count(*) > 1)"
            ),
            # Decades start in a year ending in 1; periods are the year, a season or a month.
            "decade_aligned": "SELECT count(*) FROM decadal WHERE decade % 10 <> 1",
            "period_known": (
                "SELECT count(*) FROM decadal WHERE period NOT IN ('annual', 'DJF', 'MAM', "
                "'JJA', 'SON', '01', '02', '03', '04', '05', '06', '07', '08', '09', '10', "
                "'11', '12')"
            ),
            # The annual rows equal the yearly counts summed over the decade ...
            "annual_matches_yearly": (
                "SELECT count(*) FROM (SELECT station_id, year - (year - 1) % 10 AS decade, "
                + ", ".join(f"sum({c}) AS {c}" for c in _SUM_CHECK_COLS)
                + " FROM yearly GROUP BY ALL) y "
                "FULL JOIN (SELECT * FROM decadal WHERE period = 'annual') a "
                "USING (station_id, decade) WHERE "
                + " OR ".join(f"y.{c} IS DISTINCT FROM a.{c}" for c in _SUM_CHECK_COLS)
            ),
            # ... and so do the 12 months and the 4 seasons.
            "months_sum_to_annual": _decadal_parts_check([f"{m:02d}" for m in range(1, 13)]),
            "seasons_sum_to_annual": _decadal_parts_check(["DJF", "MAM", "JJA", "SON"]),
            # Years with dust are valid years, valid years are observed years, at most 10.
            "years_le_10": (
                "SELECT count(*) FROM decadal WHERE NOT (n_dust_years <= n_years_valid "
                "AND n_years_valid <= n_years_obs AND n_years_obs <= 10 "
                "AND n_dust_years <= n_dust_years_obs)"
            ),
            # The mean exists exactly when the frequency does.
            "mean_null_iff_freq_null": (
                "SELECT count(*) FROM decadal "
                "WHERE (dusty_days_mean IS NULL) <> (dusty_freq IS NULL)"
            ),
            **_count_order_checks("decadal"),
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


def station_status_counts(gold_dir: Path) -> dict[str, int]:
    """Stations per position status in a gold version (statuses without a station omitted), and
    `not_dusty`: the stations left out of gold by the dusty-station threshold."""
    path = _sql_path(gold_dir / "stations" / "part-000.parquet")
    rows = duckdb.sql(
        f"SELECT status, count(*) FROM '{path}' GROUP BY status ORDER BY status"
    ).fetchall()
    counts = {status: int(n) for status, n in rows}
    row = duckdb.sql(f"SELECT count(*) FROM '{path}' WHERE NOT is_dusty_station").fetchone()
    counts["not_dusty"] = int(row[0]) if row else 0
    return counts


def _write_review(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    """The dusty stations that need a human decision (conflict, unverified, moved; not
    overridden), plus override rows for stations with no silver, most dusty first (docs/rules.md
    4.3: positions are checked by hand only for stations that turn out dusty)."""
    con.execute(f"""
        COPY (
            SELECT station_id, status, file_lat, file_lon, isd_name, isd_lat, isd_lon, max_km,
                   first_year, last_year, n_years, n_dusty_reports
            FROM stations
            WHERE status IN ('conflict', 'unverified', 'moved') AND is_dusty_station
            UNION ALL
            SELECT station_id, 'override_unused', NULL, NULL, NULL, NULL, NULL, NULL,
                   NULL, NULL, NULL, NULL
            FROM overrides
            WHERE station_id NOT IN (SELECT station_id FROM stations)
            ORDER BY n_dusty_reports DESC NULLS LAST, station_id
        ) TO '{_sql_path(path)}' (HEADER, DELIMITER ',')
    """)


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
    # Station-position inputs (stations.py), validated before anything is built.
    isd_path, overrides_path = settings.isd_history_path, settings.position_overrides_path
    isd_history, overrides = load_isd_history(isd_path), load_overrides(overrides_path)
    con = duckdb.connect()  # in-memory database; reads the Parquet files directly from disk
    # One view over all silver files. union_by_name: files may differ in optional columns.
    # `silver_all` feeds the station table; the other products read `silver`, the same reports
    # without the stations left out of gold (created right after `stations`).
    con.read_parquet([_sql_path(p) for p in silver_paths], union_by_name=True).create_view(
        "silver_all"
    )
    con.register("isd_history", isd_history)
    con.register("overrides", overrides)
    inputs_md5 = {"isd_history_md5": file_md5(isd_path), "overrides_md5": file_md5(overrides_path)}
    con.execute(f"SET VARIABLE agree_km = {float(settings.stations.agree_km)}")
    con.execute(f"SET VARIABLE conflict_km = {float(settings.stations.conflict_km)}")
    con.execute(f"SET VARIABLE min_dusty_days = {int(settings.stations.min_dusty_days)}")
    con.execute(f"SET VARIABLE min_dusty_years = {int(settings.stations.min_dusty_years)}")
    for key, md5 in inputs_md5.items():
        con.execute(f"SET VARIABLE {key} = '{md5}'")  # hex digits only: safe to inline
    # Make the valid-day threshold available to the SQL as getvariable('min_bins').
    con.execute(f"SET VARIABLE min_bins = {int(settings.products.min_bins_per_day)}")
    # ... and the visibility limit of dusty_day_vis5 as getvariable('vis5_max_m').
    con.execute(f"SET VARIABLE vis5_max_m = {float(settings.products.vis5_max_m)}")
    # ... and the coverage threshold of a month in the yearly table.
    con.execute(
        f"SET VARIABLE min_valid_days_month = {int(settings.products.min_valid_days_month)}"
    )
    try:
        # Record which silver this gold version was built from (lineage).
        lineage = {
            "silver_method_version": expected[0],
            "silver_config_hash": expected[1],
            **inputs_md5,  # the station-position inputs (isd-history, overrides)
        }
        atomic_write_bytes(out_dir / "silver_version.json", json.dumps(lineage, indent=2).encode())
        for product in PRODUCTS:
            target = out_dir / product.name / "part-000.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            sql = (SQL_DIR / product.sql_file).read_text()
            # COPY (query) TO file: DuckDB streams the query result straight into Parquet.
            con.execute(f"COPY ({sql}) TO '{_sql_path(target)}' (FORMAT parquet, COMPRESSION zstd)")
            # Re-read the written file as a view so the checks test exactly what was saved.
            con.read_parquet(_sql_path(target)).create_view(product.name)
            if product.name == "stations":
                con.execute(
                    "CREATE VIEW silver AS SELECT * FROM silver_all "
                    "WHERE station_id IN (SELECT station_id FROM stations WHERE in_gold)"
                )
                _write_review(con, out_dir / "stations_review.csv")
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
