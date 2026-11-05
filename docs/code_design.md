# DCDR documentation, Part 1: Code design

How the `dcdr` package is built and used: structure, commands, inputs, outputs and design.
Part 2 ([rules.md](rules.md)) gives the data rules as implemented; [rules_history.md](rules_history.md)
how they came about.
The running record of decisions and changes is [worklog.md](worklog.md).

> Living document. It describes the code **as it is now** and is updated with every change.
> Last updated: 2026-11-04 (method version 2.1.0).

---

## 1. What DCDR is

DCDR (Dust Climate Data Record) turns NOAA's Integrated Surface Database (ISD) hourly station
reports into quality-controlled, analysis-ready dust records: one row per station per local day,
saying whether the day was dusty and with what weather. It is the successor of duISD v1
(Xi 2021; Zenodo [10.5281/zenodo.4107346](https://doi.org/10.5281/zenodo.4107346)).

It is a Python package with a command-line tool, `dcdr`, that runs a three-stage pipeline:

```
NOAA S3 (CSV)  --ingest-->  BRONZE  --process-->  SILVER  --aggregate-->  GOLD
one CSV per                 raw copy,              one row per report,      one row per
station-year                text, lossless         decoded, QC-flagged,     station x local day
                                                   dust-classified,
                                                   synoptic-sampled
```

The three layers ("medallion" architecture, common in data engineering) separate concerns:
bronze is a faithful copy of the source, silver is cleaned and interpreted per report, gold is
aggregated for analysis. Each layer can be rebuilt from the one before it.

## 2. Installation

Requirements: macOS or Linux, [uv](https://docs.astral.sh/uv/), internet access to AWS S3.

```bash
git clone <repo-url> DCDR && cd DCDR
uv sync            # creates .venv with the exact versions in uv.lock, installs dcdr (editable)
uv run dcdr --help
```

`uv sync` installs the package in editable mode: changes under `src/` take effect immediately.
`uv build` makes a wheel (`dist/dcdr-*.whl`) that works when installed anywhere (tested in
`tests/integration/test_packaging.py`).

## 3. Repository layout

```
DCDR/
├── pyproject.toml, uv.lock     package metadata, dependencies, exact lock; tool settings
├── src/dcdr/                   the package
│   ├── cli.py                  the `dcdr` command (Typer); thin layer over pipeline.py
│   ├── pipeline.py             runs stages, decides what to rebuild, writes run records
│   ├── config.py               loads and validates settings; config hashes
│   ├── default.yaml            built-in default settings (every method choice is a key)
│   ├── state.py                atomic writes, download manifest, run log
│   ├── ingest.py               NOAA S3 -> bronze
│   ├── process/                bronze -> silver
│   │   ├── parse.py            decode ISD packed fields; local mean solar time; SYNOP iX
│   │   ├── qc.py               quality flags, RH, capped visibility, gusts
│   │   ├── classify.py         dust class per report; haze rule; RH screen
│   │   ├── sources.py          source group of each report (SYNOP / METAR / other)
│   │   ├── guard.py            station guards for inferred "nothing significant" (V3; METAR)
│   │   ├── derived.py          are a station-year's SYNOPs copies of its METARs?
│   │   ├── build.py            runs the above for one station-year, writes silver
│   │   └── migrations.py       targeted rebuild: carry files across a method change
│   ├── aggregate.py            silver -> gold (versioned, checked, CURRENT pointer)
│   ├── stations.py             station-position inputs: isd-history and the override file
│   ├── sql/daily.sql           the gold `daily` table, in SQL (run by DuckDB)
│   ├── sql/three_hourly.sql    the gold `three_hourly` table, in SQL
│   └── schemas.py              the silver data contract
├── config/stations/            run inputs: station lists and region definitions
├── notebooks/                  (local only, not in the public repo) analyses (01: pilot station selection; 02: validating inferred "nothing significant";
│                               03: METAR present weather; 04: slot sources over the full record)
├── scripts/                    one-off scripts (thin_slice_report.py; fetch_references.py;
│                               list_isd_stations.py: station list of the whole ISD archive;
│                               compare_gold_versions.py: dusty days per station, two gold versions)
├── references/                official code manuals, local copies (git-ignored; index: docs/references.md)
├── tests/                      pytest: unit/, integration/, fixtures/
├── docs/                       code_design.md, rules.md, rules_history.md, worklog.md, references
│                               (the design spec and plans in docs/superpowers/ are local only)
└── legacy/                     (local only, not in the public repo) original v1 scripts, duISD v1
                                data, ISD format document (never imported)
```

Naming rule: code and commands are named by **action** (`ingest`, `process`, `aggregate`), data by
**layer** (`bronze/`, `silver/`, `gold/`). Planned later actions: `enrich` (environmental drivers)
and `forecast` (ML).

## 4. Commands

All commands run as `uv run dcdr <command>` from the repo (or `dcdr <command>` in an activated
environment). Every pipeline command records a run (section 7.6).

| Command | What it does | Main options |
|---|---|---|
| `dcdr ingest` | List NOAA's S3 files for the years; download new or changed station-years into bronze; saves the download record and logs progress every 500 files, so a stopped run resumes where it left off; re-downloads recorded files missing from disk | `--years`, `--stations` |
| `dcdr process` | Build silver for station-years that are missing or out of date; after a method change with a migration, carry forward the files it cannot affect (targeted rebuild); logs progress every 500 stations | `--years`, `--stations`, `--force`, `--workers` |
| `dcdr verify-silver` | Re-process a random sample of carried silver files with the running code and compare them with the files; exits with an error if any differ | `--sample` (default 1000), `--seed` |
| `dcdr aggregate` | Build a new gold version from silver, check it, activate it; prints the gold folder, the stations per position status and those left out as not dusty (e.g. `{'agree': 96, 'override': 1, 'not_dusty': 3}`); the stations needing a decision are in its `stations_review.csv` | `--years`, `--stations` |
| `dcdr run` | `ingest`, then `process`, then `aggregate` | as above |
| `dcdr status` | Download status counts and the five latest runs (read-only) | |
| `dcdr --version` | Package version | |

Common options:
- `--years`: `2024`, `2020-2024` or `2019,2021-2023` (required for pipeline commands).
- `--stations FILE`: CSV with a `station_id` column. Default: `stations.station_list` in the config
  (currently `config/stations/pilot_100.csv`).
- `--config FILE`: replace the built-in `default.yaml`. `--profile FILE`: override some keys.
- `--force` (process, run): rebuild silver even if it is up to date.
- `--workers N` (process): build N stations at a time, each in its own process (default 1).
  Results are identical; see "Rebuild silver for the whole ISD" in section 8.

Examples:

```bash
export DCDR_DATA=/Volumes/CrucialX8/dcdr          # where data live (see 5.1)
uv run dcdr run --stations config/stations/pilot_20.csv --years 2020-2024
uv run dcdr status
```

`ingest` also downloads the year before the first requested year: the haze rule looks back 24 h,
which on 1 January reaches the previous year.

`process` builds each station-year from its own bronze plus the previous year's last day (haze
look-back), when on disk. Each report is stored in the file of its own UTC year (method 2.0.0).
The previous year's fingerprint (`prev_source_md5`) is stored, so a change there rebuilds the
year; the next year is not read.

Per station, `process` first computes the two station guards (V3, METAR) from all the station's
bronze years, then builds each requested year. The guards classify only the reports that can be
evidence (visibility reported and iX 2/3/5/6, or full-text METARs with visibility > 5 km):
parsing, QC and classification work report by report, so the counts are the same as classifying
everything. With `--workers N`, stations go to N worker processes (`ProcessPoolExecutor`, spawn);
each worker runs Polars on one thread (`POLARS_MAX_THREADS=1`), and its log lines use the same
format as the main process.

## 5. Configuration

### 5.1 Where the data go
`paths.data_root` (default `data`, relative to the working directory). Override with the
`DCDR_DATA` environment variable (recommended; put `export DCDR_DATA=...` in `~/.zshrc`) or a
profile. The folder is created if missing.

### 5.2 Settings
All method choices live in `src/dcdr/default.yaml`, validated by Pydantic models in `config.py`
(unknown or misspelled keys stop the run). Sections:

| Section | Controls | Affects silver hash |
|---|---|---|
| `paths` | data root | no |
| `ingest` | bucket, region, concurrency, retries, checkpoint interval (`checkpoint_every`) | no |
| `parse` | which OD wind types are gusts | yes |
| `qc` | QC code mapping, accepted flags, visibility cap, RH screen | yes |
| `classify` | weather fields used, haze window, snow/sand temperatures, SYNOP iX rule, inferred nil (V3) and its guard, METAR rules (`metar_rules`, `metar_report_types`, `metar_nil_min_vis_m`), METAR inference and its guard (`metar_infer_nil`, `metar_nil_guard_*`), derived SYNOPs (`derived_*`) | yes |
| `sources` | excluded report types (`exclude_types`: SOD, SOM), source groups (`groups`: SYNOP and METAR report types; every other type is "other") | yes |
| `products` | valid-day threshold (`min_bins_per_day`), visibility limit of `dusty_day_vis5` (`vis5_max_m`), gold versions kept | no (gold only) |
| `stations` | default station list; station positions: the saved isd-history (`isd_history`, relative to the data root), the override file (`position_overrides`), the distance bands `agree_km` (10) and `conflict_km` (50); the dusty-station threshold `min_dusty_days` (10) and `min_dusty_years` (2) | no (gold only) |

Precedence: `default.yaml` < `--profile` < test overrides < `DCDR_DATA`.

### 5.3 Fingerprints
- `config_hash`: SHA-256 (16 hex) of all resolved settings; stored in every run record.
- `silver_config_hash`: hash of the silver sections only; stored in every silver file. If it
  changes, silver is rebuilt.
- `METHOD_VERSION` (`src/dcdr/process/__init__.py`, currently **2.1.0**): bumped whenever
  processing logic changes; stored in every silver file. If it changes, silver is rebuilt,
  except where the change declares a migration (8, "After a method change").

## 6. Inputs

### 6.1 NOAA ISD (the data source)
`s3://noaa-global-hourly-pds/<year>/<station_id>.csv`, public, anonymous, `us-east-1`. One CSV
per station-year; columns differ by station and year (a column appears only if the station
reported that group). Station ID = USAF (6) + WBAN (5) digits, e.g. `40582099999`.
Reference for every field: `legacy/isd-format-document.pdf` (NOAA ISD format document; a local
copy, not in the public repo: NOAA NCEI publishes it with the ISD data).

### 6.2 Station lists (`config/stations/`)

| File | Content | Made by |
|---|---|---|
| `pilot_100.csv` | the 100 pilot stations: 88 dusty + 12 contrast | notebook 01 |
| `pilot_20.csv` | 20 dusty stations (2-3 per region) for end-to-end tests | notebook 01 |
| `contrast.csv` | the 12 hand-picked contrast stations, with reasons | by hand |
| `regions.yaml` | dust regions: lat/lon box + optional country filter | by hand |
| `position_overrides.csv` | hand decisions on station positions: `station_id, lat, lon, action` (`use` the given position or `exclude` the station), `reason` (required), `decided` (date); validated by `stations.load_overrides` (a missing or non-11-digit `station_id`, a missing reason, a duplicate station, an out-of-range position or an unknown action stops the gold build, naming the file line) | by hand |

Station-list columns: `station_id, name, country, lat, lon, region, aridity_index`. Only
`station_id` is required by the pipeline.

### 6.3 isd-history (saved copy)
NOAA's `isd-history.csv` (one row per station: USAF, WBAN, name, country, position, period) is
read from `<data root>/catalog/isd-history.csv` (config `stations.isd_history`); the pipeline
never downloads it. `stations.load_isd_history` keys it by `station_id` = USAF + WBAN, treats an
empty position or 0/0 as "no position", and stops on a station listed twice or a missing file.
Silver uses its longitude for reports without a position (rules.md 4.2; without the file,
`process` warns and fills from the station-year median), gold for the position check (4.3).

## 7. Outputs

Everything goes under the data root:

```
<data_root>/
├── bronze/year=YYYY/<station_id>.parquet
├── silver/year=YYYY/<station_id>.parquet
├── gold/
│   ├── CURRENT                          text file: run ID of the active version
│   └── versions/<run_id>/
│       ├── silver_version.json          which silver (method version, config hash) and which
│       │                                station inputs (isd-history and override md5s) built it
│       ├── stations/part-000.parquet    station positions: status, trusted position, evidence
│       ├── stations_review.csv          stations needing a hand decision, most dusty first
│       ├── daily/part-000.parquet
│       └── three_hourly/part-000.parquet
└── _state/
    ├── sources.parquet                  download manifest (one row per S3 file)
    ├── runs.parquet                     run log (one row per command run)
    └── runs/<run_id>/config.resolved.yaml   exact settings of each run
```

`year=YYYY` folders are "Hive-style" partitions: DuckDB and Polars can filter by year from the
folder name. Parquet files carry provenance in their footer metadata (section 7.6).

### 7.1 Bronze
All columns of the NOAA CSV, **as text**, unchanged, plus `source_key`, `source_md5`,
`ingest_run_id`. Lossless by design (see 9.1). About 9 % of the CSV size (Parquet + zstd).

### 7.2 Silver
One row per report (exact duplicates collapsed), about 250 columns: decoded values, QC flags,
local time, dust class and evidence, source group. Core columns come first:

| Group | Columns |
|---|---|
| Identity, time | `station_id, time_utc, time_lmst, lmst_date, lmst_hour, summary_lmst_date, report_type, source_group` (synop/metar/other; null for summaries), `source, lat, lon, lon_used, elev` |
| Source | `source_group` (synop/metar/other; null for summaries), `is_excluded_type` (SOD/SOM) |
| Weather observed? | `has_pw`, `pw_status` (reported, nil_significant, nil_inferred, nil_metar, not_observed), `syn_ix`, `metar_text` (full, fragment), `metar_auto`, `metar_wx` (missing, group, cavok, none), `metar_rmk_dust`, `synop_derived` |
| Dust | `dust_rank, dust_class_rule, dust_rule_id, dust_class_final, dust_haze, rh_screened, flag_du, flag_duhz, flag_susp, flag_bd, flag_ds, flag_vicinity, flag_past, flag_ambiguous, flag_daily_dust, pw_evidence` |
| Weather values | `wsd, use_wsd, wind_dir, use_wind_dir, gust, use_gust, gust_src, vis_raw, vis_capped, use_vis, t, use_t, td, use_td, rh, slp, use_slp` |
| Bookkeeping | `parse_status, dup_count, row_key, source_key` |
| Raw decoded fields | e.g. `mw1_code, mw1_qc, au1_obsc, ay1_code, ...` (alphabetical) |

Meanings are in Part 2. The contract (`schemas.py`) is checked before every write: types,
allowed values, unique `row_key`, and a source group exactly on the reports that are not
summaries.

### 7.3 Gold `daily`
One row per station per local (LMST) day, built from the `three_hourly` bins of the same gold run
(`aggregate.PRODUCTS` runs `three_hourly` first; `daily.sql` reads it as a view, and silver only
for daily-summary dust). Columns (definitions in Part 2, section 9):
`n_reports, n_bins, n_bins_obs, valid_day, dusty_day, n_bins_dusty, n_bins_obs_explicit,
valid_day_explicit, n_bins_nil_inferred, n_bins_nil_metar, n_bins_obs_{synop, metar, other},
n_bins_dust_{synop, metar, other}, valid_day_{synop, metar, other}, dusty_day_{synop, metar, other},
n_bins_obs_vis5, valid_day_vis5, dusty_day_vis5, past_weather_dust, daily_summary_dust,
vicinity_dust_day, remarks_dust_day, n_dust_bins_{suspended, blowing, whirls, storm, dust_haze},
n_bins_rh_screened, dust_first_lmst_hour, wsd_mean, wsd_max, gust_max, vis_min, vis_hmean,
n_vis_zero, t_mean, t_min, t_max, td_mean, rh_mean, slp_mean, dup6_mean, d_t_mean, d_slp_mean`.
Which dusty-day column to use (each with its own `valid_day*`): `dusty_day` for the fullest count
(any report in a bin); `dusty_day_synop` (SYNOP judged alone) to compare SYNOP stations;
`dusty_day_vis5` to compare stations or years with different report mixes; see Part 2,
sections 5.2, 5.3 and 9. The valid-day threshold reaches the SQL as `getvariable('min_bins')`
(config `products.min_bins_per_day`).

Checks run before activation (each must return 0 rows): at most 8 bins per day; observed bins ≤
bins; explicit, per-source, inferred, implied and vis5-judgeable bins ≤ observed bins; a source's
dusty bins ≤ the bins it observed; `dusty_day` = `dusty_day_synop` OR `_metar` OR `_other`; each
source's dusty bins ≤ `n_bins_dusty` ≤ their sum; `dusty_day_vis5` implies `dusty_day`; dusty bins
≤ observed bins, and `n_bins_dusty > 0` exactly on `dusty_day` days; every `valid_day*` matches its
definition; one row per station-day; day-to-day changes only between valid days.

Planned gold products (not built yet): `stations` catalog, `climatology`, `diurnal`, `trends`, Uth,
code-6 diagnostic.

### 7.4 Gold `three_hourly`
One row per station per 3-hour UTC bin (00 = 00:00-02:59, ..., 21), for every bin with a report.
Built from silver first in each gold run; `daily` is built from it (method 2.0.0). Columns
(definitions in Part 2, section 10): `station_id, bin_time, lmst_date, lmst_hour, state_time_utc,
state_report_type, wsd, wind_dir, t, td, rh, slp, vis, dup6, gust_max, wsd_max, vis_min, observed,
observed_explicit, dusty, c_{suspended, blowing, whirls, storm, dust_haze, screened},
dust_synop, dust_metar, dust_other, dust_vis5, nil_inferred, nil_metar, first_dust_time_utc, vicinity_dust, past_weather_dust, remarks_dust, n_reports, n_reports_pw,
n_synop, n_metar, n_speci, n_other`.

The state values come from one report per bin (the first regular SYNOP or METAR), chosen with a
window function: `row_number() OVER (PARTITION BY station_id, bin_time ORDER BY ...)` numbers the
reports of each bin, and row 1 is kept. "Regular" means the silver `source_group` synop, or metar
without the special reports (FM-16, SAOSP). The station's LMST offset (one per station, from its
most common `lon_used`) is a CTE that picks, per station, the longitude with the most rows
(`QUALIFY row_number() ... = 1`, ties to the smallest).

Checks run before activation (each must return 0 rows): one row per station and bin; bins start
at 00, 03, ..., 21 on the full hour; the state report lies inside its bin; observed reports ≤
reports, and the type counts add up to `n_reports`; `dusty` is null exactly when nothing was
observed; no source verdict (`dust_synop`, `_metar`, `_other`, `dust_vis5`) without an
observation; an observed bin is dusty exactly when a source saw dust; `dust_vis5` implies `dusty`;
`first_dust_time_utc` is set exactly when the bin is dusty.

Adding a gold product is one entry in `aggregate.PRODUCTS` (name, SQL file, checks) plus the SQL
file; `build_gold` runs every entry against the same silver view.

### 7.5 Gold statistics: `monthly`, `yearly`, `decadal`
Built from `daily` in the same gold run. `PRODUCTS` runs in list order and `build_gold` re-reads
each written table as a view of its name, so `monthly.sql` reads the view `daily` (already checked)
and `yearly.sql` and `decadal.sql` read `monthly`.
`monthly`: one row per station and month of `lmst_date`, key `station_id, year, month`; columns and
definitions in Part 2, section 11. Every additive count starts with `n_` and is `BIGINT` (DuckDB
sums `BIGINT` into `HUGEINT`, so sums are cast back); only rates and visibility values are
`DOUBLE`. `yearly` (key `station_id, year`) sums every monthly `n_` column with one DuckDB
expression, `CAST(sum(COLUMNS(c -> starts_with(c, 'n_') AND c NOT IN (...))) AS BIGINT)`, which
keeps each column's name; the excluded columns are `n_calendar_days` (recomputed) and the visibility
counts (visibility is reported in `monthly` only). So a new monthly count reaches `yearly` without
editing `yearly.sql`, as long as it follows the `n_` contract. `decadal` (key `station_id,
decade, period`) maps each month to its three periods (`annual`, its season, itself) with a small
`periods` table built in SQL, joins it to `monthly`, and sums the same way per station, decade and
period; a `calendar` CTE gives each period's days over the decade's 10 years, and
`dusty_days_mean` = `dusty_freq * n_calendar_days / 10`. The three rates (`dusty_freq`,
`dusty_freq_obs`, `validity_ratio`) are written out from explicit counts in `monthly.sql` and from
explicit sums in `yearly.sql` and `decadal.sql` (a column made by `COLUMNS(...)` cannot be referred
to by name in the same SELECT). All three tables are written sorted by
their key (as `daily` and `three_hourly`), so the same input gives the same file. The coverage threshold
`products.min_valid_days_month` reaches the SQL as the variable `min_valid_days_month`.

Checks run before activation (each must return 0 rows): one row per station and month; days,
valid days and dusty days recomputed from `daily` match; for each dusty-day definition
(`aggregate.STAT_VARIANTS`) dusty valid days ≤ valid days ≤ observed days ≤ days ≤ calendar days
and dusty valid days ≤ dusty days at any coverage; `dusty_freq` is null exactly when there is no
valid day, `dusty_freq_obs` exactly when there is no observed day (and lies in 0-1),
`validity_ratio` exactly when one of its two shares is undefined; and `dusty_freq` =
f / (f + r(1 − f)) with f = `dusty_freq_obs` and r = `validity_ratio` (Part 2, 11.4), within
1e-9, which ties the three rates together. These come from `aggregate._count_order_checks(table)`, shared by the statistics
tables. `yearly` adds: one row per station and year; days, observed, valid and dusty days and
observed bins equal the sums over `monthly`; covered ≤ valid ≤ observed months ≤ 12. `decadal`
adds: one row per station, decade and period; decades start in a year ending in 1; periods are
known names; days, valid days, dusty days and dusty days at any coverage of the `annual` rows equal
the `yearly` sums over the decade, and the 12 months and the 4 seasons each add up to `annual`;
dust years ≤ valid years ≤ observed years ≤ 10 and dust years ≤ dust years at any coverage;
`dusty_days_mean` is null exactly when `dusty_freq` is.

### 7.5a Gold `stations`
One row per station in silver, built **first** in every gold run from the view `silver_all` (all
silver files), the saved isd-history and the override file (`stations.py`, registered as DuckDB
views `isd_history` and `overrides`). Columns (Part 2, section 4.3): `station_id, status,
computed_status, in_gold, position_ok, is_dusty_station, n_dusty_days, n_dusty_years,
trusted_lat, trusted_lon, file_lat, file_lon, isd_name, isd_lat,
isd_lon, max_km, first_year, last_year, n_years, n_dusty_reports, override_action,
override_reason, isd_history_md5, overrides_md5`. Right after it, `build_gold` creates the view
`silver` = `silver_all` restricted to the stations with `in_gold`, which every later product reads,
and writes `stations_review.csv`. Checks: one row per station; known statuses; every station in
gold has a trusted longitude; `in_gold` = `position_ok AND is_dusty_station` (`trusted_lat` can be null for an `unverified` station without an
in-file latitude: gold uses only the longitude); no `conflict` in gold. `three_hourly` and `daily`
also check that they hold only stations in gold (`stations_in_gold`).

### 7.6 Provenance
- Bronze metadata: `dcdr_layer, source_md5, source_key, run_id`.
- Silver metadata: `dcdr_layer, method_version, config_hash, run_id, source_md5,
  prev_source_md5, nil_guard, metar_guard` (`1` pass, `0` fail, empty = no decision),
  `lon_fill` (the isd-history longitude that reports without a position took; `none` = the
  year median; empty = no report needed one, absent in files before 2.1.0).
- Carried silver files (`_state/silver_carried.parquet`): `station_id, year, built_version,
  built_run_id, valid_for, migration, carried_run_id`; a row counts only while the file's own
  `method_version` and `run_id` match it.
- Gold lineage (`silver_version.json`) also records `silver_files` and `silver_carried`.
- Gold lineage (`silver_version.json`): `silver_method_version, silver_config_hash,
  isd_history_md5, overrides_md5` (empty when there is no override file).
- Run log: `run_id, command, started_at, finished_at, status, git_commit, git_dirty,
  dcdr_version, config_hash, summary` (JSON with counts and failures).

## 8. How to

**Look inside a Parquet file**
```bash
uv run python -c "import polars as pl; print(pl.read_parquet_schema('PATH'))"          # columns
uv run python -c "import polars as pl; print(pl.read_parquet('PATH', n_rows=5))"       # first rows
uv run python -c "import duckdb; print(duckdb.sql(\"SELECT report_type, count(*) FROM 'PATH' GROUP BY 1\"))"
uv run python -c "from dcdr.state import read_parquet_metadata; print(read_parquet_metadata('PATH'))"
```
Interactive: `uv run jupyter lab`; VS Code "Data Wrangler"; `duckdb -ui` (DuckDB CLI).

**Use the active gold version**
```bash
uv run python -c "import duckdb; g='$DCDR_DATA/gold/versions/'+open('$DCDR_DATA/gold/CURRENT').read().strip()+'/daily/part-000.parquet'; print(duckdb.sql(f\"SELECT station_id, count(*) FILTER (WHERE valid_day) AS valid_days FROM '{g}' GROUP BY 1\"))"
```

**Compare raw text with decoded values** (checks the decoding)
```bash
uv run python -c "import duckdb; s='SILVER.parquet'; b='BRONZE.parquet'; print(duckdb.sql(f\"SELECT b.DATE, b.WND, s.wsd, b.MW1, s.mw1_code, s.dust_class_final FROM '{b}' b JOIN '{s}' s ON strptime(b.DATE,'%Y-%m-%dT%H:%M:%S')=s.time_utc AND trim(b.REPORT_TYPE)=s.report_type AND trim(b.SOURCE)=s.source LIMIT 20\"))"
```

**Change a method setting**: write a profile with only the changed keys, e.g.
`classify: {haze_window_h: 12}`, and run with `--profile my.yaml`. Silver rebuilds automatically.

**Regenerate the pilot station lists**: run `notebooks/01_duisd_v1_dusty_stations.ipynb`
(needs `legacy/duISDv1/`).

**Download the whole ISD archive** (all 30,375 stations, 711,411 station-year files, 1.47 TB of
CSV, 1901-2025; about 115 GB of bronze; roughly a day on a home connection). Run it yourself in a
terminal; it does not need Claude.

1. Make the station list (about 20 s; lists NOAA's S3 bucket, writes `config/stations/all_isd.csv`
   with one column, `station_id`):
   ```bash
   uv run python scripts/list_isd_stations.py
   ```
2. Download year by year, detached from the terminal. One `dcdr ingest` per year keeps each run
   short; `nohup ... &` lets it survive closing the terminal; `caffeinate -i` keeps the Mac awake
   (it still stops on shutdown, sleep with the lid closed on battery, or if the disk is
   unplugged). The `cd` and `export` inside make it work from any folder: `uv run dcdr` only
   works inside the project, and the station list path is relative.
   ```bash
   nohup caffeinate -i zsh -c 'cd /Users/xinxi/Projects/DCDR && export DCDR_DATA=/Volumes/CrucialX8/dcdr && for y in {1901..2025}; do uv run dcdr ingest --stations config/stations/all_isd.csv --years $y || echo "YEAR $y FAILED"; done' >> /Volumes/CrucialX8/dcdr/_state/ingest_all_isd.log 2>&1 &
   ```
3. Watch progress: each run logs a `started (ingest)` line, a progress line every 500 files
   (`3500/12949 files, 41 MB/s, 0 failed, about 9 min left`) and a `run ...` summary per year.
   Ctrl-C stops only the watching.
   ```bash
   tail -f /Volumes/CrucialX8/dcdr/_state/ingest_all_isd.log
   grep -c "^run " /Volumes/CrucialX8/dcdr/_state/ingest_all_isd.log     # finished year runs
   pgrep -fl "dcdr ingest"                                               # is it running?
   ```
4. Stop it (loses at most the last 500 files):
   ```bash
   pkill -f "zsh -c cd /Users/xinxi/Projects/DCDR"; pkill -f "dcdr ingest"
   ```
5. Resume: run step 2 again, optionally from the first unfinished year (`{2014..2025}`). Files
   already recorded in the manifest are skipped; each run also re-lists the year before (haze
   look-back), which costs a second.
6. Check it is complete: every `run ...` line shows `'failed': 0`, and the manifest
   (`_state/sources.parquet`) holds as many `ok` files per year as S3 lists.

Notes: `listed`/`selected`/`skipped` in a run summary include the previous year's files (already
downloaded by the previous step, so skipped). Files written by a run that was stopped before its
next checkpoint are not in the manifest yet and are downloaded again (overwritten). Adapt the
paths (`/Users/xinxi/Projects/DCDR`, `/Volumes/CrucialX8/dcdr`) to your machine.

**Rebuild silver for the whole ISD** (about 711,000 station-years; with `--workers 8` a few
hours, serially about two days). Run it yourself in a terminal. `caffeinate -ims` keeps the Mac
awake while it runs; `tee` keeps a copy of the log.
```bash
cd /Users/xinxi/Projects/DCDR && export DCDR_DATA=/Volumes/CrucialX8/dcdr
caffeinate -ims uv run dcdr process --years 1901-2025 --stations config/stations/all_isd.csv --workers 8 2>&1 | tee -a ~/dcdr_process_all_isd.log
grep "silver: " ~/dcdr_process_all_isd.log | tail -1     # progress (every 500 stations)
```
Ctrl-C stops it; files already written stay, and the next run skips them (it still recomputes
each station's guards, a few minutes per thousand stations). More workers than cores does not
help; each worker needs about 0.2 GB of memory. Station-years without a position (rules.md
4.2) are not built and are counted as `no_position` in the progress lines and the run summary,
not as `failed`; a silver file from an earlier method for such a year is deleted (`removed`, with
a warning line). `failed` is left for real errors.

### After a method change: targeted rebuild
When `METHOD_VERSION` moves on and the change can only affect some stations, declare it in
`src/dcdr/process/migrations.py` (with a test of `affects`, as for 2.1.0):

```python
MIGRATIONS["2.2.0"] = Migration("2.1.0", "2.2.0", "what changed", affects_function)
```

`affects(raw)` gets one bronze station-year and must return True if the change can alter the
station's silver in any way (when in doubt, True). Then:

```bash
caffeinate -ims uv run dcdr process --years 1901-2025 --stations config/stations/all_isd.csv --workers 6 2>&1 | tee -a ~/dcdr_process.log
uv run dcdr verify-silver --sample 1000      # re-process 1000 carried files and compare
```
The process summary counts `built`, `carried`, `skipped`, `no_position` and `removed`. Every
station's bronze is still read once (guards and `affects`), about an hour for the whole ISD;
only affected stations are built. A change that can affect every station (QC, common codes) gets
no migration: it rebuilds in full. Before a published release, rebuild in full
(`dcdr process --force`) rather than relying on carried files.

## 9. Design decisions (engineering)

1. **Bronze keeps everything.** Ingest stores the whole CSV, as text. Choosing columns happens in
   `parse.py`. Reason: unknown future needs; re-downloading is expensive. Example: the `REM`
   column (raw SYNOP text) later provided the SYNOP iX indicator.
2. **Incremental, reproducible runs.** Downloads are skipped when the S3 ETag/size is unchanged
   (manifest). Silver is skipped when its inputs, method version and settings are unchanged
   (file metadata). A changed source file rebuilds only what it affects. A method change
   rebuilds everything unless it declares a migration: then files it cannot affect are carried
   forward (ledger) and checked by sample (`verify-silver`). Why: 2.1.0 changes 0.65 % of
   station-years, and a full rebuild takes 13-16 hours (2026-11-04). The ledger keeps files
   untouched: rewriting their metadata would mean rewriting every file.
3. **Atomic writes.** Write `name.tmp`, then rename: a crash never leaves a half-written file.
4. **Versioned gold with a pointer.** Each build goes to a new folder; `CURRENT` moves only after
   all checks pass; the last 3 versions are kept (rollback = edit `CURRENT`).
5. **Fail loudly, never silently.** Data contracts before writing silver; gold refuses mixed or
   stale silver; one failing station-year is logged without stopping the others.
6. **Configuration over constants.** Every method choice is a validated config key, fingerprinted.
7. **Packaging.** SQL and default settings ship inside the package (`importlib.resources`); user
   paths resolve against the working directory; no code assumes the repository layout.
8. **SQL for aggregation.** Gold logic is a readable `.sql` file run by DuckDB (in-process,
   no server).

## 10. Testing and quality checks

```bash
uv run pytest                  # unit + integration tests (offline; ~300 tests)
uv run pytest -m network       # smoke test against the real NOAA bucket
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```
Tests use small synthetic ISD rows (`tests/fixtures/isd.py`) and an in-memory fake S3. CI
(`.github/workflows/ci.yml`) runs all checks on every push. New behaviour is added test-first.

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Hidden `._name` files next to data on the SSD | macOS metadata on exFAT disks | Harmless; the pipeline ignores them |
| `FileNotFoundError` while pruning gold (before 2026-10-19) | macOS deletes `._x` together with `x` | Fixed (`aggregate._ignore_vanished`) |
| Data written to `./data` instead of the SSD | `DCDR_DATA` not set in that shell | `export DCDR_DATA=...` (add to `~/.zshrc`) |
| `StaleSilverError` from `aggregate` | silver built by other code/settings | `dcdr process --years ...` (or `--force`) |
| `MixedSilverVersionsError` from `aggregate` | some silver files not yet rebuilt or carried to the running version (e.g. a stopped `process`) | finish `dcdr process` over the same years and stations |
| `verify-silver` reports differences | a migration's `affects` misses a change | remove the migration from `MIGRATIONS` and rebuild (`dcdr process --force`); report it in the worklog |
| A notebook shows as modified in git | JupyterLab re-saved it | `git restore <notebook>` if nothing changed |
| `No magic bytes found at end of file '…/._x.parquet'` in a query | a glob `*.parquet` matched macOS `._` files on exFAT | use `[0-9A-Z]*.parquet` (station IDs start with a digit or a capital letter) |

## 12. Roadmap (sub-projects)

A. pipeline (this package) · B. publishing (Hugging Face, Zenodo) · C. website (map, time
series) · D. environmental drivers (`enrich`) · E. dust forecasting at source stations
(`forecast`, trained on GEFSv12 reforecasts). Design: the original spec (`docs/superpowers/specs/2026-10-15-dcdr-pilot-design.md`, local only).
