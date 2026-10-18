# DCDR pilot: design specification

| | |
|---|---|
| **Status** | Draft for review |
| **Date** | 2026-10-15 |
| **Owner** | Xin Xi (Michigan Technological University) |
| **Working name** | DCDR (Dust Climate Data Record). The public name (DCDR or duISD v2) is decided before the first public release (§9). |
| **Scope** | Pilot vertical slice: sub-projects **A** (pipeline), **B** (publish) and **C** (website), for **100 stations × all available years**. It is designed to scale to all ISD stations with monthly incremental updates. |

---

## 1. Purpose

Build a reproducible, quality-controlled, cloud-ready record of dust events observed at surface weather stations. The source is NOAA's Integrated Surface Database (ISD) on AWS. The data is published openly and explored through an interactive website.

The project has two goals with equal weight:
1. A scientifically defensible, citable dataset (the successor to duISD v1; Xi 2021, *JGR-Atmospheres*).
2. A portfolio piece showing industry-relevant data-science, ML-engineering and data-engineering skills, in that order of priority, at $0 running cost.

## 2. Scope

### In scope for this spec (pilot)
- **A.** Ingest ISD from S3 → bronze → silver → gold, for 100 pilot stations and all of their available years.
- **B.** Versioned release of gold products (Hugging Face Datasets; Zenodo for full releases), with a data card.
- **C.** A static website: a station map coloured by climatological statistics, filters, and per-station time series (daily, monthly, annual), seasonal cycle and diurnal cycle.
- Scaling mechanisms: station discovery, incremental updates, rebuild policy.
- A monthly update workflow, built at the end of the pilot.

### Out of scope (later specs)
- **D.** Environmental drivers (ERA5, MODIS, soil moisture, gridded aridity), covariation and trends against drivers.
- **E.** Dust-event forecasting (decisions so far are recorded in Appendix A).
- Gridded products (Zarr), data downloads on the website, cloud deployment, Dagster orchestration.

## 3. Decisions already made

| # | Decision | Rationale |
|---|---|---|
| D1 | Source: `s3://noaa-global-hourly-pds/{year}/{station}.csv` (public, anonymous, us-east-1) | Same CSV format as the legacy code; no local raw archive. Measured: 1.47 TB, 711,411 station-year files. |
| D2 | **Stream** source files (download into memory → Parquet). No local CSV mirror. | User preference. Bronze provides a lossless copy at about 7% of the CSV size. |
| D3 | All compute on the user's laptop (Apple M5, 10 cores, 16 GB) for A–C | Benchmarked: Polars parses at about 1.2 GB/s; the network (~13 MB/s) is the bottleneck, about 30 h for the full backfill |
| D4 | Data root: `./data` relative to the working directory by default (git-ignored in the repo); any location via `DCDR_DATA` or a profile. The author runs it on a portable SSD (`/Volumes/CrucialX8/dcdr`, exFAT) by choice; backups are Areca RAID snapshots. The data root is created if missing (user decision 2026-10-17). | Works out of the box after a clone; storage location is a user choice, not a rule |
| D5 | Stack: Python 3.12, uv, Polars, DuckDB, Parquet/GeoParquet, Pandera, Pydantic, Typer, pytest, ruff, mypy, GitHub Actions | See §4 |
| D6 | Method choices are **explicit config options**. Old behaviour is reproducible via the `duisd_v1` profile for comparison (not validation). | User decision: no validation against v1 is needed |
| D7 | Station products use **local mean solar time** (LMST = UTC + longitude/15 h); UTC is kept for merging | Dust has a diurnal cycle; daily products should follow the local day |
| D8 | Dusty day = any dust in the **synoptic-selected** reports of that LMST day (`dusty_day_synoptic`). `dusty_day_any` is also stored. | Consistency across stations and decades; immune to changes in METAR/SPECI reporting frequency |
| D9 | **Published set** = all *dusty* stations: ≥1 dusty day in ≥5 qualifying years | User decision |
| D10 | **Source set** (ML only) = aridity index < 0.5 **and** median annual dusty days > 14 over the baseline | User decision; dusty *days*, not % frequency, because observation frequency is uneven |
| D11 | Classification baseline 1991–2020; products cover **all** years | Coverage is best after about 1973; products keep the full history |
| D12 | WMO code 6 is **not** assumed to mean transported dust | User experience: observer coding errors are common. A diagnostic is computed instead (§7.3.8). |
| D13 | DUP6 only (fixed threshold 6 m/s); no DUP based on station Uth | User decision |
| D14 | Uth is computed **only for source stations** | Uth measures soil erodibility and only applies at sources |
| D15 | ISD rain and snow are kept but marked low reliability and excluded from trends | They are often missing and biased; gridded products are used in D |
| D16 | Pilot = 100 stations chosen by a reproducible script, reviewed by the user | §8.3 |
| D17 | Website: static site + DuckDB-WASM; pilot data served from the same site | $0, never sleeps; avoids Hugging Face browser CORS limits (§10) |
| D18 | No validation against duISD v1 | User decision |

## 4. Technology stack

| Concern | Tool | Notes |
|---|---|---|
| Environment and packaging | **uv**, Python 3.12, `src/dcdr` layout | Locked dependencies (`uv.lock`) |
| S3 access | **obstore** (anonymous) | Listing and streaming download; MD5 computed while downloading |
| Parsing, QC, classification, sampling | **Polars** | Detailed, testable Python logic |
| Gold products | **DuckDB SQL** (`sql/*.sql`) | Readable SQL; spills to disk |
| Statistics | scipy, **pymannkendall** (modified Mann–Kendall), statsmodels | Trends, model fits |
| Geospatial | **rasterio** (aridity raster), **GeoPandas** (GeoParquet catalog) | Only where geometry is needed |
| Data contracts | **Pandera** (Polars backend) | Schema + value checks for each layer |
| Config | YAML + **Pydantic** | Validated; resolved config saved per run |
| CLI | **Typer** | Thin; calls `pipeline.py` |
| Tests and quality | pytest, ruff, mypy, pre-commit | CI on GitHub Actions |
| Website | **Observable Framework**, **MapLibre GL JS**, **Observable Plot**, **DuckDB-WASM** | Static build on GitHub Pages |
| Publishing | `huggingface_hub`, Zenodo REST API (full releases only) | Versioned releases |

## 5. Repository layout

```
dcdr/
├── pyproject.toml, uv.lock, README.md, .gitignore, .pre-commit-config.yaml
├── src/dcdr/
│   ├── cli.py          # Typer entry point; parses arguments and calls pipeline functions
│   ├── pipeline.py     # stage runner: plans work, applies the rebuild policy, writes run records
│   ├── config.py       # Pydantic settings; profile merging; resolved config + hash
│   ├── state.py        # source manifest + run records (_state/)
│   ├── ingest.py       # S3 listing, streaming download, bronze writer
│   ├── silver/
│   │   ├── parse.py    # ISD packed fields → typed columns; time fields
│   │   ├── qc.py       # QC flags (never deletes), RH, visibility harmonization
│   │   ├── classify.py # per-field weather-code tables → dust classes + evidence + rule id
│   │   └── sampling.py # synoptic-slot candidates, ranks, selection reasons
│   ├── gold.py         # product registry; runs sql/*.sql in DuckDB; Python products (trends, Uth)
│   ├── schemas.py      # Pandera contracts: bronze, silver, gold
│   ├── sql/            # one file per gold table (shipped in the wheel)
│   ├── default.yaml    # built-in defaults (shipped in the wheel)
│   └── publish.py      # web export, releases (Hugging Face, Zenodo), release manifest
├── config/             # run inputs, resolved relative to the working directory
│   ├── profiles/duisd_v1.yaml
│   └── stations/       # regions.yaml, pilot_100.csv
├── scripts/select_pilot_stations.py
├── tests/{unit,integration,fixtures}/
├── web/                # Observable Framework site (reads published web-export files only)
├── docs/               # specs, plans, data card, method notes
├── legacy/             # original scripts and station_history/ (never imported)
└── .github/workflows/  # ci.yml, deploy-site.yml, monthly-update.yml
```

Rule: a module becomes a package only when it grows past about 400 lines.

## 6. Data layout and storage model

Data root `DCDR_DATA` (default `<repo>/data`; may be `s3://…` later):

```
_state/sources.parquet                       # source manifest (§7.1.3)
_state/runs.parquet                          # one row per run
_state/runs/<run_id>/config.resolved.yaml
inputs/aridity/ai_v3_yr.tif                  # CGIAR Global Aridity Index v3 (copied from legacy/station_history)
inputs/isd-history.csv                       # NOAA station history (refreshed by ingest)
bronze/year=YYYY/<station_id>.parquet        # one file per station-year
silver/year=YYYY/<station_id>.parquet        # one file per station-year
gold/versions/<run_id>/<table>/…parquet      # complete gold build
gold/CURRENT                                 # text file containing the active <run_id>
releases/<version>/…                         # local copy of each release
web_export/<version>/…                       # files consumed by the website
```

### 6.1 Write safety (tested on the CrucialX8 exFAT volume, 2026-10-15)
- **Single files** are written as `<name>.tmp` and then `os.replace`d onto the final name. Replacing a file by rename was verified to work on the volume. A failed write leaves the previous file intact.
- **Directories cannot be swapped in one step** (verified: `os.replace` onto a non-empty directory fails). Multi-file outputs (gold) are therefore built in a new `versions/<run_id>/` directory and activated by rewriting the single `CURRENT` pointer file. The last 3 gold versions are kept (config).
- Discovery never relies on a broad glob. Bronze and silver paths come from the manifest and the station list. Gold is read through `CURRENT`. As defence in depth, any directory listing excludes names starting with `.` (covers `._*` AppleDouble files and `.DS_Store`) and ending in `.tmp`.
- Bronze and silver files carry Parquet key-value metadata: `dcdr_layer`, `method_version`, `config_hash`, `run_id`, `source_md5`.

### 6.2 Upgrade path (not built in the pilot)
If full-scale silver (about 700k files) needs fast whole-archive scans, adopt a table format (Apache Iceberg or DuckLake) for silver and gold. It provides snapshots and atomic commits and aligns with the cloud stack.

## 7. Pipeline stages and contracts

### 7.1 Ingest → bronze

#### 7.1.1 Discovery
- List `s3://noaa-global-hourly-pds/{year}/` for each year in scope (paginated ListObjectsV2). With obstore, a listing prefix matches **whole path segments only** (e.g. `prefix="2024"`, not `"2024/7238"`). ETags arrive quoted (`"…"`) and are stored without the quotes. `REPORT_TYPE` values may carry trailing spaces (`"SOD  "`), so they are stripped during parsing.
- Keep keys whose station ID (file name without `.csv`, 11 characters) is in the selected station list (`config.stations.list`), or all keys when `stations: all`.
- The pilot list is a CSV with at least `station_id, region, role` (`role` ∈ `source_candidate, contrast`).

#### 7.1.2 Download
- obstore, anonymous, concurrency `ingest.max_concurrency` (default 16), retries with exponential backoff (default 5).
- Objects larger than `ingest.spool_threshold_mb` (default 100) are written to a temp file instead of memory.
- The MD5 is computed over the downloaded bytes and compared with the ETag when the ETag has no `-` (on 2026-10-15, NOAA ETags equalled the MD5 for the 3 files tested). A mismatch is treated as a failed download.
- The CSV is read with Polars, **all columns as strings** (`infer_schema_length=0`).

#### 7.1.3 Source manifest (`_state/sources.parquet`)
| Column | Type | Meaning |
|---|---|---|
| `key` | str | S3 key `YYYY/SSSSSSSSSSS.csv` |
| `station_id`, `year` | str, int | Parsed from the key |
| `size`, `etag`, `last_modified` | int, str, timestamp | From the listing |
| `md5` | str | Computed while downloading |
| `retrieved_at` | timestamp | |
| `status` | str | `ok`, `failed`, `missing_upstream` |
| `rows` | int | CSV data rows = bronze rows |
| `bronze_path` | str | |
| `run_id` | str | Run that wrote the current bronze file |
| `error` | str | Last error message, if any |

**Change detection:** a key is (re)downloaded if it is absent from the manifest, its `etag` or `size` changed, or its last status was `failed`.
**Failures:** a failed download records `status=failed` and leaves the existing bronze file untouched. A run never aborts because of one file.
**Upstream removal:** a key counts as removed only if it is absent from a **complete, successful** listing of its year prefix. It is then marked `missing_upstream`, its bronze is kept, and it is reported in the run summary. Downstream policy is decided manually. A failed or partial listing never marks anything removed.

#### 7.1.4 Bronze contents
- Every source column, unchanged, as string; plus `source_key`, `source_md5`, `ingest_run_id`.
- Each file keeps its own column set; readers use `union_by_name`. Silver parses a fixed list of known fields, so schema differences end at bronze. Unknown columns are logged once per run.
- Check: bronze row count = CSV row count; `STATION` and `DATE` non-null.

### 7.2 Bronze → silver

Unit of work: one station-year **plus the bronze rows of the same station in the final 24 h (UTC) of the previous year**, which are needed by the haze rule (§7.2.5). Output: the rows **owned** by the target year. A synoptic-slot candidate is owned by its slot's year, so a report at 23:56 on 31 December belongs to the next year's file because it competes for the 1 January 00:00 slot — **if that file's bronze exists**. When year Y+1 has no bronze (the last year ingested, or a station that stopped), Y keeps those rows; silver metadata records `next_year_present`, and Y is rebuilt when Y+1 bronze arrives (final review I2). Every other row is owned by its own UTC year. Each report is therefore stored exactly once, and each slot is selected in exactly one file. This was found on real Las Vegas data in the Plan 1 thin slice. **No rows are deleted**; everything is flagged.

**Failures:** a station-year that fails to build is logged and counted (`failed`, `failures` in the run summary); its previous silver file, if any, is left in place and the stage continues (final review I3).

Row order: input rows are **explicitly sorted** by `(time_utc, report_type, source, row_key)`, where `row_key` = SHA-1 of the row's source values (columns in sorted name order, provenance columns excluded). This makes order independent of file row order. NOAA notes that files from 2005 onward may be unsorted or incompletely merged (`NOTICE-ISD-MERGE-ISSUE.TXT`). Rows identical in **all** source columns are collapsed to one (`dup_count` records how many). Rows with the same timestamp but different values are kept as separate candidates.

#### 7.2.1 Parsed fields (`parse.py`)
| Group | ISD fields | Parsed columns |
|---|---|---|
| Control | `STATION, DATE, SOURCE, REPORT_TYPE, CALL_SIGN, QUALITY_CONTROL, LATITUDE, LONGITUDE, ELEVATION` | `station_id, time_utc, source, report_type, lat, lon, elev` |
| Wind | `WND` | `wind_dir, wind_dir_qc, wind_type, wsd, wsd_qc` (m/s) |
| Visibility | `VIS` | `vis_raw (m), vis_qc, vis_var, vis_var_qc` |
| Temperature | `TMP, DEW` | `t, t_qc, td, td_qc` (°C) |
| Pressure | `SLP` | `slp, slp_qc` (hPa) |
| Gust | `OC1`, `OD1–OD3` (types 3 = maximum instantaneous wind, 4 = maximum gust; config `parse.gust_od_types`, default `[3, 4]`) | `gust, gust_qc, gust_src`. The legacy code used type 3 only, which misses e.g. every Las Vegas gust (type 4). |
| Present weather | `MW1–MW7`, `AU1–AU9`, `AW1–AW4` | code + QC per occurrence (§7.2.4) |
| Vicinity / past / daily | `MV1–MV7`, `AY1–AY2`, `AZ1–AZ2`, `AX1–AX6`, `AT1–AT8` | code + QC (+ period for AY/AZ/AX; source and weather type for AT). AT/AX appear in daily-summary (SOD) rows. |
| Precipitation / snow | `AA1–AA4`, `AJ1` | `precip_mm, precip_period_h, precip_qc, snow_depth_cm, snow_qc` (low reliability) |
| Status | | `parse_status` (bit flags per malformed field), `dup_count` |

Missing-value sentinels (e.g. `9999`, `99999`, `999999`, `+9999`) become null. Scale factors follow the ISD format document.

#### 7.2.2 Time model
- `time_utc` from `DATE`.
- `time_lmst = time_utc + lon_used/15 h`, where `lon_used` is the report's own longitude, or the station-year median if missing. Coordinates outside ±90 / ±180 (ISD missing code `+999999` reads as 999.999) are set to null first; a station-year with no valid longitude fails with `MissingLongitudeError` (final review I5). This is **local mean** solar time; apparent solar time (equation of time, ±16 min) is not used and doesn't matter at 3-hourly resolution.
- `lmst_date` (the local day used by daily products), `lmst_hour` (fractional hour, 0–24).
- `slot_lmst_date` = LMST date of the **slot** time. A selected report counts on its slot's day, not its own, because a report up to 30 min early can sit across local midnight (final review I1).
- `summary_lmst_date` (SOD rows only) = `date(time_lmst − 12 h)`. SOD rows are stamped 23:59 local **standard** time, which can fall just after LMST midnight (e.g. 07:59Z at Las Vegas), so they are dated to the day they summarise (final review C1).

#### 7.2.3 QC (`qc.py`)
- ISD QC codes per value (ISD format document, general quality-code table): `0,1,4,5,9` and the validator letters `A,C,I,M,P,R,U` → `ok`; `2,6` → `suspect`; `3,7` → `erroneous`; any other code → `suspect`; missing value → `missing`. Stored as `<var>_flag`. The mapping is config (`qc.codes`).
- Range checks set `erroneous`, using the MIN/MAX of each field in the ISD format document: wind direction 1–360°; wsd 0–90 m/s; t −93.2 to 61.8 °C; td −98.2 to 36.8 °C, and td ≤ t + 0.5 °C; vis_raw 0–160,000 m; slp 860–1090 hPa; OC1 gust 5–110 m/s; OD speed 0–200 m/s.
- `use_<var>` = flag ∈ `qc.accept` (default `[ok]`; set `[ok, suspect]` to include suspect values).
- **RH:** Magnus form (Alduchov & Eskridge 1996): `e_s(T) = 6.1094·exp(17.625·T/(T+243.04))`, `rh = 100·e_s(td)/e_s(t)`. Null if either value is unusable; `td > t + 0.5` makes RH null and sets the td flag to erroneous.
- **Visibility:** `vis_capped = min(vis_raw, qc.vis_cap_m)` (default 10,000 m). `vis_raw`, `vis_qc` and `vis_capped` are all kept.

#### 7.2.4 Dust classification (`classify.py`)

Each present-weather field has **its own code table** (verified against the ISD format document, `noaa-isd-pds/isd-format-document.pdf`, on 2026-10-15). Only occurrences whose QC is accepted are used.

| Field | Code | Class |
|---|---|---|
| **MW** (manual, WMO 4677) | 05 | `haze` |
| | 06 | `suspended` |
| | 07 | `blowing` |
| | 08 | `whirls` |
| | 09 | `storm` |
| | 30, 31, 32 | `storm` (slight/moderate) |
| | 33, 34, 35 | `storm_severe` |
| | 98 | `storm_thunder` |
| | 04 (smoke) | `none` |
| **AW** (automated, WMO 4680) | 04 | `haze_or_suspended` (haze, smoke *or* suspended dust; ambiguous) |
| | 07 | `blowing` |
| | 27, 28, 29 | `blowing_snow_or_sand` → resolved by temperature (below) |
| | 30–35 | **`none` (fog; not dust)** |
| | 05 (smoke) | `none` |
| **AU** (METAR style; components intensity, descriptor, precipitation, obscuration, other) | other = 4 (SS) or 5 (DS) | `storm`; `storm_severe` if intensity = 3 (heavy); `storm_thunder` if descriptor = 7 (TS) |
| | other = 1 (PO) | `whirls` |
| | obscuration 5 (DU) or 6 (SA) with descriptor 5 (BL) or 4 (DR) | `blowing` |
| | obscuration 5 (DU) or 6 (SA) without BL/DR | `suspended` |
| | obscuration 7 (HZ) | `haze` |
| | any of the above with intensity = 4 (VC) | `vicinity` (not at the station) |
| **MV** (vicinity) | 03, 05 | `vicinity` (storm) |
| | 04 | `vicinity` (whirls) |
| | 07, 08 | `vicinity` (blowing) |
| **AY** (past weather, manual, WMO 4561) | 3 | sandstorm, dust storm or blowing snow → `flag_past` if resolved as sand (temperature) |
| **AZ** (past weather, automated, WMO 4531) | 2 | blowing phenomena, visibility reduced → `flag_past` if resolved as sand (temperature) |
| **AX** (daily past weather, summary of day) | 07 | blowing dust or sand, visibility ≤ ½ mile → `flag_daily_dust` |
| **AT** (daily present weather) | 07 | dust, volcanic ash, blowing dust/sand or obstruction → `flag_daily_dust` |

**Snow/sand resolution** (AW 27–29, AY 3): sand/dust if `t ≥ classify.sand_min_t_c` (default 3 °C) and no snow evidence in the same report (AU precipitation 03 or MW 70–79); snow if `t ≤ 0 °C`; otherwise `ambiguous`. Default thresholds are reviewed with diagnostics during the pilot (§15, V1).

**Combining occurrences** within one report, by precedence: `storm_thunder > storm_severe > storm > whirls > blowing > suspended > haze_or_suspended > haze > none`. `vicinity`, `past_*` and `ambiguous` are recorded as evidence but **do not count as station dust by default** (`classify.count_vicinity: false`, `classify.count_past: false`). `unknown` = no usable present-weather field in the report.

**Silver classification columns:**
- `pw_evidence`: list of `{field, code, class}`.
- `dust_class_rule`: the combined class.
- `dust_rule_id`: an identifier of the deciding table row.
- `has_pw`: any usable present-weather field.
- Flags derived from `dust_class_rule`: `flag_du` (suspended, blowing, whirls, storm*), `flag_susp` (suspended), `flag_bd` (blowing, whirls; v1-compatible), `flag_ds` (storm*).
- `flag_vicinity`, `flag_past`.

#### 7.2.5 Haze rule
A report whose class is `haze` or `haze_or_suspended` becomes `dust_haze` if the **same station** has any report with `flag_du = true` (before RH screening, any report type) with `time_utc` in **(t − 24 h, t]**. The window length is `classify.haze_window_h` (default 24; 0 disables). Implemented as a time-based rolling window over the sorted rows, including the previous-year look-back. Output: `flag_duhz` (`flag_du` or `dust_haze`).

#### 7.2.6 RH screen
`rh_screened = true` if `rh > qc.rh_screen_pct` (default 90) **and** the class is in `qc.rh_screen_classes` (default `[suspended, haze, haze_or_suspended, dust_haze]`). Blowing dust, whirls and storms are never screened. The screen **does not alter** `dust_class_rule`.
`dust_class_final` = `dust_class_rule`, except that RH-screened reports become `screened`. Products use `dust_class_final` unless configured otherwise.

#### 7.2.7 Synoptic sampling (`sampling.py`)
- Slots: 00, 03, …, 21 UTC each day. Candidates: reports with `|time_utc − slot| ≤ sampling.window_min` (default 30), excluding `sampling.exclude_types` (default `[SOD, SOM]`).
- Each candidate gets `slot_time`, `selection_rank` and `selection_reason`. Rank order: (1) report-type priority, `sampling.priority` (default `[FM-12, SY-MT, SY-SA, SY-AE, SY-AU, FM-15, SAO, AUTO, FM-16]`, unlisted types after); (2) `has_pw` (true first, which favours reports *with* present-weather information, never dust-positive ones); (3) `|Δt|`; (4) `time_utc`; (5) `source`; (6) `row_key`.
- `is_synoptic_selected` = rank 1. The result is independent of input row order.
- `sampling.synoptic_dust_source`: `selected` (default; dust from the selected report only) or `any_in_window`.

**Silver contract** (`schemas.py`): typed columns as above; at most one selected report per station per slot; flags in their allowed sets; time columns non-null; `lmst_date` within ±1 day of the UTC date.

### 7.3 Silver → gold

Gold is rebuilt **in full** for the pilot into `gold/versions/<run_id>/`, validated, then activated via `CURRENT`. Gold refuses to build if silver files have mixed `method_version` or `config_hash` values (`MixedSilverVersionsError`), or if that single version differs from the running code's `METHOD_VERSION` and silver config hash (`StaleSilverError`, final review I4). Each gold version records its silver lineage in `silver_version.json`. "Valid synoptic slot" = a selected report with `has_pw` true, i.e. carrying at least one usable **present-weather** code (MW/AU/AW). **A missing weather field always means "not observed", never "no significant weather"** (user decision, 2026-10-17): gaps can come from instrument outages, human error or protocol changes, even at stations that report weather occasionally. Past-weather and daily-summary fields add dust evidence but do not make a slot valid. "Valid day" = an LMST day with ≥ `products.min_slots_per_day` valid synoptic slots (default 4).

#### 7.3.1 `synoptic3h`
The selected reports (one per station per slot), with UTC and LMST time, all parsed values, flags and classes.

#### 7.3.2 `daily` (per station × `lmst_date`)
| Column | Definition |
|---|---|
| `n_reports` | Reports in the day, excluding `sampling.exclude_types` |
| `n_slots`, `n_slots_pw` | Selected synoptic slots whose **slot** falls on this LMST day (`slot_lmst_date`); of those, slots with `has_pw` |
| `valid_day` | `n_slots_pw ≥ products.min_slots_per_day` |
| `dusty_day_synoptic` | Any selected slot with `dust_class_final` ∈ {suspended, blowing, whirls, storm*, dust_haze} (**primary**) |
| `dusty_day_any` | Same test over all non-excluded reports |
| `past_weather_dust`, `daily_summary_dust` | Any non-excluded report with `flag_past` (AY 3 / AZ 2, resolved as sand); any report, SOD included, with `flag_daily_dust` (AT 07 / AX 07) |
| `dusty_day_any_source` | `dusty_day_any` OR `past_weather_dust` OR `daily_summary_dust`: the most sensitive dusty-day flag. SOD rows are dated by `summary_lmst_date`. A station-day exists only if it has a non-excluded report or a selected slot; a day known only from a daily summary has no row. |
| `n_dust_slots_<class>` | Count of selected slots per class |
| `dust_first_lmst_hour` | Earliest `lmst_hour` of a dusty selected slot |
| `wsd_mean`, `wsd_max` | Scalar mean and maximum of usable `wsd` over selected slots |
| `gust_max` | Maximum usable gust over **all** reports in the day (gusts come mainly from METAR) |
| `vis_min`, `vis_hmean`, `n_vis_zero` | Minimum and harmonic mean of `vis_capped` over usable selected values > 0; zeros are counted, not included |
| `t_mean, t_min, t_max, td_mean, rh_mean, slp_mean` | Over usable values at selected slots |
| `dup6_mean` | Mean over selected slots with usable `wsd` of DUP(U) = U³(1 + 6/U)(1 − 36/U²) for U > 6 m/s, else 0 (m³ s⁻³; Marsham et al. 2011 form) |
| `d_t_mean`, `d_slp_mean` | Difference from the previous `lmst_date`'s mean, only if **both** days are valid; else null |
| `precip_mm`, `snow_depth_cm` | Sum and maximum; marked low reliability; excluded from trends |

#### 7.3.3 `monthly`, `annual`
`valid_days`, `dusty_days` (sum of `dusty_day_synoptic` over valid days), `dusty_day_fraction = dusty_days / valid_days`, per-class dusty-day counts, means of daily means over valid days, `coverage = valid_days / days_in_period`.

#### 7.3.4 `station_year_coverage` (per station × calendar year in LMST)
`days_any_report`, `days_with_pw`, `days_k_slots_pw` for k = 1…8 (days with ≥ k valid synoptic slots), `slot_days_<HH>` (days with a valid slot at each UTC hour), `report_type_counts` (map), `pw_fill_mw`, `pw_fill_au`, `pw_fill_aw` (share of reports with each field present), `qualifying` = `days_k_slots_pw[products.qualifying_min_slots] ≥ products.qualifying_min_days` (defaults 4 and 300). The coverage statistics are always stored, so the qualifying-year definition can be changed and re-applied without reprocessing.

#### 7.3.5 `stations` (GeoParquet catalog)
| Column | Definition |
|---|---|
| `station_id, name, country, state, icao` | From `isd-history.csv` |
| `lat, lon, elev`, `geometry` | Most recent location; point geometry (EPSG:4326) |
| `location_history` | List of `{lat, lon, elev, valid_from, valid_to}`. A new entry starts when the silver location changes by > 0.01° or > 10 m elevation. |
| `first_year, last_year, n_years, n_qualifying_years` | |
| `aridity_index`, `aridity_flag`, `aridity_pixel_lat/lon`, `aridity_dist_km` | §7.3.6 |
| `dusty_years` | Qualifying years with ≥ 1 dusty day |
| `is_dusty` | `dusty_years ≥ stations.dusty_min_years` (default 5) → in the published set |
| `baseline_period` | `stations.baseline` (default 1991–2020) |
| `baseline_qualifying_years`, `baseline_status` | `none` (0), `partial` (1 to 23), `sufficient` (≥ 24 of 30, i.e. 80%); thresholds in config |
| `baseline_median_dusty_days` | Median annual `dusty_days` over baseline qualifying years; null if fewer than `stations.source_min_baseline_years` (default 10) |
| `is_source` | `aridity_flag = ok` **and** `aridity_index < stations.source_max_ai` (0.5) **and** `baseline_median_dusty_days > stations.source_min_dusty_days` (14) |
| `code6_index`, `code6_status`, `code6_n` | §7.3.8 |
| `uth_*` | Source stations only (§7.3.7) |
| `role, region` | From the pilot list (pilot only) |

#### 7.3.6 Aridity extraction
- Input: CGIAR Global Aridity Index v3, annual (`ai_v3_yr.tif`; stored ×10,000, so values are divided by 10,000). It's already available in `legacy/station_history/` and copied to `inputs/aridity/`. The data card records its version, DOI, licence and reference period (§15, V2).
- Value at the pixel containing the station's current location. If that pixel is no-data or ≤ 0, search the nearest valid pixel within `stations.aridity_search_px` (default 3). `aridity_flag`: `ok`, `nearest_used`, `invalid_zero`, `missing`.
- **Zero values are invalid**, not hyperarid: the legacy list gives Antarctic stations 0.0, which would otherwise pass the source test.
- The aridity index is climatological. Per-decade aridity and drift come in sub-project D.

#### 7.3.7 Uth (source stations only)
Input: synoptic-selected reports with usable `wsd` and a known `dust_class_final`. Computed for all years, the baseline, and each decade.

Two outcome definitions: `all_dust` (`flag_du`) and `raised` (blowing, whirls, storm*). Two methods:
- **`histogram_ratio`** (legacy method): 1 m/s bins from 0 to 20 m/s; ratio = dust/all × 100 per bin; Uth = interpolated wind where the ratio first reaches q ∈ {10, 20, 25, 50}%. This reproduces `legacy/master_DCDR.py::calculate_uth` exactly, for comparison.
- **`logistic_background`** (new): maximum-likelihood fit of P(dust | U) = b + (1 − b)·σ((U − U₅₀)/s) on binary outcomes. Reported: `b` (wind-independent background), `U₅₀`, `s`, onset wind `U_q` where the excess above background reaches q = 0.1, convergence status, and a bootstrap 90% interval for U₅₀ (200 resamples).
- Minimum data: `n ≥ 300` and `n_dust ≥ 30` per fit, else `status = insufficient_data`. **No method silently substitutes for another.** All fits and diagnostics go in `gold/uth`, with diagnostic plots in the pilot report.

#### 7.3.8 Code-6 diagnostic (all published stations)
Groups over selected slots with usable wind: G6 = `suspended` from MW 06 or AU DU without BL/DR; G_raised = blowing, whirls, storm*; G_none = `none`.
`code6_index = (median(G6) − median(G_none)) / (median(G_raised) − median(G_none))`, computed only if every group has ≥ 30 reports and median(G_raised) − median(G_none) ≥ 1 m/s; otherwise null with `code6_status`.
Interpretation (descriptive): ≈ 0 means code-6 winds look like non-dust conditions; ≈ 1 means they look like raised dust.

#### 7.3.9 `climatology`
Per station × `window` ∈ {`baseline`, `all`} × `period_type` ∈ {`month` (1–12), `season` (DJF, MAM, JJA, SON; DJF uses December of the previous year), `year`, `decade` (1940s…)}: `dusty_days_mean`, `dusty_day_fraction_mean`, `wsd_mean`, `vis_hmean_mean`, `n_years_used`, `status`. Only qualifying years are used. A decade needs ≥ 5 qualifying years, else its value is null with `status = insufficient`.

#### 7.3.10 `diurnal`
Per station × `window` × 3-hour LMST bin (`floor(lmst_hour/3)·3`): fraction of valid selected slots that are dusty; `n` slots.

#### 7.3.11 `trends`
Per station × `window` ∈ {`all`, `baseline`}: annual `dusty_days` over qualifying years only; requires ≥ 10 qualifying years. **Modified Mann–Kendall** test (Hamed & Rao 1998, `pymannkendall.hamed_rao_modification_test`) + Sen's slope. Outputs: `slope_days_per_decade`, `p_value`, `n_years`, `first_year`, `last_year`, `method`, `status`.

**Gold checks:** schemas; `0 ≤ dusty_day_fraction ≤ 1`; `dusty_days ≤ valid_days`; every published station has ≥ 1 daily row; the catalog matches the station list; class counts reconcile between daily and monthly.

## 8. Station sets

### 8.1 Published set
`is_dusty = true` (D9). All published stations appear on the map, in climatology and in trends.

### 8.2 Source set
`is_source = true` (D10). Used only by sub-project E. Uth is computed only for these stations.

### 8.3 Pilot selection (`scripts/select_pilot_stations.py`)
1. Inputs: `isd-history.csv` (refreshed from `noaa-isd-pds`), the S3 listing (years available per station), duISD v1 (Zenodo 10.5281/zenodo.4107346, used **only to rank** candidates), and the aridity raster.
2. Region boxes (`config/stations/regions.yaml`, first match wins; lat °N, lon °E):

   | Region | Lat | Lon | Target n |
   |---|---|---|---|
   | North Africa (Sahara, Sahel) | 12 to 37 | −18 to 35 | 15 |
   | Middle East | 12 to 40 | 35 to 63 | 15 |
   | Central Asia | 35 to 50 | 50 to 80 | 12 |
   | East Asia | 34 to 50 | 75 to 120 | 15 |
   | Southern Africa | −35 to −15 | 10 to 30 | 8 |
   | North America | 25 to 42 | −125 to −100 | 10 |
   | South America | −55 to −15 | −75 to −55 | 8 |
   | Australia | −40 to −15 | 113 to 154 | 7 |
   | Contrast (transport-affected, METAR-heavy) | Canary Islands/Cabo Verde, Korea/Japan, Caribbean, US METAR stations incl. Las Vegas `72386023169` | — | 10 |

3. Candidates: inside the box, ≥ 30 years with data since 1973, valid aridity.
4. Rank by duISD v1 mean dust frequency. If its files can't be used for ranking, fall back to counting dust codes in a 3-year sample scan (§15, V4).
5. Greedy selection with ≥ 200 km minimum spacing within a region.
6. Output `config/stations/pilot_100.csv` (`station_id, name, country, lat, lon, region, role, rank_metric, n_years, aridity_index`) plus a map. The user reviews and edits the list before ingest.

## 9. Publishing (B)

- `dcdr publish --version X.Y.Z[-pilot]`:
  1. Build `releases/<version>/` from the active gold version: products, data card (`DATA_CARD.md`: sources, licences, methods, definitions, known limitations, citation), and `release_manifest.json` (version, run_id, git commit, config hash, source snapshot = manifest hash, per-file SHA-256, row counts, validation results).
  2. Run all checks. Any failure → nothing is uploaded.
  3. Upload to the Hugging Face dataset repo named by config key `publish.hf_repo` (set when the Hugging Face account is created in M4), tagged with the version. **Verify** remote file presence and checksums.
  4. Only after verification, update the `latest` pointer (HF tag/branch and `latest.json`). A failed release leaves the previous `latest` unchanged; the release is marked `built_local` until verified, then `published`.
  - Zenodo deposits are made only for full (non-pilot) releases.
- Licence: CC BY 4.0 for data (NOAA NODD terms allow redistribution; the data card states the data is derived from, not identical to, NOAA ISD). MIT for code. The public dataset name is decided before the first public release.

## 10. Website (C)

### 10.1 Web export (`dcdr export web --version …`)
| File | Content | Loaded |
|---|---|---|
| `stations.parquet` | Published stations: id, name, country, region, lat, lon, flags, aridity, coverage summary, baseline status | On page load |
| `climatology.parquet` | §7.3.9 for published stations | On page load |
| `trends.parquet`, `diurnal.parquet` | §7.3.10–11 | On page load / on demand |
| `station_year_month.parquet` | station × year × month: valid_days, dusty_days, per-class counts | On demand (custom period filters) |
| `series/<station_id>/daily.parquet`, `monthly.parquet`, `annual.parquet` | **One file per station** | When a station is selected |

Station-level partial reads are guaranteed by **one file per station**, not by sort order. Initial page payload target: < 5 MB.

### 10.2 Site (`web/`, Observable Framework)
- **Map:** MapLibre GL JS with a free basemap (OpenFreeMap). Stations are coloured by the selected statistic (mean dusty days; dusty-day fraction; trend) and period (month, season, year, decade; baseline or all years). Filters: region, country, source/dusty, record length, baseline status, custom year range (recomputed in DuckDB-WASM from `station_year_month`). **Missing or insufficient values** are shown as grey markers with a legend entry and a tooltip giving the reason.
- **Station page:** daily, monthly and annual time series (Observable Plot), seasonal cycle, diurnal cycle, coverage chart, location history, metadata.
- **About:** methods, definitions, data card, citation.
- The site reads only published web-export files; it never imports pipeline code.
- **Pilot hosting:** web-export files are bundled with the static site on GitHub Pages (same origin; no CORS). At full scale, the data base URL moves to Cloudflare R2 with CORS configured, through one config value.
- `deploy-site.yml`: builds and deploys when `web/` changes or a release is published. It never runs the scientific pipeline.

## 11. Configuration and provenance

- `src/dcdr/default.yaml` sections: `paths`, `ingest`, `parse`, `qc`, `classify`, `sampling`, `products`, `stations`, `publish`, `web`. Every threshold named in this spec is a config key with the stated default.
- `config/profiles/duisd_v1.yaml` approximates legacy behaviour for comparisons: MW fields only; UTC days; first report in 3-hour bins aligned to midnight; RH > 90 screens all dust classes; no QC-code filtering; no visibility cap; haze window 24 h.
- Each run writes `_state/runs/<run_id>/config.resolved.yaml` (all values after merging) and records `config_hash` (SHA-256 of the canonical resolved YAML), the git commit, the dirty flag, the command and per-stage counts in `_state/runs.parquet`.
- `method_version`: a constant in `dcdr.silver`, bumped whenever parse, QC, classify or sampling logic changes. The silver `config_hash` covers only the sections that affect silver.

## 12. Rebuild policy

| Change | Recomputed |
|---|---|
| Source object new or changed (ETag/size) | That bronze station-year → its silver, **and the next year's silver** (haze look-back) → all gold |
| Parse/QC/classify/sampling code (`method_version`) or silver config | All silver in scope → all gold |
| Gold SQL or product config | All gold (gold is rebuilt in full; it's small) |
| Station list, `isd-history.csv` or aridity raster | Catalog and all gold |
| Web export or site code | Web export (if its schema or content changed) and/or site only |

Gold always rebuilds in full in the pilot, deliberately conservative. Incremental gold is a future optimisation.

## 13. Testing

- **Unit:**
  - Packed-field parsers, using real ISD strings including malformed ones and every missing-value sentinel.
  - QC flag mapping and range checks; RH including td > t.
  - **Each code table row in §7.2.4** (one test per row), including AW 30–35 → `none`; snow/sand resolution; precedence.
  - Haze window boundaries (exactly t − 24 h excluded, t included).
  - Sampling rank and tie-breaks; LMST conversion near the dateline and at New Year.
  - DUP6 at U = 6 (→ 0) and U = 10; harmonic mean with zeros; `d_t_mean` across a missing day.
  - The legacy `histogram_ratio` reproduction.
- **Integration fixtures** (real, trimmed station-year files under `tests/fixtures/`), together covering:
  - different column sets;
  - malformed fields;
  - competing reports in one slot;
  - unsorted rows and exact duplicates;
  - **a haze event crossing 31 December**;
  - a station location change;
  - a source correction (changed ETag) that changes outputs;
  - a METAR-heavy US station (AU/AW only);
  - a SYNOP-only station.
- **Invariants:**
  - shuffled input gives identical output;
  - re-running unchanged inputs gives identical files and checksums;
  - a changed source file rebuilds only that station-year, the next year and gold;
  - an **injected failure during a write** leaves the previous file valid;
  - a failed release leaves `latest` unchanged.
- **Snapshot tests** (pilot): saved summary statistics per station (class counts, dusty days per year, qualifying years). Counts must match **exactly**; floating-point statistics within documented tolerances (relative 1e-9 for deterministic sums, 1e-3 for model fits). Changes need an explicit snapshot update in a reviewed commit.
- **CI** (`ci.yml`): ruff, mypy, pytest (unit + integration) on every push.

## 14. Operations

- **CLI:**
  - `dcdr ingest`
  - `dcdr build silver`
  - `dcdr build gold`
  - `dcdr validate`
  - `dcdr export web`
  - `dcdr publish`
  - `dcdr run` (all stages; incremental by default; `--force` and `--years`/`--stations` scoping)
  - `dcdr status` (manifest and run summary)
- **Logging:** console + `_state/runs/<run_id>/run.log`, with a run summary (files, rows, failures, durations).
- **Backups:** `rsync --link-dest` dated snapshots of `DCDR_DATA` onto the Areca RAID after each release and weekly during active work; keep the last 8 snapshots. Releases are kept permanently.
- **Monthly update** (`monthly-update.yml`, built at the end of the pilot): ingest the current year for published stations → silver/gold for that year → refresh the current-year time series in the web export and release a patch version. Baseline climatology doesn't change monthly; trends are updated once a year.

## 15. Verification tasks (completed during implementation, each with an acceptance criterion)

| ID | Task | Accepted when |
|---|---|---|
| V1 | Snow/sand temperature thresholds for AW 27–29 and AY 3 | Pilot diagnostics (counts by temperature and month) reviewed by the user; thresholds confirmed or changed in config |
| V2 | CGIAR Aridity Index v3 version, DOI, licence and reference period | Recorded in the data card from the official dataset page |
| V3 | Observable Framework maintenance status | Latest release within the last 6 months; otherwise switch to Vite + TypeScript with the same libraries |
| V4 | duISD v1 files usable for ranking pilot candidates | The selection script produces ranked candidates per region; else the documented fallback is used |
| V5 | Pilot station list | User approves `pilot_100.csv` |
| V6 | Website performance | Initial payload < 5 MB; a station series loads in < 2 s on the user's connection |
| V7 | ISD QC code meanings for every parsed field | Unit tests reference the format-document tables for each field |

## 16. Milestones (detailed in the implementation plan)

1. **M0** Repo scaffolding: uv project, CI, config models, state tables.
2. **M1** Thin slice: **one station-year** → bronze → silver → `daily` → one chart in a notebook.
3. **M2** Pilot selection (V4, V5) + bronze/silver for 100 stations × all years.
4. **M3** Gold: products, coverage, catalog (aridity, flags), climatology, diurnal, trends, Uth, code-6 diagnostic.
5. **M4** Release (B) to Hugging Face.
6. **M5** Website (C) on GitHub Pages.
7. **M6** Monthly update workflow, backups, README and data card polish.

## 17. Risks

| Risk | Mitigation |
|---|---|
| Code-table mapping errors bias dust frequency | One unit test per table row; per-field fill diagnostics; review of pilot class counts by field |
| Report-type changes over decades (SYNOP → METAR) create artificial trends | Synoptic sampling, coverage table, per-field fill shares in the catalog; trends use qualifying years only |
| Small-file overhead at full scale | Table-format upgrade path (§6.2) |
| exFAT has no journal | Atomic single-file writes, pointer for gold, always eject safely, RAID snapshots |
| Free-tier changes (GitHub, Hugging Face) | Static files are portable; R2 or another static host are drop-in alternatives |

---

## Appendix A: Decisions recorded for later sub-projects

**D (environmental drivers):** gridded ERA5 (ARCO-ERA5 on GCS), MODIS/NDVI, soil moisture, per-decade aridity and trend (ERA5-Land or TerraClimate P/PET); join to stations; covariation and trend views on the website. Gridded precipitation replaces ISD precipitation for analysis.

**E (dust forecasting):**
- **Scope:** **source stations only** (D10).
- **Training inputs:** **GEFSv12 reforecasts** (`noaa-gefs-retrospective`, 2000–2019, open) for consistency between training and live use.
- **Live inputs:** operational GEFS (`noaa-gefs`). ECMWF open data (CC BY 4.0) is a second source; it must be archived by us, because ECMWF keeps it only 2–3 days. ERA5-input models serve only as a "perfect forecast" baseline.
- **Labels:** three variants, `L_all` (all dust codes), `L_wind` (dust + concurrent wind ≥ station threshold within ±1 slot) and `L_raised` (blowing/whirls/storm), compared rather than chosen up front.
- **Features:** include the LMST hour of day (diurnal cycle).
- **Models:** baselines (climatology, persistence, DUP threshold), then LightGBM (+ Optuna, SHAP).
- **Model-structure experiment:** per-station vs regional vs global-with-station-features vs global + per-station calibration, evaluated per station, with skill plotted against the number of events.
- **Metrics:** PR-AUC, Brier skill score, reliability, CSI. Validation on held-out years and held-out regions.
- **Operations:** MLflow tracking and registry; daily inference in GitHub Actions; verification against observed ISD; Dagster introduced here.

## Appendix B: Glossary

| Term | Meaning |
|---|---|
| **Bronze / silver / gold** | Raw-faithful / cleaned-and-classified / analysis-ready data layers |
| **Synoptic slot** | 00, 03, …, 21 UTC, with one selected report per station |
| **LMST** | Local mean solar time = UTC + longitude/15 h |
| **Valid day** | LMST day with ≥ 4 valid synoptic slots (default) |
| **Qualifying year** | Station-year meeting the coverage rule (default ≥ 300 days with ≥ 4 valid slots) |
| **Dusty day** | Valid day with dust in at least one selected slot |
| **Dusty station** | ≥ 1 dusty day in ≥ 5 qualifying years (published set) |
| **Source station** | Aridity index < 0.5 and baseline median > 14 dusty days a year (ML set) |
| **DUP6** | Dust uplift potential with a 6 m/s threshold |
| **Uth** | Threshold wind speed for dust emission (source stations only) |
