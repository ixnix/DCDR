# DCDR documentation, Part 1: Code design

How the `dcdr` package is built and used: structure, commands, inputs, outputs and design.
Part 2 ([rules.md](rules.md)) gives the data rules as implemented; [rules_history.md](rules_history.md)
how they came about.
The running record of decisions and changes is [worklog.md](worklog.md).

> Living document. It describes the code **as it is now** and is updated with every change.
> Last updated: 2026-10-30 (method version 1.10.0).

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
│   │   ├── sampling.py         one report per 3-hourly synoptic slot
│   │   ├── guard.py            station guards for inferred "nothing significant" (V3; METAR)
│   │   ├── derived.py          are a station-year's SYNOPs copies of its METARs?
│   │   └── build.py            runs the above for one station-year, writes silver
│   ├── aggregate.py            silver -> gold (versioned, checked, CURRENT pointer)
│   ├── sql/daily.sql           the gold `daily` table, in SQL (run by DuckDB)
│   ├── sql/three_hourly.sql    the gold `three_hourly` table, in SQL
│   └── schemas.py              the silver data contract
├── config/stations/            run inputs: station lists and region definitions
├── notebooks/                  (local only, not in the public repo) analyses (01: pilot station selection; 02: validating inferred "nothing significant";
│                               03: METAR present weather; 04: slot sources over the full record)
├── scripts/                    one-off scripts (thin_slice_report.py; fetch_references.py;
│                               list_isd_stations.py: station list of the whole ISD archive)
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
environment). Every pipeline command records a run (section 7.5).

| Command | What it does | Main options |
|---|---|---|
| `dcdr ingest` | List NOAA's S3 files for the years; download new or changed station-years into bronze; saves the download record and logs progress every 500 files, so a stopped run resumes where it left off; re-downloads recorded files missing from disk | `--years`, `--stations` |
| `dcdr process` | Build silver for station-years that are missing or out of date | `--years`, `--stations`, `--force` |
| `dcdr aggregate` | Build a new gold version from silver, check it, activate it | `--years`, `--stations` |
| `dcdr run` | `ingest`, then `process`, then `aggregate` | as above |
| `dcdr status` | Download status counts and the five latest runs (read-only) | |
| `dcdr --version` | Package version | |

Common options:
- `--years`: `2024`, `2020-2024` or `2019,2021-2023` (required for pipeline commands).
- `--stations FILE`: CSV with a `station_id` column. Default: `stations.station_list` in the config
  (currently `config/stations/pilot_100.csv`).
- `--config FILE`: replace the built-in `default.yaml`. `--profile FILE`: override some keys.
- `--force` (process, run): rebuild silver even if it is up to date.

Examples:

```bash
export DCDR_DATA=/Volumes/CrucialX8/dcdr          # where data live (see 5.1)
uv run dcdr run --stations config/stations/pilot_20.csv --years 2020-2024
uv run dcdr status
```

`ingest` also downloads the year before the first requested year: the haze rule looks back 24 h,
which on 1 January reaches the previous year.

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
| `sampling` | synoptic window, excluded report types, slot sources (`sources`: SYNOP and METAR report types, in order of preference), off-grid schedules (`schedule_*`) | yes |
| `products` | valid-day threshold, visibility limit of `dusty_day_vis5` (`vis5_max_m`), gold versions kept | no (gold only) |
| `stations` | default station list | no |

Precedence: `default.yaml` < `--profile` < test overrides < `DCDR_DATA`.

### 5.3 Fingerprints
- `config_hash`: SHA-256 (16 hex) of all resolved settings; stored in every run record.
- `silver_config_hash`: hash of the silver sections only; stored in every silver file. If it
  changes, silver is rebuilt.
- `METHOD_VERSION` (`src/dcdr/process/__init__.py`, currently **1.8.0**): bumped whenever
  processing logic changes; stored in every silver file. If it changes, silver is rebuilt.

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

Station-list columns: `station_id, name, country, lat, lon, region, aridity_index`. Only
`station_id` is required by the pipeline.

## 7. Outputs

Everything goes under the data root:

```
<data_root>/
├── bronze/year=YYYY/<station_id>.parquet
├── silver/year=YYYY/<station_id>.parquet
├── gold/
│   ├── CURRENT                          text file: run ID of the active version
│   └── versions/<run_id>/
│       ├── silver_version.json          which silver (method version, config hash) built it
│       ├── daily/part-000.parquet
│       └── three_hourly/part-000.parquet
└── _state/
    ├── sources.parquet                  download manifest (one row per S3 file)
    ├── runs.parquet                     run log (one row per command run)
    └── runs/<run_id>/config.resolved.yaml   exact settings of each run
```

`year=YYYY` folders are "Hive-style" partitions: DuckDB and Polars can filter by year from the
folder name. Parquet files carry provenance in their footer metadata (section 7.5).

### 7.1 Bronze
All columns of the NOAA CSV, **as text**, unchanged, plus `source_key`, `source_md5`,
`ingest_run_id`. Lossless by design (see 9.1). About 9 % of the CSV size (Parquet + zstd).

### 7.2 Silver
One row per report (exact duplicates collapsed), about 250 columns: decoded values, QC flags,
local time, dust class and evidence, synoptic selection. Core columns come first:

| Group | Columns |
|---|---|
| Identity, time | `station_id, time_utc, time_lmst, lmst_date, lmst_hour, summary_lmst_date, report_type, source, lat, lon, lon_used, elev` |
| Sampling | `slot_time, slot_lmst_date, slot_delta_min, slot_offset_min` (0, or ±60 for an off-grid schedule), `is_candidate, is_excluded_type, slot_source` (synop/metar), `source_rank, is_source_selected` (the slot's report for its source), `selection_rank, selection_reason, is_synoptic_selected` (the slot's primary report: its SYNOP if any, else its METAR) |
| Weather observed? | `has_pw`, `pw_status` (reported, nil_significant, nil_inferred, nil_metar, not_observed), `syn_ix`, `metar_text` (full, fragment), `metar_auto`, `metar_wx` (missing, group, cavok, none), `metar_rmk_dust`, `synop_derived` |
| Dust | `dust_rank, dust_class_rule, dust_rule_id, dust_class_final, dust_haze, rh_screened, flag_du, flag_duhz, flag_susp, flag_bd, flag_ds, flag_vicinity, flag_past, flag_ambiguous, flag_daily_dust, pw_evidence` |
| Weather values | `wsd, use_wsd, wind_dir, use_wind_dir, gust, use_gust, gust_src, vis_raw, vis_capped, use_vis, t, use_t, td, use_td, rh, slp, use_slp` |
| Bookkeeping | `parse_status, dup_count, row_key, source_key` |
| Raw decoded fields | e.g. `mw1_code, mw1_qc, au1_obsc, ay1_code, ...` (alphabetical) |

Meanings are in Part 2. The contract (`schemas.py`) is checked before every write: types,
allowed values, unique `row_key`, at most one primary report and one report per source per slot.

### 7.3 Gold `daily`
One row per station per local (LMST) day. Columns (definitions in Part 2, section 9):
`n_reports, n_slots, n_slots_pw, valid_day, dusty_day, n_slots_pw_explicit, valid_day_explicit,
n_slots_nil_inferred, n_slots_nil_metar, n_slots_synop, n_slots_pw_synop, valid_day_synop,
dusty_day_synop, n_slots_metar, n_slots_pw_metar, valid_day_metar, dusty_day_metar,
n_slots_pw_vis5, valid_day_vis5, dusty_day_vis5, dusty_day_any, past_weather_dust,
daily_summary_dust, vicinity_dust_day, remarks_dust_day, dusty_day_any_source, n_dust_slots_{suspended, blowing,
whirls, storm, dust_haze}, n_slots_rh_screened, dust_first_lmst_hour, wsd_mean, wsd_max, gust_max,
vis_min, vis_hmean, n_vis_zero, n_vis_from_metar, t_mean, t_min, t_max, td_mean, rh_mean, slp_mean, dup6_mean,
d_t_mean, d_slp_mean`.
Which dusty-day column to use (each with its own `valid_day*`): `dusty_day` for the fullest count
(SYNOP or METAR per slot); `dusty_day_synop` to compare with duISD v1 or SYNOP-only stations;
`dusty_day_vis5` to compare stations or years with different report mixes; see Part 2,
sections 5.2, 5.3 and 9.

Checks run before activation (each must return 0 rows): at most 8 slots per day; observed slots ≤
slots; explicit, per-source, inferred, implied and vis5-judgeable slots ≤ observed slots; per-source
slots ≤ slots; `dusty_day` = `dusty_day_synop` OR `dusty_day_metar`; the narrower dusty-day flags
imply `dusty_day`; every `valid_day*` matches its definition; dusty days have slots; one row per
station-day; day-to-day changes only between valid days.

Planned gold products (spec §7.3, not built yet): `monthly`, `annual`,
`station_year_coverage`, `stations` catalog, `climatology`, `diurnal`, `trends`, Uth, code-6 diagnostic.

### 7.4 Gold `three_hourly`
One row per station per 3-hour UTC bin (00 = 00:00-02:59, ..., 21), for every bin with a report.
Built from silver in the same gold run as `daily`, but independently of it (no slots). Columns
(definitions in Part 2, section 10): `station_id, bin_time, lmst_date, lmst_hour, state_time_utc,
state_report_type, wsd, wind_dir, t, td, rh, slp, vis, dup6, gust_max, vis_min, observed,
observed_explicit, dusty, c_{suspended, blowing, whirls, storm, dust_haze, screened},
first_dust_time_utc, vicinity_dust, past_weather_dust, remarks_dust, n_reports, n_reports_pw,
n_synop, n_metar, n_speci, n_other`.

The state values come from one report per bin (the first regular SYNOP or METAR), chosen with a
window function: `row_number() OVER (PARTITION BY station_id, bin_time ORDER BY ...)` numbers the
reports of each bin, and row 1 is kept. "Regular" means a slot source type: `build_gold` passes the
config lists `sampling.sources.synop` and `.metar` to the SQL as DuckDB variables `synop_types` and
`metar_types` (`SET VARIABLE`, like `min_slots`), so the SQL and sampling can't drift apart.

Checks run before activation (each must return 0 rows): one row per station and bin; bins start
at 00, 03, ..., 21 on the full hour; the state report lies inside its bin; observed reports ≤
reports, and the type counts add up to `n_reports`; `dusty` is null exactly when nothing was
observed; `first_dust_time_utc` is set exactly when the bin is dusty.

Adding a gold product is one entry in `aggregate.PRODUCTS` (name, SQL file, checks) plus the SQL
file; `build_gold` runs every entry against the same silver view.

### 7.5 Provenance
- Bronze metadata: `dcdr_layer, source_md5, source_key, run_id`.
- Silver metadata: `dcdr_layer, method_version, config_hash, run_id, source_md5,
  prev_source_md5, next_year_present, nil_guard` (`1` pass, `0` fail, empty = no decision).
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

## 9. Design decisions (engineering)

1. **Bronze keeps everything.** Ingest stores the whole CSV, as text. Choosing columns happens in
   `parse.py`. Reason: unknown future needs; re-downloading is expensive. Example: the `REM`
   column (raw SYNOP text) later provided the SYNOP iX indicator.
2. **Incremental, reproducible runs.** Downloads are skipped when the S3 ETag/size is unchanged
   (manifest). Silver is skipped when its inputs, method version and settings are unchanged
   (file metadata). A changed source file rebuilds only what it affects.
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
| A notebook shows as modified in git | JupyterLab re-saved it | `git restore <notebook>` if nothing changed |
| `No magic bytes found at end of file '…/._x.parquet'` in a query | a glob `*.parquet` matched macOS `._` files on exFAT | use `[0-9A-Z]*.parquet` (station IDs start with a digit or a capital letter) |

## 12. Roadmap (sub-projects)

A. pipeline (this package) · B. publishing (Hugging Face, Zenodo) · C. website (map, time
series) · D. environmental drivers (`enrich`) · E. dust forecasting at source stations
(`forecast`, trained on GEFSv12 reforecasts). Design: the original spec (`docs/superpowers/specs/2026-10-15-dcdr-pilot-design.md`, local only).
