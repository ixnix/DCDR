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
1. Check that all silver files were built by the current code and settings (refuse otherwise),
   and count their column layouts (schemas) and reports, from each file's footer only.
2. Work in batches of whole stations (`products.gold_batch_reports` reports each): every product
   is computed per station, so DuckDB's memory grows with the batch, not with all of silver
   (built at once, 600 stations already exhausted 16 GB of RAM, 2026-11-05). DuckDB also gets a
   memory limit and a spill folder on the data disk. Each batch opens its silver files as one
   view: with one schema (the normal case) as they are; with several, lined up by column name
   (`union_by_name`), which reads every file's schema first and needs time and memory that grow
   faster than the number of files (the whole ISD ran out of memory that way).
3. Pass 1: the station table for every station (in station-ID order; ~10 % of the build),
   merged into one file, checked as a whole; the review list.
4. Pass 2: only the stations in gold, ordered by place: grid cells of `products.gold_grid_deg`
   along a Hilbert curve, then each station's place in its cell. Batches hold whole cells (a cell
   is split only when it alone is larger than a batch). For each batch, every other product: its
   SQL file (src/dcdr/sql/<name>.sql) written as one part under `_batches/`, then its checks
   (each a query that must return 0; all per station, so checking each batch checks the table).
5. Combine each product's batch parts, in order, into files of about `products.gold_file_mb`,
   checking the row counts. Every grid cell then lies in consecutive files, so a later product
   on a lon/lat grid can be built from consecutive files.
6. Only if everything passed: point `gold/CURRENT` at the new version, prune old versions.

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
import logging
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path

import duckdb
import polars as pl
import pyarrow.parquet as pq

from dcdr.config import Settings, silver_config_hash
from dcdr.process import METHOD_VERSION
from dcdr.process.migrations import effective_version, ledger_path, read_ledger
from dcdr.state import atomic_write_bytes, footer_metadata, safe_listdir
from dcdr.stations import (
    file_md5,
    load_isd_history,
    load_overrides,
    load_period_notes,
    note_texts,
)

log = logging.getLogger(__name__)

# SQL files ship inside the package (src/dcdr/sql/), found the same way as default.yaml.
SQL_DIR = files("dcdr") / "sql"
# Files between progress lines of the silver check (the whole ISD takes ~40 min on the USB SSD).
PROGRESS_EVERY = 50_000


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

# Periods that end before the archive end (products.archive_end, SQL variable `archive_end`), as
# the statistics SQL keeps them: so a table is compared only with the complete periods of the
# table it is built from (a day of `daily`; a year of `monthly` or `yearly`).
_MONTH_DONE = "last_day(lmst_date) < getvariable('archive_end')"
_YEAR_DONE = "make_date(year, 12, 31) < getvariable('archive_end')"
_DECADE_DONE = "make_date(year - (year - 1) % 10 + 9, 12, 31) < getvariable('archive_end')"


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
                f"FROM daily WHERE {_MONTH_DONE} GROUP BY ALL) d "
                "FULL JOIN monthly m USING (station_id, year, month) "
                "WHERE d.n_days IS DISTINCT FROM m.n_days "
                "OR d.n_valid_days IS DISTINCT FROM m.n_valid_days "
                "OR d.n_dusty_days IS DISTINCT FROM m.n_dusty_days"
            ),
            # No month cut short by the archive end (docs/rules.md 11).
            "before_archive_end": (
                "SELECT count(*) FROM monthly WHERE last_day(make_date(year, month, 1)) "
                ">= getvariable('archive_end')"
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
                f"sum(n_bins_obs) AS n_bins_obs FROM monthly WHERE {_YEAR_DONE} GROUP BY ALL) m "
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
            # No year cut short by the archive end.
            "before_archive_end": f"SELECT count(*) FROM yearly WHERE NOT ({_YEAR_DONE})",
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
                + f" FROM yearly WHERE {_DECADE_DONE} GROUP BY ALL) y "
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
            # No decade cut short by the archive end.
            "before_archive_end": (
                "SELECT count(*) FROM decadal "
                "WHERE make_date(decade + 9, 12, 31) >= getvariable('archive_end')"
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


class ArchiveEndError(RuntimeError):
    """Silver holds reports after `products.archive_end` (a newer download): move the setting."""


@dataclass
class SilverCheck:
    """What gold learns from the silver files' footers before building (`_check_silver`)."""

    versions: set[tuple[str, str]]  # distinct (method version, config_hash) pairs
    carried: int  # files that count as their version through the ledger of carried files
    schemas: int  # distinct column layouts (names and types, in order)
    rows: list[int]  # reports per file, in the order of the paths (sizes the batches)
    # (file station, other station): files that also hold reports of another station ID (some
    # ISD files: a station renumbered within a year); such stations must share a batch
    links: set[tuple[str, str]]
    # the latest report time (`time_utc`) in any file, from the footers' statistics; None if no
    # file has them (checked against products.archive_end)
    last_report: datetime | None = None


def _check_silver(paths: list[Path], ledger: pl.DataFrame) -> SilverCheck:
    """Read every silver file's footer once: its version (for the guards in `build_gold`), its
    column layout (to decide how DuckDB reads the files), its number of reports (batches), its
    latest report time (the max statistic of `time_utc`, for the archive-end guard) and
    the station IDs in it, from the min/max statistics of `station_id` (`links`). Min and max are
    exact for files with at most two IDs, as every ISD file now; the `unique_station` check of
    the station table catches any other ID.

    A file's version is `effective_version`: the one that built it, or the one it was carried to
    (process/migrations.py). Files are found in the ledger by their place in silver
    (`year=YYYY/<station>.parquet`).
    """
    ledger_rows = {(r["station_id"], r["year"]): r for r in ledger.iter_rows(named=True)}
    versions: set[tuple[str, str]] = set()
    layouts: set[tuple[tuple[str, str], ...]] = set()
    rows: list[int] = []
    links: set[tuple[str, str]] = set()
    carried = 0
    last_report: datetime | None = None
    for i, p in enumerate(paths, 1):
        footer = pq.read_metadata(p)  # the footer only, not the data
        meta = footer_metadata(footer.metadata)
        row = ledger_rows.get((p.stem, int(p.parent.name.removeprefix("year="))))
        version = effective_version(meta, row)
        carried += version != meta.get("method_version", "")
        versions.add((version, meta.get("config_hash", "")))
        schema = footer.schema.to_arrow_schema()
        layouts.add(tuple((f.name, str(f.type)) for f in schema))
        rows.append(footer.num_rows)
        j = schema.get_field_index("station_id")
        for g in range(footer.num_row_groups if j >= 0 else 0):
            stats = footer.row_group(g).column(j).statistics
            if stats is not None and stats.has_min_max:
                links.update((p.stem, sid) for sid in (stats.min, stats.max) if sid != p.stem)
        t = schema.get_field_index("time_utc")
        for g in range(footer.num_row_groups if t >= 0 else 0):
            stats = footer.row_group(g).column(t).statistics
            if stats is not None and stats.has_min_max and stats.max is not None:
                last_report = max(last_report, stats.max) if last_report else stats.max
        if i % PROGRESS_EVERY == 0:
            log.info("silver check: %d/%d files", i, len(paths))
    return SilverCheck(versions, carried, len(layouts), rows, links, last_report)


def _station_batches(
    paths: list[Path],
    rows: list[int],
    budget: int,
    order: list[tuple[str, int]] | None = None,
    links: Iterable[tuple[str, str]] = (),
) -> list[list[Path]]:
    """Group silver files into batches of whole stations, each with at most `budget` reports.

    `order` lists (station, grid cell) in the order to follow; only those stations are batched.
    A batch never splits a cell that fits into one: a cell that does not fit after the stations
    already in the batch starts a new batch, and only a cell larger than a whole batch is split,
    by station (a station larger than a batch is a batch of its own). Without `order`: every
    station in station-ID order, each its own cell.

    `links` are (file station, other station) pairs: a file named after one station that also
    holds reports of another (`SilverCheck.links`). Linked stations are one unit, batched together
    where the first of them comes in `order`, so no station's reports are split over two batches.

    Every gold product is computed per station, so a batch can be built on its own; DuckDB's
    memory then grows with the batch, not with all of silver.
    """
    by_station: dict[str, list[tuple[Path, int]]] = {}
    for path, n in zip(paths, rows, strict=True):
        by_station.setdefault(path.stem, []).append((path, n))
    parent: dict[str, str] = {}  # union-find: each station points towards its unit's first ID

    def unit_of(station: str) -> str:
        parent.setdefault(station, station)
        while parent[station] != station:
            station = parent[station]
        return station

    for a, b in links:
        ua, ub = unit_of(a), unit_of(b)
        if ua != ub:
            parent[max(ua, ub)] = min(ua, ub)
    unit_files: dict[str, list[tuple[Path, int]]] = {}
    for station in sorted(by_station):
        unit_files.setdefault(unit_of(station), []).extend(by_station[station])
    if order is None:
        order = [(s, i) for i, s in enumerate(sorted(by_station))]
    size = {u: sum(n for _, n in files) for u, files in unit_files.items()}
    cells: list[list[str]] = []  # consecutive units of one cell
    unit_cell: dict[str, int] = {}
    for station, cell in order:
        unit = unit_of(station)
        if unit not in unit_files or unit in unit_cell:  # no files, or already placed
            continue
        if cells and unit_cell[cells[-1][0]] == cell:
            cells[-1].append(unit)
        else:
            cells.append([unit])
        unit_cell[unit] = cell
    batches: list[list[Path]] = []
    current: list[Path] = []
    current_n = 0
    for members in cells:
        if current and current_n + sum(size[u] for u in members) > budget:
            batches.append(current)  # the whole cell does not fit: start a new batch
            current, current_n = [], 0
        for unit in members:
            if current and current_n + size[unit] > budget:  # only in a cell > a batch
                batches.append(current)
                current, current_n = [], 0
            current += [path for path, _ in unit_files[unit]]
            current_n += size[unit]
    if current:
        batches.append(current)
    return batches


# Sub-cells per side of a grid cell for ordering stations inside it (10-degree cells: ~0.04 deg).
_FINE = 256


def _hilbert(n: int, x: int, y: int) -> int:
    """Position of cell (x, y) along a Hilbert curve over an n x n grid (n a power of 2). Cells
    next to each other on the curve are next to each other on the grid, so sorting by it keeps
    nearby places together (the standard algorithm: Wikipedia, "Hilbert curve", xy2d)."""
    d = 0
    s = n // 2
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:  # rotate the quadrant so the curve stays continuous
            if rx == 1:
                x, y = n - 1 - x, n - 1 - y
            x, y = y, x
        s //= 2
    return d


def _spatial_order(stations: pl.DataFrame, grid_deg: float) -> list[tuple[str, int]]:
    """(station, grid cell) ordered by place: cells of `grid_deg` degrees along a Hilbert curve
    (the cell number is its place on the curve), then each station's place inside its cell (a
    finer Hilbert curve), then the station ID. Stations of one cell are therefore consecutive.
    Uses the trusted position (gold `stations`); stations without one go last, in a cell of
    their own."""
    cols, rows = round(360 / grid_deg), round(180 / grid_deg)
    n = 1
    while n < max(cols, rows):
        n *= 2

    def key(station: str, lat: float | None, lon: float | None) -> tuple[int, int, int, str]:
        if lat is None or lon is None:
            return (1, n * n, 0, station)  # after every real cell
        fx, fy = (lon + 180) / grid_deg, (lat + 90) / grid_deg
        cx, cy = min(int(fx), cols - 1), min(int(fy), rows - 1)
        sx, sy = min(int((fx - cx) * _FINE), _FINE - 1), min(int((fy - cy) * _FINE), _FINE - 1)
        return (0, _hilbert(n, cx, cy), _hilbert(_FINE, sx, sy), station)

    keys = [
        key(s, lat, lon)
        for s, lat, lon in stations.select("station_id", "trusted_lat", "trusted_lon").iter_rows()
    ]
    return [(k[3], k[1]) for k in sorted(keys)]


def _size_groups(sizes: list[int], target: int) -> list[list[int]]:
    """Group consecutive items (by index) so each group's total size stays within `target`; an
    item larger than `target` is a group of its own. Used to combine batch parts into files."""
    groups: list[list[int]] = []
    current: list[int] = []
    total = 0
    for i, size in enumerate(sizes):
        if current and total + size > target:
            groups.append(current)
            current, total = [], 0
        current.append(i)
        total += size
    if current:
        groups.append(current)
    return groups


def _connect(settings: Settings, spill: Path) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB connection limited to `products.gold_memory_limit`; what does not fit
    goes to `spill`, a folder on the data disk (removed after the build)."""
    spill.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(
        config={
            "memory_limit": settings.products.gold_memory_limit,
            "temp_directory": str(spill),
        }
    )


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
    overridden), plus override and period-note rows for stations with no silver (a typo would
    otherwise do nothing), most dusty first (docs/rules.md 4.3: positions are checked by hand only
    for stations that turn out dusty)."""
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
            UNION ALL
            SELECT DISTINCT station_id, 'note_unused', NULL, NULL, NULL, NULL, NULL, NULL,
                   NULL, NULL, NULL, NULL
            FROM period_notes
            WHERE station_id NOT IN (SELECT station_id FROM stations)
            ORDER BY n_dusty_reports DESC NULLS LAST, station_id
        ) TO '{_sql_path(path)}' (HEADER, DELIMITER ',')
    """)


def _run_checks(con: duckdb.DuckDBPyConnection, product: Product, where: str) -> None:
    """Run a product's checks against its view; raise GoldCheckError naming `where` if any fail."""
    failures = {}
    for name, query in product.checks.items():
        row = con.execute(query).fetchone()
        count = row[0] if row else 0
        if count:
            failures[name] = count
    if failures:
        raise GoldCheckError(f"{product.name}{where}: {failures}")


def _register_notes(con: duckdb.DuckDBPyConnection, period_notes: pl.DataFrame) -> None:
    """Period notes (stations.py): the text each gold table copies into its `note` column."""
    note_years, note_decades, note_stations = note_texts(period_notes)
    con.register("period_notes", period_notes)
    con.register("note_years", note_years)
    con.register("note_decades", note_decades)
    con.register("note_stations", note_stations)


def _set_product_variables(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    """The `products` settings the product SQL reads with getvariable(...)."""
    p = settings.products
    # the valid-day threshold, getvariable('min_bins') ...
    con.execute(f"SET VARIABLE min_bins = {int(p.min_bins_per_day)}")
    # ... the visibility limit of dusty_day_vis5, getvariable('vis5_max_m') ...
    con.execute(f"SET VARIABLE vis5_max_m = {float(p.vis5_max_m)}")
    # ... the coverage threshold of a month in the yearly table ...
    con.execute(f"SET VARIABLE min_valid_days_month = {int(p.min_valid_days_month)}")
    # ... and the archive end: monthly, yearly and decadal keep periods ending before it (a far
    # date when there is none, so the SQL needs no NULL case). A date: safe to inline.
    end_sql = (p.archive_end or date(9999, 12, 31)).isoformat()
    con.execute(f"SET VARIABLE archive_end = DATE '{end_sql}'")


def _stations_batch(
    con: duckdb.DuckDBPyConnection, paths: list[Path], staging: Path, k: int, union: bool
) -> None:
    """Pass 1 for one batch: the station table of its stations into
    `<staging>/stations/batch-<k>.parquet`, checked. Files with one schema are read as they are;
    only mixed schemas need union_by_name (module docstring)."""
    con.read_parquet([_sql_path(p) for p in paths], union_by_name=union).create_view("silver_all")
    product = PRODUCTS[0]
    target = staging / product.name / f"batch-{k:05d}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    sql = (SQL_DIR / product.sql_file).read_text()
    # COPY (query) TO file: DuckDB streams the query result straight into Parquet.
    con.execute(f"COPY ({sql}\n) TO '{_sql_path(target)}' (FORMAT parquet, COMPRESSION zstd)")
    con.read_parquet(_sql_path(target)).create_view(product.name)
    _run_checks(con, product, f" (batch {k})")


def _build_batch(
    con: duckdb.DuckDBPyConnection, paths: list[Path], staging: Path, k: int, union: bool
) -> None:
    """Pass 2 for one batch of stations in gold: every product after the station table into
    `<staging>/<product>/batch-<k>.parquet` (rows by station, then time, as each SQL orders
    them), each part checked. The checks are all per station (or per station and period), so
    checking each batch checks the whole table. Expects the view `stations` (all stations)."""
    con.read_parquet([_sql_path(p) for p in paths], union_by_name=union).create_view("silver_all")
    # Only the reports of stations in gold (a batch holds only those; this keeps it explicit).
    con.execute(
        "CREATE OR REPLACE VIEW silver AS SELECT * FROM silver_all "
        "WHERE station_id IN (SELECT station_id FROM stations WHERE in_gold)"
    )
    for product in PRODUCTS[1:]:
        target = staging / product.name / f"batch-{k:05d}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        sql = (SQL_DIR / product.sql_file).read_text()
        # COPY (query) TO file: DuckDB streams the query result straight into Parquet.
        con.execute(f"COPY ({sql}\n) TO '{_sql_path(target)}' (FORMAT parquet, COMPRESSION zstd)")
        # Re-read the written file as a view so the checks test exactly what was saved; the
        # later products of this batch read it under the product's name.
        con.read_parquet(_sql_path(target)).create_view(product.name)
        _run_checks(con, product, f" (batch {k})")


def _combine(
    con: duckdb.DuckDBPyConnection,
    parts: list[Path],
    folder: Path,
    target_bytes: int,
    order_by: str = "",
) -> None:
    """Write a product's batch parts (in station order) as `folder/part-NNN.parquet` files of about
    `target_bytes` each, keeping the row order, and check no row was lost or added. `order_by`
    sorts each file instead (only for the small station table: linked stations, which share a
    batch, put the batches slightly out of station-ID order)."""
    folder.mkdir(parents=True, exist_ok=True)
    before = sum(pq.read_metadata(p).num_rows for p in parts)
    groups = _size_groups([p.stat().st_size for p in parts], target_bytes)
    for j, group in enumerate(groups):
        out = folder / f"part-{j:03d}.parquet"
        files = [parts[i] for i in group]
        if len(files) == 1 and not order_by:
            files[0].rename(out)  # nothing to combine: move it
            continue
        listed = ", ".join(f"'{_sql_path(f)}'" for f in files)
        # read_parquet keeps the files' order and DuckDB keeps insertion order, so the rows stay
        # sorted by station (and time) without a sort.
        con.execute(
            f"COPY (SELECT * FROM read_parquet([{listed}]){' ORDER BY ' if order_by else ''}"
            f"{order_by}) TO '{_sql_path(out)}' "
            "(FORMAT parquet, COMPRESSION zstd)"
        )
        for f in files:
            f.unlink()
    after = sum(pq.read_metadata(p).num_rows for p in folder.glob("part-*.parquet"))
    if after != before:
        raise GoldCheckError(f"{folder.name}: {before} rows in the batch parts, {after} combined")


def build_gold(settings: Settings, silver_paths: list[Path], run_id: str) -> Path:
    """Build every gold product into gold/versions/<run_id>/, check it, then activate it.

    Gold is built in batches of whole stations (`products.gold_batch_reports` silver reports
    each): every product except the station table is written as one part file per batch; the
    station table is merged into one file. Raises (and leaves the previous gold version active)
    if the silver inputs are mixed or stale, or if any product check fails. Returns the new
    version's folder.
    """
    if not silver_paths:
        raise ValueError("no silver files to build gold from")
    # Guard 1: all silver files must come from the same code and settings ...
    check = _check_silver(silver_paths, read_ledger(ledger_path(settings.data_root)))
    versions, n_carried = check.versions, check.carried
    if len(versions) != 1:
        raise MixedSilverVersionsError(f"silver files have mixed versions: {sorted(versions)}")
    # Guard 2: ... and that must be the code and settings running now.
    expected = (METHOD_VERSION, silver_config_hash(settings))
    if versions != {expected}:
        raise StaleSilverError(
            f"silver is {versions.pop()}, code expects {expected}; re-run `dcdr process --force`"
        )
    # Guard 3: the statistics drop periods cut by the archive end; reports after it mean a newer
    # download, which must not be cut silently (move products.archive_end first).
    archive_end = settings.products.archive_end
    if archive_end and check.last_report and check.last_report.date() > archive_end:
        raise ArchiveEndError(
            f"silver has reports up to {check.last_report:%Y-%m-%d %H:%M} UTC, after "
            f"products.archive_end {archive_end}: set it to the archive's last day"
        )
    gold_root = settings.data_root / "gold"
    out_dir = gold_root / "versions" / run_id
    # Station-position inputs (stations.py), validated before anything is built.
    isd_path, overrides_path = settings.isd_history_path, settings.position_overrides_path
    isd_history, overrides = load_isd_history(isd_path), load_overrides(overrides_path)
    notes_path = settings.period_notes_path
    period_notes = load_period_notes(notes_path)
    union = check.schemas > 1
    if union:
        log.warning(
            "silver: %d files, %d schemas: lining them up by column name (union_by_name), "
            "slow and memory-hungry for many files",
            len(silver_paths),
            check.schemas,
        )
    else:
        log.info("silver: %d files, 1 schema", len(silver_paths))
    budget = settings.products.gold_batch_reports
    reports = dict(zip(silver_paths, check.rows, strict=True))
    spill = gold_root / f"_spill_{run_id}"
    con = _connect(settings, spill)
    con.register("isd_history", isd_history)
    con.register("overrides", overrides)
    _register_notes(con, period_notes)
    inputs_md5 = {
        "isd_history_md5": file_md5(isd_path),
        "overrides_md5": file_md5(overrides_path),
        "period_notes_md5": file_md5(notes_path),
    }
    con.execute(f"SET VARIABLE agree_km = {float(settings.stations.agree_km)}")
    con.execute(f"SET VARIABLE conflict_km = {float(settings.stations.conflict_km)}")
    con.execute(f"SET VARIABLE min_dusty_days = {int(settings.stations.min_dusty_days)}")
    con.execute(f"SET VARIABLE min_dusty_years = {int(settings.stations.min_dusty_years)}")
    for key, md5 in inputs_md5.items():
        con.execute(f"SET VARIABLE {key} = '{md5}'")  # hex digits only: safe to inline
    _set_product_variables(con, settings)
    staging = out_dir / "_batches"
    target_bytes = int(settings.products.gold_file_mb * 2**20)
    try:
        # Pass 1: the station table for every station (cheap: ~10 % of the build), in batches in
        # station-ID order; merged into one file, checked as a whole, and the review list.
        first = _station_batches(silver_paths, check.rows, budget, links=check.links)
        log.info(
            "gold: %d reports; %d files also hold another station's reports; "
            "pass 1 (stations) in %d batches",
            sum(check.rows),
            len(check.links),
            len(first),
        )
        for k, batch in enumerate(first):
            _stations_batch(con, batch, staging, k, union)
        parts = sorted((staging / "stations").glob("batch-*.parquet"))
        _combine(con, parts, out_dir / "stations", 2**62, order_by="station_id")
        stations_file = _sql_path(out_dir / "stations" / "part-000.parquet")
        con.read_parquet(stations_file).create_view("stations")
        _run_checks(con, PRODUCTS[0], " (all batches)")
        _write_review(con, out_dir / "stations_review.csv")
        # Pass 2: only the stations in gold, ordered by place (gold_grid_deg cells along a Hilbert
        # curve), in batches of whole cells; every other product, then combined. A cell is split
        # only when it alone is larger than a batch, so each cell lies in consecutive files.
        in_gold = pl.read_parquet(out_dir / "stations" / "part-000.parquet").filter("in_gold")
        order = _spatial_order(in_gold, settings.products.gold_grid_deg)
        # Pass 2 finds a station's files by name (silver/year=YYYY/<station_id>.parquet) or by the
        # station IDs in their footers (links); a station in gold found in neither would silently
        # drop out, so stop instead.
        known = {p.stem for p in silver_paths} | {other for _, other in check.links}
        unfound = sorted({s for s, _ in order} - known)
        if unfound:
            raise GoldCheckError(f"stations in gold found in no silver file: {unfound[:10]}")
        # With no station in gold, one batch still writes every product (empty).
        batches = _station_batches(silver_paths, check.rows, budget, order, check.links) or [
            silver_paths[:1]
        ]
        log.info(
            "gold: pass 2 (products) for %d stations in gold in %d batches of at most %d reports",
            len(order),
            len(batches),
            budget,
        )
        # Record which silver this gold version was built from (lineage), and how it was split.
        lineage = {
            "silver_method_version": expected[0],
            "silver_config_hash": expected[1],
            "silver_files": len(silver_paths),
            "silver_carried": n_carried,  # files built by an earlier method, carried forward
            **inputs_md5,  # the station inputs (isd-history, overrides, period notes)
            "gold_batches": len(batches),
            "gold_batch_reports": budget,
            "gold_grid_deg": settings.products.gold_grid_deg,
            # periods cut by the archive end are not in monthly, yearly, decadal
            "archive_end": archive_end.isoformat() if archive_end else None,
        }
        atomic_write_bytes(out_dir / "silver_version.json", json.dumps(lineage, indent=2).encode())
        for k, batch in enumerate(batches):
            started = time.monotonic()
            _build_batch(con, batch, staging, k, union)
            log.info(
                "gold: batch %d/%d done (%d stations, %d reports) in %.0f s",
                k + 1,
                len(batches),
                len({p.stem for p in batch}),
                sum(reports[p] for p in batch),
                time.monotonic() - started,
            )
        # Combine the batch parts into files of about gold_file_mb, keeping the order.
        for product in PRODUCTS[1:]:
            parts = sorted((staging / product.name).glob("batch-*.parquet"))
            _combine(con, parts, out_dir / product.name, target_bytes)
            log.info("gold: %s combined", product.name)
        shutil.rmtree(staging)
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)  # discard the unfinished version
        raise
    finally:
        con.close()
        shutil.rmtree(spill, ignore_errors=True)
    # Everything passed: switch readers to the new version, then delete old ones.
    atomic_write_bytes(gold_root / "CURRENT", run_id.encode())
    prune_versions(gold_root, settings.products.gold_keep_versions)
    return out_dir


STATISTICS = ("monthly", "yearly", "decadal")
COPIED = ("stations", "daily", "three_hourly")  # copied unchanged by rebuild_statistics
# Each statistics table's key after `station_id` (the order its SQL writes within a station).
_STAT_KEYS = {"monthly": "s.year, s.month", "yearly": "s.year", "decadal": "s.decade, s.period"}


def rebuild_statistics(settings: Settings, run_id: str) -> Path:
    """A new gold version from the active one with only `monthly`, `yearly` and `decadal`
    recomputed (`dcdr aggregate --statistics-only`; user decision 2026-11-06): for a change that
    touches only the statistics rules or their settings, in minutes instead of a full rebuild.

    The other tables (`stations`, `daily`, `three_hourly`, the review list) are copied unchanged;
    the statistics are computed from that `daily` with the same SQL files and checks as
    `build_gold`, one `daily` file at a time (each holds whole stations), in the stations' order
    in `daily`, so the tables equal a full build's row for row. Refuses (leaving the active
    version) when the active gold was built from other silver than the code expects, when the
    period notes changed (decadal reads them, daily holds the old ones), or when `daily` holds
    days after `products.archive_end`. A change to silver, `daily`, `three_hourly` or the
    station table needs a full `build_gold`. Returns the new version's folder."""
    src = current_gold_dir(settings)
    if src is None or not (src / "silver_version.json").exists():
        raise GoldCheckError("no gold version to rebuild the statistics from: build gold in full")
    lineage = json.loads((src / "silver_version.json").read_text())
    expected = [METHOD_VERSION, silver_config_hash(settings)]
    built = [lineage.get("silver_method_version"), lineage.get("silver_config_hash")]
    if built != expected:
        raise GoldCheckError(
            f"gold {src.name} was built from silver {built}, the code expects {expected}: "
            "build gold in full"
        )
    notes_path = settings.period_notes_path
    period_notes = load_period_notes(notes_path)
    if file_md5(notes_path) != lineage.get("period_notes_md5", ""):
        raise GoldCheckError(
            f"the period notes changed since gold {src.name} (daily holds the old ones): "
            "build gold in full"
        )
    daily_parts = sorted((src / "daily").glob("part-*.parquet"))
    if not daily_parts:
        raise GoldCheckError(f"gold {src.name} has no daily table: build gold in full")
    archive_end = settings.products.archive_end
    if archive_end:
        listed = ", ".join(f"'{_sql_path(p)}'" for p in daily_parts)
        last = duckdb.sql(f"SELECT max(lmst_date) FROM read_parquet([{listed}])").fetchone()
        if last and last[0] and last[0] > archive_end:
            raise ArchiveEndError(
                f"gold {src.name} has days up to {last[0]}, after products.archive_end "
                f"{archive_end}: set it to the archive's last day"
            )
    gold_root = settings.data_root / "gold"
    out_dir = gold_root / "versions" / run_id
    spill = gold_root / f"_spill_{run_id}"
    con = _connect(settings, spill)
    _register_notes(con, period_notes)
    _set_product_variables(con, settings)
    staging = out_dir / "_batches"
    try:
        out_dir.mkdir(parents=True)
        # Copy everything but the statistics and the lineage (rewritten below). `._*` files are
        # macOS's AppleDouble files on a non-Mac disk, not data (safe_listdir skips them too).
        for entry in safe_listdir(src):
            if entry.name in STATISTICS or entry.name == "silver_version.json":
                continue
            if entry.is_dir():
                shutil.copytree(entry, out_dir / entry.name, ignore=shutil.ignore_patterns("._*"))
            else:
                shutil.copy2(entry, out_dir / entry.name)
        log.info("gold: %s copied from %s; statistics from its daily", ", ".join(COPIED), src.name)
        lineage.update(archive_end=archive_end.isoformat() if archive_end else None)
        lineage["statistics_rebuilt_from"] = src.name
        atomic_write_bytes(out_dir / "silver_version.json", json.dumps(lineage, indent=2).encode())
        products = [p for p in PRODUCTS if p.name in STATISTICS]
        for k, part in enumerate(sorted((out_dir / "daily").glob("part-*.parquet"))):
            con.read_parquet(_sql_path(part), file_row_number=True).create_view("daily_rows")
            con.execute(
                "CREATE OR REPLACE VIEW daily AS SELECT * EXCLUDE (file_row_number) FROM daily_rows"
            )
            # each station's place in this daily file: full builds write the statistics in
            # the stations' daily order (batches in spatial order, station ID within a batch)
            con.execute(
                "CREATE OR REPLACE TABLE station_order AS SELECT station_id, "
                "min(file_row_number) AS k FROM daily_rows GROUP BY station_id"
            )
            for product in products:
                target = staging / product.name / f"batch-{k:05d}.parquet"
                target.parent.mkdir(parents=True, exist_ok=True)
                sql = (SQL_DIR / product.sql_file).read_text()
                con.execute(
                    f"COPY (SELECT s.* FROM ({sql}\n) s JOIN station_order o USING (station_id) "
                    f"ORDER BY o.k, {_STAT_KEYS[product.name]}) "
                    f"TO '{_sql_path(target)}' (FORMAT parquet, COMPRESSION zstd)"
                )
                con.read_parquet(_sql_path(target)).create_view(product.name)
                _run_checks(con, product, f" ({part.name})")
            log.info("gold: statistics of %s done", part.name)
        target_bytes = int(settings.products.gold_file_mb * 2**20)
        for name in STATISTICS:
            parts = sorted((staging / name).glob("batch-*.parquet"))
            _combine(con, parts, out_dir / name, target_bytes)
        shutil.rmtree(staging)
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)  # discard the unfinished version
        raise
    finally:
        con.close()
        shutil.rmtree(spill, ignore_errors=True)
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
