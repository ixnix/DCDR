# DCDR work log

A dated record of what was done, what was found and what was decided, newest first.
"User decision" = decided by Xin Xi; "Ruling" = a choice made by Claude during implementation,
open to review. Details of the current method: [rules.md](rules.md); of the software:
[code_design.md](code_design.md). Commits are referenced by their short hash.

## Open items

- [ ] Review `stations_review.csv` (conflicts and unverified, most dusty first) and write
      `config/stations/position_overrides.csv` before the whole-ISD gold.
- [ ] Re-run notebook 07 (Australia) and look at the first `three_hourly` table (e.g. one outbreak
      at Phoenix and one at Niamey).
- [ ] Station catalog with aridity index and the dust-source flag (criteria from the statistics,
      window 1991-2020), then wind histograms and Uth for source stations (decisions 2026-10-31).
- [ ] Papunya is dusty on 43 % of its valid days in the 1.10.1 gold (1,489 of 3,449; other
      Australian stations 8-11 %): real (central desert) or a station habit?
- [ ] Check Niamey (Diori Hamani) before ~1975 (almost no dust, then >= 150 days a year) and the
      fall at Ciudad Constitucion (2000-2023): reporting changes or real?
- [ ] Distribution (B) and website (C), designed together on the pilot dataset (user decision
      2026-11-02); needs a basic station table (name, coordinates, elevation, country, published
      set). Licence: open, non-commercial, compatible with the ISD terms.
- [ ] *Postponed* (user decision 2026-11-02): update functionality. NOAA's S3 ISD files end on
      2025-08-27; ISD's future is outside our control, so releases cover the existing record.
- [ ] FMH-1: replace the 2005 mirror copy (everyspec.com) with an ICAMS/NOAA copy if one appears.
- [ ] Coverage criteria for the published products (valid days per year and decade), from the
      station catalog.
- [ ] Investigate stations with no dust in 2020-2024 despite high duISD ranks (Ashchysai, Shakawe,
      Good Hope): change of observing practice or real decline?
- [ ] Later gold products: climatology, diurnal cycle, trends, code-06 diagnostic.
- [ ] Publish documentation (e.g. MkDocs) when stable.
- [ ] `config/stations/all_isd.csv` (untracked): commit or ignore.
- [ ] Whole-ISD gold under 2.1.0 (silver done and verified 2026-11-05; gold failed twice for
      memory, then on duplicate stations; fixed 2026-11-05: re-run, ~1.5-2 h), then the
      `stations_review.csv` review.
      Before a published release: a full rebuild (`--force`).
- [ ] Same-station reports at the same time in two files (9,303 reports, 4 station-years,
      2026-11-05): accept, or collapse across files in gold (a method change)? And whether to
      join station records across ID changes (40 of the 46 mixed files are renumberings).
- [ ] Gold is not bit-for-bit reproducible: DuckDB's parallel sums give 9 `daily` means
      (`wsd_mean, vis_hmean, t_mean, td_mean, rh_mean, slp_mean, dup6_mean, d_t_mean,
      d_slp_mean`) that differ between two builds of the same silver in the last bits (at most
      2e-12; 4-21 of 535,705 rows per column). Decide: ordered sums, rounding, or accept and
      document.

---

## 2026-11-05

- **Whole-ISD gold stopped on 15 duplicate stations; linked stations now share a batch** (this
  commit). Run `20261105T191607Z-59cfde` (commit 1998171) checked 704,777 silver footers in
  30 min (one schema; 3,899,647,818 reports, less than the 9 billion extrapolated from 300
  stations), built the station table in 400 batches, then stopped:
  `GoldCheckError: stations (all batches): {'unique_station': 15}`. Nothing was published; the
  previous gold stays current.
  - Cause: batches group silver files by file name, but `station_id` is each report's `STATION`
    field. A full scan of `station_id` against the file name (all 704,777 files) found 46
    station-years whose file also holds reports of another station ID, 45 of which have files of
    their own: mostly stations renumbered within a year (e.g. `72222013899` in 1985: 5,512
    reports of `72222113899`; `99999903919` in 1955: all 8,759 reports of `72458613922`). Where
    such a pair fell into two batches, the station appeared twice. The one-batch build merged
    them silently, as the rule says (a report belongs to its `STATION`).
  - Fix: the footer check also reads the IDs in each file from the min/max statistics of
    `station_id` (no extra reads); linked stations form one unit that is batched together. The
    46 footer links equal the 46 pairs of the full scan. The station table is sorted by station
    ID when combined (linked units put batches slightly out of ID order). The guard "station in
    gold with no silver file named after it" became "found in no silver file" (name or footer).
    No result changes: METHOD_VERSION stays 2.1.0.
  - Tests: a file holding a 4th day of another station; batched gold equals one-batch gold
    (same rows), the station appears once and has all 4 days; links found from the footer;
    linked stations share a batch in both passes. 406 tests pass.
  - *Ruling:* keep the rule that a report belongs to its `STATION` field (not to the file it
    came in); a rule for the 46 files can be added later if wanted (they can be listed again by scanning `station_id` against the file name).
  - What the 46 are (finding, same day): 40 are one station whose ID changed within the year
    (same place within 2 km, the reports switch in one block; e.g. US airports renumbered on
    1985-06-01 and 1990-03-01, Canadian 72xxx -> 71xxx on 1977-07-01); 5 are different stations
    (East Liverpool -> Findlay 258 km, Coalville -> Fairfield 84 km, Attu -> Shemya 64 km,
    Beardmore Camp -> Little Jeana 232 km, Little Jeana -> Fletcher Ice Island T-3, no position),
    whose foreign reports carry the other station's own position; 1 whole file
    (`99999903919`, 1955) holds only reports of Salina Muni `72458613922` (1.9 km away). The
    `STATION` label was right in every case, so the rule stands.
  - Same station, same time, in two files: 9,303 reports in 4 station-years (Harrisburg 1985,
    both files hold the whole year: 5,136 + 3,619; Laredo 1965: 178; Lackland 1973: 370). Same
    time, temperature and wind (9,264), but other ISD source flag (3 vs 5) and some groups differ
    (MW, gust, SLP), so `row_key` differs and neither copy is collapsed. In gold, a bin's state
    takes one report, dust flags combine both, report counts double. Atlantic City Jan-Feb 1974
    reports under two IDs (`72407093730`, `99999993730`), 218 at the same time.

- **Gold ordered by place, in two passes** (this commit; user decision: organise the batches by
  closeness on lon/lat grid bins, with a future product that aggregates stations in uniform
  lon/lat bins in mind). The batches of the previous commit followed station IDs.
  - Pass 1 builds the station table for every station (in station-ID order) and the review
    list; pass 2 builds the other products for the stations in gold only, ordered by place:
    10-degree cells (`products.gold_grid_deg`, must divide 180) along a Hilbert curve, then each
    station's place in its cell (a finer Hilbert curve, 256 steps per side). Batches hold whole
    cells; a cell is split only when it alone exceeds a batch. Every cell then lies in
    consecutive files, so a product on a grid whose step divides 10 degrees can be built from
    consecutive files. The station table stays one file sorted by station ID (the lookup).
  - Measured on 300 real stations (scratchpad): only 161 stations, 55 % of the reports, are in
    gold; the station table alone takes 7 s of a 78-111 s build. Every station in gold has a
    trusted latitude and longitude.
  - *Ruling:* batches of whole cells rather than re-sorting rows by place: re-sorting each
    product cost +6 s per 10-million-report batch (+35 %; +20 % with the SQL's own sort removed);
    whole cells give the same guarantee (each cell in consecutive files) at no cost. Rows inside
    a batch keep the SQL's order (station ID, time).
  - Guard: pass 2 finds a station's files by name (`silver/year=YYYY/<station_id>.parquet`); a
    station in gold without one stops gold (it would silently drop out). Found because several
    gold tests named their silver files `A`, `B` or `S`; the test helpers now name files after
    the station, as real silver does.
  - Checks: tests first (Hilbert curve steps to a neighbouring cell; cells kept together; a
    fitting cell never split; a larger cell split by station; files follow the order; pass 2
    reads only stations in gold; the file-name guard; the grid setting); 402 tests, ruff, mypy.
    Real data: 60 stations batched vs one batch: same rows (floats within 1e-12); 300 stations
    with 29 batches and 8 MB files: all 99 cells in consecutive files, no two interleaved.
  - Timing: identical runs differ by +-30 % on this Mac (75, 103 and 120 s for the same
    300-station build), so batch sizes cannot be ranked by time; peak memory 1.7-2.6 GB for
    1-10 million reports. Kept 10 million. Whole ISD: ~2-3 h for the batches plus ~40 min of
    footer checks (estimate with that spread).
  - Also: a 21 GB `.tmp/` in the repository was DuckDB's spill from the stopped 600-station
    test (an in-memory connection spills to `./.tmp` by default); deleted, and `.tmp/` ignored.

- **Gold in batches of stations, combined into ~256 MB files** (this commit). The `union_by_name`
  fix below solved only the first wall: tested on real data, the gold build itself does not fit
  either. 60 random whole stations at once: 12 s, peak 2.1 GB; 600 at once: still on
  `three_hourly` after 10 min with swap at 14.8 of 16 GB (stopped). DuckDB's plan shows why: it
  reads only the 27 columns gold needs (so trimming columns would not help), but it materialises
  the `rep` step of `three_hourly` (read by both the ranking and the grouping), holding every
  report of the build in memory. The whole ISD has never been built as gold before: the largest
  gold so far was 98 stations (3 November).
  - Change (user decisions 2026-11-05: batches; gold organised by product, not station-year;
    keep 2 versions): stations are split into batches of whole stations of at most
    `products.gold_batch_reports` silver reports; each batch writes one part per product into
    `_batches/` and is checked there (all checks are per station); the station table is merged
    into one file and the review list is written at the end; then consecutive parts are
    combined into files of ~`products.gold_file_mb` with a row-count check. DuckDB gets a memory
    limit (`products.gold_memory_limit`) and a spill folder `gold/_spill_<run_id>/`.
    `gold_keep_versions` 3 -> 2 (a whole-ISD version is ~45 GB). Gold results unchanged, no
    `METHOD_VERSION` bump; the lineage adds `gold_batches` and `gold_batch_reports`.
  - Batch size (*ruling*, measured on real silver in the scratchpad, 150 random stations,
    ~45 million reports): batches of 2, 5 and 10 million reports peak at 1.7, 2.2 and 2.6 GB
    and take 47, 42 and 45 s (reading dominates, not the batch size). Chosen: 10 million, with
    a 4 GB limit for DuckDB. 300 stations (~85 million reports) with the defaults: 9 batches,
    78 s, peak 2.1 GB. Whole ISD (~100x): ~900 batches, ~2-2.5 h after the ~40-min silver check.
    The sample comes from stations active in 2015, so it is likely denser than the average
    ISD station: an upper estimate.
  - File size (*ruling*): the batch is the unit of memory, the file the unit of storage.
    Without combining, ~900 parts per product, the statistics' under 0.5 MB each. 256 MB files:
    `three_hourly` ~37 GB in ~145 files, `daily` ~8 GB in ~32, the statistics and `stations` one
    file each (estimates from the 300 stations: 362 MB, 80 MB, 1.7 MB monthly). DuckDB's default
    row groups (~122,000 rows) kept: a station touches 1-3 of the 108 row groups of a
    `three_hourly` file, and reading one station takes milliseconds.
  - Checks: tests first (batching, file grouping, batched = one batch, an empty batch, the
    combining check catching a lost row, memory limit and spill folder, lineage); 391 tests,
    ruff, mypy. Real data: 60 random stations as one batch and one file against 19 batches and
    8 MB files (9 `three_hourly` files): `stations, three_hourly, yearly, decadal` and the review
    list identical; `daily` and `monthly` the same rows and order, floats within 1e-12 (the
    run-to-run noise, open item).
  - Correction: notebooks 04, 05 and 07 and a code_design example read `part-000.parquet` by
    name, not `part-*.parquet` as I had said; changed to `part-*.parquet` (the notebooks are
    local, git-ignored). The previous entry's "fixed" covered only the first wall.

- **Whole-ISD gold killed twice for memory; fixed** (this commit). Two `dcdr aggregate` runs
  (1901-2025, all ISD stations, 704,777 silver files) died without a traceback, at 11:31 and
  12:33, each about 40 s to 8 min after the ~40-min silver version check, before the gold
  folder was created: in the step that opens all silver files as one DuckDB view with
  `union_by_name`. Swap grew from 6 to 8 GB and free memory fell to 19 % during the second run;
  no Jetsam report (the system log was not readable without admin rights), so a memory kill is
  the best explanation, not proven. (An earlier run, 08:11, stopped because its terminal window
  was closed.)
  - Measured on real files: `union_by_name` 30,000 files 27 s / 4.5 GB, 120,000 files 479 s /
    4.7 GB (time grows faster than the file count); without it under 1 s / 86-182 MB.
  - 19,472 silver files sampled across all 125 years: one schema (250 columns, same order and
    types), so the union is not needed.
  - Change (*ruling*): gold's version check (`_check_silver`, which already reads every footer)
    also counts the distinct column layouts; with one, the view is created without
    `union_by_name`; with several, it falls back to it and logs a warning. A progress line every
    50,000 files of the check. Gold results unchanged, no `METHOD_VERSION` bump.
  - Check on 2,280 real silver files (60 random stations, all their years), in the scratchpad:
    new reading against forced `union_by_name`: `stations, three_hourly, monthly, yearly,
    decadal` identical; `daily` the same rows except the last bits of 9 means, which also
    differ between two builds with the same reading (DuckDB's parallel float sums; open item).

- **Whole-ISD silver under 2.1.0, targeted** (13dc8b1, run 20261105T014043Z-351acb, 6 workers,
  20:40 to 00:34, 3 h 53 min; the station scan is nearly all of it, ~13-26 min per 2,000
  stations, slowest in the dense US 72xxxx range). Result: 704,429 carried, 348 built,
  6,634 no position (4,488 old files removed), 0 failed. Silver now holds 704,777 files
  (709,265 - 4,488); the ledger `_state/silver_carried.parquet` has one row per carried file
  (built 2.0.0, valid for 2.1.0).
  - Check against the 2026-11-04 position census: 4,440 of the 4,488 removals are census
    station-years (every report 0/0 or without latitude). The other 48 have latitude 0 and no
    longitude in every report: a lone 0 with a missing partner counts as missing (rules.md 4.2),
    which the census query did not count. The one census station-year not removed is Xuzhou
    58028099999 1974, placed by isd-history and rebuilt.
  - My pre-run expectation of "about 4,600 built" was wrong: most affected station-years are
    removed, not built; and the run took longer than my 2.5-3.5 h estimate.
  - `dcdr verify-silver --sample 1000` (run 20261105T121903Z-ec5cda): 1,000 carried files
    re-processed with the 2.1.0 code, `different: []`.

## 2026-11-04

- **Silver build speed-up** (user decision: option A, after the whole-ISD `process` run reached
  38 % in 17.5 h, about 258 station-years a minute). Profile of one station (New Delhi, 70
  station-years, on the internal disk: same speed as on the USB SSD, so not disk-bound): 18.4 s,
  of which 7.8 s station guards and 8.8 s building; `classify_reports` costs ~0.08 s per call
  even on a nearly empty frame (Polars sets up its ~250-column expression each time). Changes,
  results unchanged (no `METHOD_VERSION` bump, so files already built stay current):
  - the guards classify only reports that can be evidence and skip years with none (guards
    7.8 s -> 3.4 s; New Delhi 18.4 s -> 13.4 s). *Ruling:* not a single classification shared
    by guard and build: the build collapses exact duplicates and the guard does not, so a
    shared pass could change guard counts and results.
  - `dcdr process --workers N`: N stations at a time in separate processes, Polars on one
    thread each; a progress line every 500 stations.
  - Checks: 24 sample stations (426 station-years) copied to the scratchpad: new code identical
    to the running build's files (235 of 235) and parallel identical to serial (425 of 425).
    Timings on the sample while the old run still used ~1.7 cores: 1 worker 106 s, 4 51 s,
    6 39 s, 8 37 s, 10 37 s; above 6 the time is set by the largest station (22 % of the
    sample's data), not by the cores. Memory about 0.2 GB per worker.
  - *Ruling:* recommend `--workers 8` (4 performance + 4 of the 6 efficiency cores of the M5;
    two cores left for the system).
- **CI failed on 57ff180** (`test_process_rejects_zero_workers`): GitHub Actions prints Typer's
  error message in colour, and the colour codes split `--workers`; the test now strips them
  (reproduced locally with `FORCE_COLOR=1`).
- **"no valid LONGITUDE" errors in the whole-ISD run are expected**: 1,206 by 05:44, from
  station-years whose every report has LONGITUDE 999.999 (ISD missing) and no isd-history
  position (checked 49 stations: none has one; 34 are ISD placeholders named "BOGUS ...").
  They are skipped and counted as failed. Open: log them as skipped, not as errors.

- **Whole-ISD silver under 2.0.0 finished** (run 20261104T112720Z-50021c, 06:27-17:25,
  `--workers 6`): built 415,711, skipped 293,554 (current from the run of 2026-11-03), failed
  2,146; 709,265 files on disk, no `.tmp` left; a sample of 374 files all 2.0.0 with config
  6e37b492548f4874. The speed varied with the data, not the machine: ~1,100-1,200 station-years
  a minute on small station-years, ~270 on US airports (~97 MB of bronze a minute; workers often
  waiting on the exFAT USB disk).
- **Dust-day list for a collaborator** (El Paso 72270, Phoenix 72278, Fresno 72389, 2015-2020):
  gold built in the scratchpad from the three stations' silver 2014-2021 (thresholds 0, so Fresno
  is kept: 4 dusty days, below the dusty-station threshold); El Paso 90 dust days, Phoenix 43,
  Fresno 4; all 2,192 days valid at all three. El Paso and Phoenix identical to the pilot gold
  except floating-point sums (~1e-13). Not checked: Phoenix 2019-2020 (1 and 2 dust days).
- **Failures of the whole-ISD run**: all 2,146 "no valid LONGITUDE", in 584 stations and 37
  years (1944-2004); all 584 are in isd-history without a position, 305 named "BOGUS ...". 312
  stations failed in every year, 272 in some; of those, 167 had their built years at 0/0.
- **User decision: no position, no silver** (method 2.1.0; rules_history.md): station-years with
  a missing position or at 0/0 are not built and removed from silver; 0/0 reports have no
  position. Census of the 2.0.0 silver: 4,406 station-years (0.6 %) in 755 stations would be
  removed (rules.md 4.2). *Rulings:* "0" means both coordinates (single zeros are real: Tema,
  Pontianak, the South Pole); counted `no_position`, not `failed` (closes the open item on
  logging); the guards skip such years; no position is borrowed from other years (not
  verifiable). Needs a full silver rebuild (open item).
- **User clarification of the rule**: exclude a station-year only when there is no way to place
  it; isd-history places station-years and reports without a position (rules.md 4.2). Data:
  isd-history places 1 of 6,552 such station-years (Xuzhou 1974); 112 of the 113 built years
  with some reports without a position have no isd-history position. *Ruling:* isd-history
  before the year median; `lat`/`lon` stay as in the files. Kept as 2.1.0 (nothing built yet).
- **Targeted rebuild** (user decision: option B, instead of a 13-16 h full rebuild):
  `process/migrations.py` declares that 2.1.0 cannot affect stations whose reports all have a
  full in-file position; their 2.0.0 files are carried forward (ledger
  `_state/silver_carried.parquet`, files untouched), gold counts them as 2.1.0 through the
  ledger, and `dcdr verify-silver` re-processes a sample and compares. Census: 769 stations
  (4,625 station-years) affected. Checks in scratch copies: benchmark sample 425 of 425 carried
  and identical on re-processing (12 s instead of 37 s); Xuzhou, a BOGUS station and Tema behave
  as designed. *Rulings:* the whole station is rebuilt when any of its years is affected (the
  guards use all years); a ledger row counts only while the file's own version and run ID match
  it; without isd-history on disk, `process` warns and fills from the year median.

## 2026-11-03

- **metar_test_100 rebuilt under method 2.0.0** (user: `process`, 6,885 silver files, after an
  `aggregate` that rightly refused its 1.7.0 silver; then `aggregate`; gold
  20261103T164922Z-e57cb1, all checks pass). All 100 stations `agree`; the review list holds only
  the Nioro du Sahel override as `override_unused` (a pilot_100 station). Dusty-station threshold:
  97 kept, 3 left out, all with little dust in ~80 years of hourly reports: Gulkana AK (7 dusty
  days in 6 years), Northway AK (5 in 5), Honolulu (1 in 1). The kept stations have 15 to 8,547
  dusty days (median 234); only 6 lie between 10 and 30, so the cut falls in a clear gap.
- **pilot_100 gold with the threshold and the first override** (user: `aggregate`; gold
  20261103T165713Z-6b353c, all checks pass): 99 `agree`, 1 `override` (Nioro du Sahel, now at the
  isd-history position 15.239/-9.577), `not_dusty` 2 (Zhang Ping, Haikou: 0 dusty days); Shannon
  stays (42 dusty days in 20 years). `stations_review.csv` is empty. Dusty valid days 240,351 and
  dusty days at any coverage 286,103, both unchanged from the 2.0.0 pilot gold (the two stations
  left out had no dust; Nioro's offset moved ~1 min and no dusty day); valid days 1,580,441 ->
  1,561,510 (the two stations' valid days). Niamey 2020: 243 dusty days, 27 May dusty.
  *Note:* with `products.gold_keep_versions: 3` the 1.10.1 gold (05f054) and the first 2.0.0
  pilot gold (bdd7e7) were pruned; the comparisons are recorded above. A gold version covers one
  station list: the active gold now holds pilot_100; metar_test_100 is in e57cb1.
- **Dusty-station threshold** (user decision): a station goes into gold only with **at least 10
  dusty days in at least 2 different years**; no-dust control stations (pilot: Shannon, Zhang
  Ping, Haikou) are not kept in the full build. Suggested from the whole-bronze census (rough
  dust codes per report): >= 1 dust report 17,306 stations; >= 10 reports 11,445; >= 10 in >= 2
  years 11,261; >= 100 reports 4,253; stations with < 10 dust reports hold 0.24 % of all dust
  reports. Built in the gold `stations` table (`n_dusty_days`, `n_dusty_years`,
  `is_dusty_station`; `in_gold` = usable position and dusty), config `stations.min_dusty_days` /
  `min_dusty_years`; the review list shows only dusty stations; `dcdr aggregate` prints
  `not_dusty`. *Rulings:* dusty days counted as in `daily` (local day of the dusty bin, any
  coverage); test settings switch the threshold off (0/0) except in its own tests.
- **Position review of the pilot** (user decision): Nioro du Sahel (61230099999), the only
  station in `stations_review.csv` (moved, 24 km): use the isd-history position (15.239, -9.577).
  First row of `config/stations/position_overrides.csv`.
- **Pilot rebuilt with method 2.0.0 and station positions** (user ran `process` and
  `aggregate`, pilot_100, 1931-2025; gold 20261103T154804Z-bdd7e7, silver 2.0.0, isd-history md5
  b086d8ee..., no override file; all gold checks pass). Stations: 99 `agree`, 1 `moved` (61230
  Nioro du Sahel, 24.4 km); none left out. Against the 1.10.1 gold (20261102T163756Z-05f054):
  dusty valid days 233,010 -> 240,351 (+3.2 %), valid days 1,552,147 -> 1,580,441 (+1.8 %),
  dusty days at any coverage 280,198 -> 286,103 (+2.1 %); **no station has fewer dusty days** on
  either measure (`scripts/compare_gold_versions.py`). Not done: the check against a 2.0.0 gold
  built before the station positions (no such version; one build had both changes; it would only
  concern Nioro du Sahel, whose offset is unchanged by design).
- **Finding: Niamey 2020 acceptance check passes.** Exactly 7 days are newly dusty (4 May,
  19 May, 27 May, 28 June, 7 July, 16 September, 3 October; 236 -> 243 dusty days), all already
  valid. Their 10 dusty reports are routine METARs on the full hour, 60 min from the nearest
  synoptic hour, so they were never slot candidates (±30 min). 27 May: the duststorm crossed
  between the SYNOPs: 14:00 METAR `07034G48KT 0050 R09/M0150V0900 SQ DS TS`; the 12:00 SYNOP
  reported nothing significant (iX 2) and the 15:00 SYNOP ww 95 (thunderstorm with rain, which
  takes the single ww code). The `TEMPO`/`BECMG ... SQ BLDU` forecasts at 11:00-13:00 are rightly
  not counted (trend groups are ignored, rules.md 5.2).
- **Finding: why US airports gain most** (El Paso 1,206 -> 1,786 dusty valid days, +48 %;
  Phoenix 206 -> 590, +186 %). Scratch script (read-only, not in the repository) classifying each
  newly dusty day by why the old slots missed its dust: El Paso 583 days with new dust (378 only in
  routine METARs > 30 min from a synoptic hour, mostly at :51; 203 only in SPECIs) and 3 days
  newly valid; Phoenix 384 (276 only in SPECIs, monsoon haboobs; 107 in non-candidate METARs).
  Cause: of the three hourly METARs per 3 hours (e.g. 00:51, 01:51, 02:51) only one was ever a
  candidate; SPECIs never; US SYNOPs since ~2005 are derived copies (rule 5.2.9), so no
  independent look. 3 days (El Paso 2, Phoenix 1) are not explained by the slot rule and were
  not traced (probably local-day boundaries). This is the documented reporting-rate effect
  (rules.md 8, 9): compare stations with the per-source columns and the report counts.
- **Station positions, step 1: the inputs** (spec
  `docs/superpowers/specs/2026-11-03-station-positions-design.md`, approved 2026-11-03; plan
  `docs/superpowers/plans/2026-11-03-station-positions.md`). New `src/dcdr/stations.py` loads the
  saved isd-history (empty or 0/0 positions = no position, user decision 2026-11-03; a duplicate
  station ID or a missing file stops the build) and the override file
  `config/stations/position_overrides.csv` (validated: reason required, one row per station,
  `use` with a valid position, or `exclude`). Config `stations`: `isd_history`,
  `position_overrides`, `agree_km` 10, `conflict_km` 50. Test settings write an empty
  isd-history, so test stations are "unverified" and kept. (c82178f)
- **Gold `stations` table and review list** (station positions, step 2): built first in every
  gold run from all silver (`silver_all`); status per station by the four-way rule with the
  southern-hemisphere gate for 949999 (user decision 2026-11-03) and the overrides; the other
  products read `silver` narrowed to `in_gold` stations. `stations_review.csv` lists conflict,
  unverified and moved stations without an override, and unused overrides, most dusty first.
  isd-history and override md5s in the gold lineage. *Rulings:* the rules are section 4.3 of
  rules.md (no renumbering); the station-position inputs are loaded before the DuckDB connection
  opens; a station with no in-file latitude is `unverified`. (756b678)
- **Gold uses the trusted positions** (station positions, step 3): `three_hourly` takes each
  station's LMST offset from `stations.trusted_lon`; `daily` dates the daily summaries with it
  (no longer silver's `summary_lmst_date`, from the in-file longitude). New gold checks: only
  stations in gold in `three_hourly` and `daily`. Unchanged for agree/moved/unverified stations
  (same longitude as before); a resolved 949999 station now has Australian local days. (a1af02d)
- **`dcdr aggregate` and `dcdr run` print the stations per position status**
  (`aggregate.station_status_counts`), beside the gold folder. (7dde73e)
- **Final review of the station-positions branch** (fresh reviewer, whole branch): no critical or
  important findings; real isd-history read correctly (29,661 stations, 1,566 without a position).
  *Fixed* (each test-first): a 949999 station without an in-file latitude is a `conflict`, not
  `unverified` (its in-file longitude may be a US one); an override row with an empty or
  non-11-digit `station_id` stops the build (a spreadsheet can drop a leading zero), and errors
  name the file line. *Deferred minors:* an empty (0-byte) override file gives polars' error, not
  `OverrideError`; `asin` just above 1 for near-antipodal pairs (not reproduced on 63 antipodal
  pairs); `decided` not checked as a date; tests for several years with different positions, an
  `exclude` absent from `three_hourly`, and a 949999 station already Australian in its file.
- **Method 2.0.0 started: source groups in silver** (spec
  `docs/superpowers/specs/2026-11-02-all-reports-bins-design.md`, approved 2026-11-03; plan
  `docs/superpowers/plans/2026-11-03-all-reports-bins.md`). User decisions 2026-11-02: 3-hour UTC
  bins over all reports replace slot sampling; groups SYNOP (FM-12, SY-MT, SY-SA, SY-AE, SY-AU,
  AUST), METAR (FM-15, FM-16, SAO, SAOSP, AUTO), other (every other type); SOD/SOM excluded.
  New `process/sources.py` adds `source_group` and `is_excluded_type`; config section `sources`
  (`exclude_types` moved there from `sampling`); silver contract: a group exactly on the
  non-summary reports. Sampling still runs until the bins replace it. `METHOD_VERSION` 2.0.0.
  *Ruling:* a missing or unknown report type is "other" (never excluded). (9313d86)
- **`three_hourly` on source groups** (method 2.0.0): new `dust_synop`, `dust_metar`, `dust_other`
  (true / false / null = not observed by that group), `dust_vis5` (null when a dusty report's
  visibility is unknown), `wsd_max`, `nil_inferred`, `nil_metar`; regular reports from
  `source_group` (SPECI/SAOSP not regular); **one LMST offset per station** (its most common
  longitude; spec, after the builder check found that the state report's own longitude can put 9
  bins on a local day). Three new gold checks. The `synop_types` / `metar_types` variables are
  gone. *Ruling:* the old test "LMST uses the state report's longitude" is replaced by
  `test_one_lmst_offset_per_station`. (6ef64dc)
- **`daily` built from the bins** (method 2.0.0): `three_hourly` runs first and `daily.sql` reads
  it. A day = the 8 bins starting on its LMST date; dusty day >= 1 dusty bin; valid day >= 4
  observed bins (`min_bins_per_day`, was `min_slots_per_day`). Each source group judged alone
  (`n_bins_obs_<g>`, `n_bins_dust_<g>`, `valid_day_<g>`, `dusty_day_<g>`, with the new `other`).
  Means from the state reports, extremes from all reports, no visibility fallback;
  `dust_first_lmst_hour` = start of the first dusty bin. Dropped: `dusty_day_any`,
  `dusty_day_any_source`, `n_vis_from_metar`, all `n_slots_*` (renamed `n_bins_*` in the
  statistics too; `n_dusty_days_any` dropped). Daily checks rewritten for bins.
  *Rulings:* the Kuwait "slot on its own LMST day" test and the slot-source tests are dropped
  (superseded by `test_bin_counts_on_the_lmst_date_it_starts_across_years` and the per-source
  tests); `scripts/thin_slice_report.py` fixed now (it read `dusty_day_synoptic`, a column that no
  longer existed, and the slot selection). (0239af7)
- **Statistics: each source judged alone, incl. `other`** (user decision 2026-11-03, option B: not
  an attribution of the all-source count). `monthly` adds `n_obs_days_other`,
  `n_valid_days_other`, `n_dusty_days_other`, `n_dusty_days_obs_other`, `n_bins_obs_other`;
  `yearly` and `decadal` sum them generically; `STAT_VARIANTS` gains `_other`, so the count-order
  checks cover it. The all-source columns keep their names (`n_dusty_days`, `dusty_freq`, ...).
  (a910c1d)
- **Slot sampling removed from silver** (method 2.0.0, user decision 2026-11-02): `sampling.py`,
  the slot columns, the off-grid shift (1.10.0), the neighbouring December/January reads (1.10.1)
  and the config section `sampling` are gone; each report lives in the file of its own UTC year,
  so the next year's bronze is no longer read (silver metadata without `next_year_present`,
  `next_source_md5`). A profile that still sets `sampling` is rejected. The open item "±90 min
  fallback window" is obsolete (no slots). (b04ace6)
- **`scripts/compare_gold_versions.py`** (read-only): per station, dusty and valid days summed over
  `yearly` in two gold versions, the difference, and the stations whose count dropped; for the
  2.0.0 pilot check (`uv run python scripts/compare_gold_versions.py OLD_RUN_ID NEW_RUN_ID`).
  (b747481)
- **Final review of the 2.0.0 branch** (fresh reviewer, whole branch): no critical or important
  findings; every gold check holds by construction. *Fixed:* the comparison script also compares
  dusty days at any coverage (`diff_obs`), so a drop caused by validity changes is told apart from
  a drop in dust detection. *Rulings:* rules.md 4.2 now says the station LMST offset is taken over
  the gold run's years (the whole record for the published products), not changed in code;
  deferred minors: `is_special` independent of the configured groups (only an odd config trips the
  gold check), the Review Focus 5 test at 15 E (bin start 22:00, not 23:xx).
- **Whole-bronze census** (2026-11-02, read-only scratch scripts, not in the repository): 711,411
  station-year files, 30,375 stations, 1901-2025, 3.90 billion reports. Largest types: FM-15
  1,445 M; FM-12 1,147 M; SAO 469 M; CRN05 329 M (no weather); FM-16 116 M; AUTO 83 M; SY-MT
  74.5 M (rough dust codes at 2,035 stations); NSRDB 68 M; AERO 4.9 M (dust codes at 234 stations,
  1952-84). Timing: FM-12 7.8 reports a day, 8 bins on 40.6 % of days; SY-MT like FM-12; SY-SA 4
  bins (00/06/12/18) on 67 % of days; AUST 1.2 % on synoptic hours, peaks at 23 and 05 UTC (local
  standard clock). AUST only in Australia (716 stations, 1939-1984; 89 % of its station-years
  without FM-12). These findings set the source groups (user decisions 2026-11-02).
- **Finding: wrong in-file metadata at 949999 stations** (2026-11-02). 370 stations with USAF
  949999 are Australian in isd-history.csv; 268 carry a US station's NAME/LATITUDE/LONGITUDE/
  ELEVATION in NOAA's data files (shared WBAN). Example 94999900108: in-file "SCAMMON BAY
  AIRPORT, AK US", isd-history ADELAIDE (WEST TERRACE), -34.93, +138.58, 1954-1979; the data
  confirm Adelaide (January 22.8 C, July 11.5 C). None in the pilot. Positions vs isd-history,
  largest station-year distance: < 10 km 27,794 stations; 10-50 km 76; 50-200 km 14; 200-1000 km
  12; >= 1000 km 279 (268 of them 949999); no reference 1,874. Errors go both ways (in-file WBAN
  collisions and digit slips; isd-history wrong for Concepcion BO and Sable Island). *User
  decision:* the four-way position rule (Open items). isd-history.csv saved at
  `$DCDR_DATA/catalog/isd-history.csv` (S3 Last-Modified 2025-08-30 01:27:57 GMT, 2,914,601
  bytes, md5 b086d8ee9fdbfa8cf545847827031e0b, 29,661 stations).

## 2026-11-02

- **Gold rebuilt with the statistics** (user ran it; gold 20261102T152055Z-66b2fc, silver 1.10.1,
  all checks pass): daily 1,916,267 rows, monthly 65,611, yearly 5,675, decadal 12,288, 100
  stations, as in the review probe. `n_slots_dusty`: 860,914 dusty slots on 280,198 dusty days
  (3.1 per day; every dusty day has one). Annual decadal coverage: 82-90 stations with >= 8 valid
  years in 1991, 2001 and 2011; 1931-1961 sparse; 2021-2030 partial.
- **Finding: valid days chosen by the weather.** The decadal ranking for 2011 is led by Tidjikja
  (61450, 221 valid of 1,881 observed days, `dusty_freq` 1.0, annual mean 365.3), Gao
  (61226, 0.993) and Tombouctou (61223, 0.97); the same three exceed 0.84 in every decade since
  1991. Cause, at Tidjikja 2014 (silver): iX 2 is sent only at 09 and 18 UTC; at 06, 12 and 15 UTC a
  report without weather carries iX 3 (not observed). A clear day has at most 2 valid slots (< 4,
  `min_slots_per_day`), a dusty day has codes at every hour, so only dusty days are valid. The
  frequency on valid days is then biased up; seen on any observed day, Tidjikja is dusty on about
  half of them (a lower bound). Not a code error: the rule "not observed unless verified" works as
  designed, but the valid-day frequency cannot be read where validity depends on the weather. A
  crude screen (valid-day frequency > 2x the observed-day bound, 1971-2011) flags 5 other
  station-decades (e.g. 94322 in 1981, 0.64 vs 0.31), so it does not catch Tidjikja: the screen
  needs design.
- **Lower bound and validity ratio** (this commit; tests first, 5 new tests). *User decisions:* no
  rule change; counts stay the primary record (as in the author's duISD research: counts of
  observed dusty days, filters for non-representative periods, applied by the user); add
  `dusty_freq_obs` = `n_dusty_days_obs / n_obs_days` (lower bound) and `validity_ratio` (valid
  share of clear observed days / valid share of dusty observed days) to `monthly`, `yearly`,
  `decadal`; **no flag**, because the right threshold is not known (a flag at 0.5 was proposed and
  dropped). Every rate defined with its formula in rules.md 11.4. New gold checks: null exactly
  where undefined, `dusty_freq_obs` in 0-1, and the identity `dusty_freq = f / (f + r(1 − f))`;
  a mutation test shows each new check fires. Run in memory on the real `daily`: all checks pass;
  median r 0.99 (station-decades), Tidjikja 0.0, Gao 0.04, Tombouctou 0.15 (2011-2020).
  *Rulings:* rates from summed counts (not means of monthly rates); primary definition only; no
  minimum number of dusty days for the ratio (documented as noisy with few). `METHOD_VERSION`
  unchanged. (632c899)
- **Gold rebuilt with the rates** (user ran it; gold 20261102T163756Z-05f054, silver 1.10.1, all
  checks pass; values as in the in-memory run). `validity_ratio` defined for 34,511 of 65,611
  station-months (median 1.0, 2,081 below 0.5), 4,777 of 5,675 station-years (0.997, 249) and
  9,895 of 12,288 decadal rows (0.99, 537). 2011-2020 annual: below the three Sahel stations
  (r 0.0-0.15) the leading stations have r 0.88-1.05 and `dusty_freq` within 0.03 of
  `dusty_freq_obs`. Monthly ratios are the noisiest (few dusty days per month).
- **Finding: daily slot sampling misses short events at hourly stations.** Niamey 2020 has 7 days
  with dust in some report but no dusty day (`dusty_day_any` and not `dusty_day`); all 7 come from
  routine hourly METARs (FM-15) not chosen for a slot (`is_source_selected` false), none from SPECIs.
  One is the 27 May haboob (dust storm in the 14:00 UTC METAR, visibility 50 m): a dust storm in
  the 3-hourly table, not a dusty day in the daily record. Known trade-off of equal sampling
  (rules.md 9-10); first case seen for a major event. No rule change; shown to the user while
  designing the daily web page.
- **Next phase: distribution and website** (*user decisions*): the dataset is open and
  non-commercial, licensed accordingly; the pilot dataset (100 stations) is used to finish the
  website design before the full station set; update functionality is postponed (ISD's future is
  outside our control), releases cover the existing record.

## 2026-10-31

- **1.10.1 rebuild checked** (user ran it 2026-10-30; gold 20261030T221323Z-eb6488, all checks
  pass): the per-local-month grid gave the Australian off-grid SYNOP stations their valid days
  (e.g. Papunya 0 → 3,449, Rabbit Flat 53 → 6,367); `three_hourly` has 12.2 million bins.
  Website: design can start; build after the statistics.
- **Gold statistics `monthly`, `yearly`, `decadal`** (commits 0002138, c02f436, 7cdc26c, this
  commit; tests first, 12 new tests). *User decisions:* valid day unchanged (≥ 4 of 8 observed
  slots); report every count, filter later; visibility only in `monthly`, no other weather
  variables; `decadal` with decadal, month-wise and season-wise means. *Accepted proposals:*
  presence from any day, frequency from valid days; inhomogeneity detected from stored counts
  (slots, report mix, class shares), not corrected. *Rulings:* full calendar lengths; DJF pooled
  within the decade; decades fixed (years ending in 1); `ext_mean_dusty` over dusty days at any
  coverage; `n_slots_dusty` primary only; `min_valid_days_month` = 10; `dusty_days_mean` =
  `dusty_freq × n_calendar_days / 10` (frequency-scaled, so gaps are not counted dust-free). No
  `METHOD_VERSION` change (silver unchanged). Details: rules.md §11, code_design.md §7.5,
  rules_history.md.
- **Review of the statistics** (fresh reviewer, whole branch): no critical findings; on the real
  pilot `daily` (1.92M rows, 100 stations; read in memory, nothing written) monthly 65,611 rows,
  yearly 5,675, decadal 12,288, built in under 0.5 s with every check at 0. Fixed (test first): the
  three tables had no `ORDER BY`, so identical runs wrote rows in different orders (not
  reproducible); now sorted by their keys. *Deferred minors:* no lower bound (`ge=1`) on
  `min_slots_per_day` and `min_valid_days_month` (0 fails the build with a check error); no
  two-station decadal test of the calendar join; worklog entries came only with the last commit;
  the `n_` summing contract is enforced by comments, not a column-set test; the annual
  `dusty_days_mean` can reach 365.3 (one sentence for website readers).
- **Uth and dust-source stations** (user decisions, future work): Uth computed record by record
  as in legacy `calculate_uth`, over 3- or 5-year centred windows, only at dust-source stations.
  Source = aridity index < 0.5 (hyper-arid, arid, semi-arid), decadal mean dusty days > 14 a year,
  enough coverage (e.g. ≥ 5 years with valid days) and raised-dust evidence (accepted proposals);
  thresholds set from the statistics over 1991-2020 (30 years, configurable). A station that is a
  source over that window is a source for all years; changes show in the Uth trend. Wind
  histograms (station × year × month × 1 m/s bin) for all stations; a `uth` table only for
  sources, with fit-quality checks per window. Order: statistics → catalog with aridity and source
  flag → Uth.
- **Incident (Claude's error):** a scratch check of the decadal sum checks ran outside pytest with
  the user's `DCDR_DATA` set, which overrides `load_settings` overrides. It wrote a fake silver file
  (`silver/year=2024/S.parquet`) and a fake gold version to the SSD and moved `gold/CURRENT`;
  pruning (keep 3) then deleted the oldest kept gold version, 20261020T192837Z-ab3f05 (built
  2026-10-20 from that day's silver). Restored `CURRENT` to 20261030T221323Z-eb6488 and moved the
  fake files off the SSD; the pruned version is lost (rebuildable only from bronze with the code
  of that date). Rule now: scratch builds run with `env -u DCDR_DATA`.

## 2026-10-30

- **Method 1.10.1: slot grid per local month** (user decision: option A; this commit; tests
  first, 311 pass). The first gold build after the user's 1.10.0 rebuild failed the check "at most
  8 slots per day": 68 local days with 9 slots at 7 Australian stations, each the first local day
  of a month whose grid changed (1.10.0 switched grids at the UTC month change, inside the local
  day). Now the grid is chosen per local month and a slot uses its local month's grid; year files
  read the neighbouring December and January for the decision (a local month can straddle New
  Year) and store the next year's bronze fingerprint. *Rulings:* a report beside a local
  midnight where the grid changes may fit no slot; derived SYNOPs ignored for detection. Checked
  in memory on real bronze: Sydney 1995-2025 and Tennant Creek 2018-2025 at most 8 slots per local
  day; El Paso 2010 selection identical to 1.10.0. My error in 1.10.0: month changes were tested
  only at longitude 0. Details: rules.md §8, rules_history.md 1.10.1.
- **Dusty-station list** (discussion, open): proposed criteria from pilot gold 1991-2020 (dusty on
  >= 1 day per 365 observed days, dust in >= 50 % of observed years and in >= 5 years; separate
  "insufficient observation" and "borderline" groups; aridity and raised-dust share as
  attributes, not filters). Tombouctou, Gao and Tidjikja have no year with >= 200 valid days, so
  inclusion must not require dense coverage. Open: recent window or any 30-year window.
- **New gold table `three_hourly`** (this commit; tests first, 15 new tests). One row per station
  and 3-hour UTC bin (00:00-02:59, ...), from all reports except summaries.
  *User decisions:* the product and its bins; all reports, SPECIs included; state values from one
  report, the first regular SYNOP or METAR (not the legacy per-column `first`); the daily product
  stays independent (still built from silver with slots). *Rulings:* summaries excluded; SYNOP
  before METAR at the same minute; a bin without a regular report uses its first report of any type;
  missing state values not filled from other reports; columns `dup6`, `first_dust_time_utc`,
  `observed_explicit`, vicinity/past/remarks flags, LMST of the bin start; product name
  `three_hourly` (a DuckDB view name can't start with a digit); `METHOD_VERSION` unchanged (silver
  unchanged). Legacy check: its 3-hourly dust flags came from the largest MW code in the bin, so
  rain or thunderstorm codes hid dust. Details: rules.md §10, code_design.md §7.4,
  rules_history.md.
- **Review of `three_hourly`** (fresh reviewer, whole branch): no critical or important findings;
  the SQL run over all real pilot silver (97.5M reports, method 1.9.0 files) gave 30.76M bins with
  all six checks at 0, in 577 s and 3.4 GB peak memory (reading from the external drive), so
  `dcdr aggregate` now takes about 10 minutes longer. Fixed (tests first): `SAOSP` (pre-1996 US
  airways special, ISD format document p. 6; 486k reports) counted with the SPECIs, not in
  `n_other`, so `n_speci` doesn't jump at the SAO-to-METAR change; LMST of the bin from the state
  report's offset (was `any_value` of the longitude: not reproducible if a station moved within a
  bin). *Rulings (reviewer's minors, deferred):* none open beyond the run time noted above.
- **Why daily dust uses 8 slots, not every report** (discussion): on 1.9.0 gold, 1991-2020 valid
  days, `dusty_day_any` / `dusty_day` = 1.02 pilot-wide but 3.4 at Phoenix, 1.9 at El Paso, 1.7 at
  Las Vegas and 1.00 at the Sudan SYNOP stations, rising with reports per day (Spearman 0.79);
  pilot reports per day went from 10.8 (1975) to 19.9 (2020). Equal sampling keeps stations and
  years comparable; `dusty_day_any` and `three_hourly` serve event studies.
- **rules.md renumbered:** new §10 (3-hourly); station selection §11, open questions §12,
  references §13.

## 2026-10-29

- **Method 1.10.0: off-grid SYNOP schedules** (user decision: option a; tests first, 291 pass).
  `sampling.py`: `schedule_offsets` finds, per station and UTC month, a 3-hourly schedule one hour
  off the synoptic hours from the observed SYNOP-source reports, and moves that month's slots
  (`slot_offset_min`); `AUST` added as the last SYNOP type. Verification before the code changed
  the design twice: (1) the on-grid Australian SYNOPs since 2000 rarely observe the weather, so the
  schedule is read from observed reports only (the first stream comparison was invalid for this
  reason; the report-by-report METAR check passes: 0.91 / 0.68 vs 0.94 elsewhere); (2) the
  schedule follows daylight saving (Woomera 2005), so detection is per month, not per year.
  *Rulings:* thresholds (< 10 % on grid, >= 80 % at one offset, >= 30 a month). On pilot_100,
  7,084 station-months at 16 stations shift (12 Australian, Sydney, 3 Central Asian in the 1930s),
  none elsewhere. Details: rules.md §8, rules_history.md 1.10.0.

- **pilot_100 rebuilt for method 1.9.0** (user): silver and gold 1931-2025; gold version
  `20261029T160035Z-48964f` (one Parquet file, 1,916,268 station-days, 100 stations).
- **Notebook 07 (local): the daily product checked as a whole** (coverage, 1991-2020 maps per
  measure, distributions, seasonal and diurnal cycles, wind, time series, rule diagnostics,
  duISD v1). Results in rules_history.md ("Daily product check"):
  - Rules pass: no break in the dusty share where the inferred share jumps (1985, 2013); seasonal
    and diurnal cycles and wind as expected (86 of 88 stations windier on dusty days); duISD v1
    Spearman 0.86, median year-to-year r 0.81.
  - Gap found: 497 station-years at 17 stations (all Australian stations, Sydney, three 1930s
    Central Asian years) have observed reports that miss the slots. Australian FM-12 on a
    3-hourly schedule +-1 h off the synoptic hours (east/centre 1 h before, west 1 h after; not
    copies of on-grid reports: temperature identical in 3-6 %); `AUST` (1945-1984, 339k reports)
    is not a slot source. Decision pending.
  - Measures differ: Sudan stations 190-260 dusty days a year, mostly suspended dust above
    5 km (`dusty_day_vis5` 10-35 % of `dusty_day`); dust_haze-only days 5 % pilot-wide (29 % at
    Ciudad Constitucion).

- **Open questions decided** (user decisions, all recommended options; rules.md §11 now lists only
  NOAA's data currency and product coverage criteria):
  1. BUFR-only SYNOPs (no iX): current handling kept; no other source.
  2. Dust only in METAR remarks: separate daily flag `remarks_dust_day` (method 1.9.0).
  3. Niamey's METARs missing dust at <= 5 km: station caveat in rules.md §5.2, no rule change.
  4. Humidity screen: only the haze-based classes (haze, haze_or_suspended, dust_haze); suspended
     dust (06, `DU`) is kept (method 1.9.0). Evidence: 3,724 of 616k suspended reports screened
     before, 14 % at Luqa. `has_pw` sampling tie-break kept.
  5. pilot_100 kept as a test set; coverage criteria come with the products.
- **Method 1.9.0** (tests first, 281 pass). `parse.py`: `metar_rmk_dust` from the remarks of a full
  METAR (`METAR_RMK_DUST`); `daily.sql`: `remarks_dust_day`; `default.yaml`:
  `rh_screen_classes: [haze, haze_or_suspended, dust_haze]`. *Rulings* from a check on pilot_100
  bronze, all years: forecasts inside remarks are cut (`TEMPO`, `BECMG`, `NOSIG`, `FMhhmm`); bare
  `SA` (Woomera observer initials, 102 of 102) and `SS` (a direction) not used. 8,333 reports
  flagged.

## 2026-10-28

- **Documentation restructured** (user request): `engineering.md` renamed `code_design.md`;
  `science.md` split into `rules.md` (the rules as implemented, method 1.8.0; section numbers kept
  for 1-12) and `rules_history.md` (per method version: change, evidence, superseded rule,
  decision; the validation results of notebooks 02-06; resolved questions). References in code,
  docs, README and CLAUDE.md updated. `docs/superpowers/` (design spec and plans) made local only
  and removed from the whole history (user decision; force-pushed; commit hashes changed again,
  references in this log and in rules_history.md updated).

- **Method 1.8.0: METARs without full text, derived SYNOPs, visibility** (user decisions, after
  notebook 06; tests first, 265 pass).
  - Pre-2000 METAR fragments: `CAVOK` before any trend word or remarks = explicit "nothing
    significant" (`parse.py`: `metar_text` full/fragment; `metar_wx = cavok` from a fragment).
  - METARs without full text: no code + visibility > 5 km = `nil_inferred` where the station
    passes the new METAR guard (`guard.py`: `metar_guard_counts`, `station_guards`; silver
    metadata `metar_guard`; a guard change triggers a rebuild).
  - Derived SYNOPs (`derived.py`, per station-year): codes and iX not used; flag `synop_derived`.
  - Visibility for the day from the slot's METAR when the primary SYNOP has none
    (`n_vis_from_metar`).
  - *Rulings:* the METAR inference reuses the status `nil_inferred` (counted with V3 in
    `n_slots_nil_inferred`, excluded from `valid_day_explicit`); derived detection on FM-12 vs
    FM-15 only; a station-year frame is assumed to hold one station.
  - Real-data check (in memory): El Paso 2020 SYNOPs detected as derived (2,896 not observed for
    weather); Gimpo 1985: of 16,076 METARs, 5,656 CAVOK from fragments and 4,073 inferred,
    93 not observed (before: most of the ~10,000 uncoded ones).

## 2026-10-27

- **Tests (a) and (b) over all years** (user request; notebook 06, local). Station catalog of
  the whole archive built from bronze (`DCDR_DATA/catalog/`: station-year inventory, 1.18 M rows;
  stations with position, report types, country, aridity from the Global Aridity Index v3).
  Answers: 5,450 stations report METAR in 2015-2025, 2,274 of them in drylands (42 %); METAR-only
  stations are 67 % American (2,090 of 3,110); 798 US METAR-only stations are in drylands (TX,
  CA, CO, the Plains, AZ, NM, UT). Extra test stations: `config/stations/metar_test_100.csv`
  (60 non-US dryland METAR stations, 40 US long-record stations).
  - (b) derived SYNOPs: 94.7 % of US station-years with both types since ~2005, 0.7 % elsewhere;
    dust survives in 17 % (independent SYNOPs 91 %).
  - (a) pre-2000 METAR text is only fragments, `CAVOK` in 30-37 %; without text dust frequency
    x2.6; with `CAVOK` x1.05; guarded inference (no code + vis > 5 km) x1.07, precision
    0.993-0.999; both x1.00. SAO not biased (explicit "nothing" codes).
  - Options to decide (none implemented): (1) read `CAVOK` in pre-2000 fragments as "nothing
    significant" (explicit; only before any trend word, since "BECMG CAVOK" is a forecast);
    (2) add the guarded inference for METARs without full text, flagged as inferred;
    (3) derived SYNOPs (temperature >= 95 % and wind >= 80 % identical, >= 100 pairs per
    station-year): present weather "not observed", dust and validity from the METAR; weather
    values still from the SYNOP.

- **Fix: METAR text without the word METAR** (method 1.7.0; tests first, 236 pass). Found while
  testing (a) on 200 stations: in the 2000s, 47 % of METARs have full text that starts with the
  station identifier ("MET046LROP 282200Z ... NOSIG;", ending in ";"), which the parser skipped,
  so the METAR rules (AUTO, //, CAVOK, no group) were not applied to them. Also found: before 2000,
  REM holds only the fragments ISD did not decode (e.g. "CAVOK NOSIG"), in 93-98 % of METARs;
  `CAVOK` appears in 30-38 % of them (an explicit "nothing significant"; not used yet, see (a)).

- **V3 kept for all years, no visibility-value condition** (user decision, after notebook 05):
  before 1980 SYNOPs carry a code, so V3 is rarely used; its impact is 1980-2013; the visibility
  value has little effect on precision, so its presence is the test. Caveat recorded: 1930s-40s
  Central Asian stations (Aralsk, Zhosaly, Naryn) rely on V3 for most reports.

- **V3 across decades** (user request: V3 had been validated on 2015-2024 only; notebook 05,
  local). Ground truth before 2010: 265 reports. V3 supplies 0.4-1.0 M nil slots per decade from
  the 1980s (barely used 1950-1979: 99 % coded). Switch-year test (iX appears 2013-2015; 79
  stations): dust frequency drops a median 1.9 points (normal 0.0), large drops at some dusty
  stations (Niamey -38, Zahedan -36); confounded by ISD's 2013 change in SYNOP intake. A first
  pass was distorted by an ISD gap: 2009-2012 had 20-200 SYNOPs a year at Sal, Zabol, Niamey,
  Zahedan. Old ground truth: precision 0.81 (156 inferences, 1970s-80s). V3 has no visibility
  value condition (answer to the user): precision 0.986 at <= 1 km, >= 0.998 above.
  Options to decide (none implemented): (1) keep V3 for all years; (2) V3 only from 1980 (before,
  SYNOPs are coded anyway; 1930s-40s Central Asian inferences unverifiable); (3) V3 only in the
  iX years (strict "verify or not observed", brings back the selection bias in 1980-2012); plus,
  with any option, require visibility > 1 km.

- **Whole-ISD ingest complete** (user ran it): 711,411 station-year files recorded, 30,375
  stations, 1901-2025, 0 failures, none changed on S3 since; bronze 103.7 GB. Checks found:
  - 85 files of 1990 (the pilot stations, recorded by the 2026-10-20 pilot run) were gone from
    disk: the 1990 folder was emptied after that run, and ingest skipped them because its record
    said "ok". Fix (tests first, 229 pass): ingest also downloads a recorded file whose bronze
    file is missing.
  - 41 stations (531 files) have IDs starting with a letter (e.g. `A0000253928`). The pipeline
    handles them; the ad-hoc glob `[0-9]*.parquet` (used to skip macOS `._` files) does not:
    use `[0-9A-Z]*.parquet`.

## 2026-10-22

- **Documented the manual download of the whole ISD archive** (user request; code_design.md 8,
  "Download the whole ISD archive"): station list, detached year-by-year loop, monitoring,
  stopping, resuming, completeness check. New `scripts/list_isd_stations.py` writes the station
  list (reproduces `config/stations/all_isd.csv` exactly). Lessons from the run: `uv run dcdr` only
  works inside the project folder (a loop started from the home folder failed with "Failed to
  spawn: dcdr" for every year), so the command now `cd`s into the project itself; `pkill` of
  `caffeinate` alone may leave the loop running.

- **Ingest saves its record and logs progress every 500 files** (user request; tests first,
  228 pass). Downloading the whole ISD archive (30,375 stations, 711,411 station-year files,
  1.47 TB of CSV, 1901-2025; station list `config/stations/all_isd.csv` from the S3 listing) was
  stopped twice mid-year (Ctrl-C; a closed terminal). The manifest was saved only at the end of
  a run, so each stop lost a whole year (20-30 min). Now `ingest` updates and saves the manifest
  after every `ingest.checkpoint_every` (500) files and logs `done/total files, MB/s, failed,
  time left`; a stopped run loses at most 500 files. Progress at that point: 1901-2013 complete,
  552,184 files (78 %), 871 GB of CSV (59 %), 0 failures.

## 2026-10-20

- **`legacy/` and `notebooks/` kept local only** (user decision): removed from the public
  repository and from its whole history (`git filter-branch --index-filter`, force-pushed);
  git-ignored, still on the author's disk. Commit hashes changed again; references in this log
  updated. The docs keep their references to these folders, noting they are local.

- **Repository made public** (user decision), with an MIT licence for the code (user decision;
  `LICENSE`, README; `pyproject.toml` already declared MIT). The duISD v1 script keeps its creator
  e-mail in `legacy/master_DCDR.py` (user decision: legacy stays verbatim).

- **Repository published to GitHub (private)** (user decision): `github.com/ixnix/DCDR`. The user is
  the only author; the `Co-Authored-By` trailers were removed from all 59 commit messages
  (`git filter-branch --msg-filter`), which changed every commit hash; content is unchanged
  (same final tree). The commit references in this log were updated to the new hashes.

- **Full-record test of the slot sources** (user request; notebook 04; method 1.6.0 unchanged).
  Ingested the whole NOAA record for pilot_100: `dcdr ingest --years 1931-2026` (5 651
  station-year files, 4 275 new, 1.7 GB bronze, 5 min; NOAA has no 2026 files and 2025 ends on
  27 August), `dcdr process` (5 651 built, 0 failed, 18 min), `dcdr aggregate` (1.92 M
  station-days, all checks passed). Findings (rules_history.md (full-record test)):
  - METARs without raw text (none before 2000) count as observed only when coded: valid days
    -90 % and dust frequency x4.6 (median) when simulated on 2010-2024 data. Pre-2000 METAR
    periods are biased (Gimpo; El Paso before 2005; Dammam, Ilam, In Salah have no SYNOP in ISD
    before 2005). Pre-1960 SAO reports always carry a code (mostly 00): not affected.
  - SYNOP vs METAR in the same slot: wind +2.4 m/s at Aktyubinsk, +1.3 Jodhpur, +1.1 Kashi;
    temperature +1.0 °C Kashi; median +0.05 °C, 0.0 m/s. Supports one source for weather values.
  - SYNOP-first weather values: visibility missing although the METAR has it in 11.5 % of slots
    with both (Australian automatic SYNOPs 50-63 %); temperature 0.2 %, wind 1.1 %.
  - METAR adds 3 541 of 220 152 dusty days (1.6 %); where both observed, METAR-only dust 2 430
    slots vs SYNOP-only 42 439 and both 36 078. Main source at El Paso (79 %), Las Vegas (60 %),
    Phoenix (53 %), Gimpo (100 %, no SYNOP).
  - Rule V3 inferred "nothing significant" for 151 US SYNOP slots in which the METAR reports dust.
  - 26 switch years (METAR share of weather values changes by >= 0.3) at 16 stations; no
    systematic jump overall, but single wind jumps (Learmonth 1999 -2.2 m/s).
  - Options to decide (none implemented): (a) pre-2000 METAR without text: count for dust only,
    never for validity; or validate a V3-like inference (no code + visibility reported) on
    2000+ data with text, guarded per station; (b) visibility: accept gaps, or take only
    visibility from the METAR when the SYNOP has none; (c) V3 at US stations: exclude stations
    whose SYNOPs are auto-generated (guard against METAR dust).

- **Weather variables from the SYNOP whenever the slot has one** (method **1.6.0**, follows
  cb3b298; user decision). In 1.5.0 the primary report (wind, temperature, humidity, pressure,
  visibility statistics) was the observed SYNOP, else the METAR, so an unobserved SYNOP gave way
  to the METAR and a station's series could switch report types from slot to slot. Now the
  primary report is the slot's SYNOP, observed or not, and the METAR only in slots without SYNOP.
  Dust is unchanged (either source). *Ruling:* a value missing from that SYNOP stays missing (no
  per-variable filling from the METAR). Tests: 2 new, 226 pass.

- **SYNOP and METAR judged independently per slot** (method **1.5.0**, follows 20d7575;
  tests first, 224 pass).
  - *Found:* US SYNOPs rarely carry the dust their METARs report. Pairing each dusty METAR with the
    next SYNOP within 15 min (2015-2024): El Paso 103 pairs, SYNOP dust in 1; Phoenix 12, 1. Of El
    Paso's, 92 say iX 1 ("group included") but contain no weather group → "not observed", yet
    under 1.4.0 they still filled the slot (report type ranked before "observed"), hiding the dust.
    Las Vegas reports at :56, El Paso and Phoenix at :51. The US stations had no SYNOP in the 1985
    and 1995 samples and 4-8 a day since at least 2005.
  - *User decisions:* SYNOP and METAR are independent per slot; a slot is dusty if either reports
    dust. When the SYNOP observed the weather it is the primary input and METAR the supplement;
    when it did not (e.g. automatically generated), the METAR fills the gap. Confirmed: sources
    SYNOP = FM-12 + SY-*, METAR = FM-15 (+ SAO, AUTO); SPECI never fill a slot (event-driven;
    still in `dusty_day_any`); at most one report per source per slot (closest that observed the
    weather); combined measure is the primary `dusty_day` (renames `dusty_day_synoptic`), with
    `dusty_day_synop` and `dusty_day_metar` alongside; weather means from one primary report per
    slot (the SYNOP if it observed the weather).
  - *Rulings:* `valid_day_explicit` now counts slots with an explicit observation from either
    source (`n_slots_pw_explicit`) instead of subtracting inferred slots; a slot counts in each
    dust class one of its two reports has; a slot dusty only through a report of unknown
    visibility is left out of `valid_day_vis5`; `sampling.priority` and the unused
    `synoptic_dust_source` were replaced by `sampling.sources`.
  - Code: `sampling.py` (`slot_source`, `source_rank`, `is_source_selected`; `is_synoptic_selected`
    is now the slot's primary report), `sql/daily.sql` rewritten (slots combine sources; weather
    statistics from primary reports), gold checks, silver contract (one primary and one report
    per source per slot).
  - Check on real 2020 bronze (built into a scratch folder): El Paso 5 dusty days (SYNOP alone 1,
    METAR alone 5); Phoenix 1 (0, 1); Zabol 126 (126, 34); Niamey 236 (235, 129); Gran Canaria 2.
    All five have 366 valid days.

- **METAR rules implemented** (method **1.4.0**; tests first: 41 new, 215 pass). Full rule tables
  for every report type: rules.md 5.3. Decisions:
  - *User decisions:* SYNOP is primary and METAR a supplement: a METAR fills a slot only when no
    SYNOP does, and the SYNOP is the truth when both exist (already how sampling ranks). METAR
    dust counts **at any visibility** (the group says dust; visibility tells its strength). `HZ`
    is haze first, then the 24-hour haze rule. Vicinity dust (`VC`) stays out of the dust counts
    and becomes the daily flag `vicinity_dust_day` (370 VC METARs in 3.18 M; would add 80 days at
    Las Vegas, 33 at El Paso, 0 elsewhere). Gold adds `dusty_day_vis5` (dust with visibility
    <= 5 km, caught alike by SYNOP and METAR; for comparing stations and years with different
    report mixes; for SYNOP the 5 km limit is a derived threshold, table 4677 has none) and
    `n_slots_metar`. AUTO and `//` are not observed; CAVOK and no group above 5 km are
    "nothing significant".
  - *Rulings:* AUTO METAR codes are not used at all (the 157 AUTO dust reports are implausible:
    `SA` at 9 999 m at Kashi, `DU` at 0 m at Learmonth), so ASOS `HZ` cannot feed the haze rule. No
    group at <= 5 km or with unknown visibility = not observed (an obscuration should have been
    coded). CAVOK = `nil_significant` (explicit); no group > 5 km = new status `nil_metar`
    (implied), excluded from `valid_day_explicit`. A group ISD did not decode = not observed.
    Weather groups are detected broadly (any table-4678 letter pairs; e.g. `-TSBLSA`), so a
    missed group can only give "not observed". Dusty slots with unknown visibility are left out
    of `valid_day_vis5`.
  - Code: `parse.py` (`metar_tokens`, `metar_auto`, `metar_wx`; US messages carry a local date and
    time before `METAR`, found when checking El Paso), `classify.py` (status rules),
    `sql/daily.sql` (`n_slots_nil_metar`, `n_slots_metar`, `dusty_day_vis5`, `n_slots_pw_vis5`,
    `valid_day_vis5`, `vicinity_dust_day`), five new gold checks, settings
    `classify.metar_rules`, `metar_report_types`, `metar_nil_min_vis_m`, `products.vis5_max_m`.
  - Check on real bronze (in memory, nothing written): El Paso 2020 METARs 8 547 `nil_metar`, 230
    reported; Zabol 2020 5 692 CAVOK, 1 676 `nil_metar`; Learmonth 2020 all AUTO METARs not observed.
- **Correction: the US Southwest stations are not METAR-only.** Phoenix, El Paso and Las Vegas
  send SYNOPs at :00 (El Paso 8 a day; Phoenix and Las Vegas 4 a day in 2016 and 2020, 8 in 2024)
  besides METARs at :51. Notebook 03 found no same-minute pairs and called them METAR-only; fixed
  in the notebook text, rules.md, and the notes in `config/stations/contrast.csv`. METAR fills
  about half of Phoenix's 2020 slots.
- **Docs fix:** rules.md 5.2 had been inserted after section 10 (bd8bb79); moved under section 5.

- **Notebook 03: METAR present weather validated** (`notebooks/03_metar_present_weather.ipynb`;
  pilot_100 2015-2024, 3.18 M METAR/SPECI, 680 k paired with a SYNOP that observed the weather).
  Findings (rules.md 5.2): METAR reports dust when visibility <= 5 km (95-96 % of SYNOP dust
  cases) and almost never above (WMO 15.8.12 holds in the data; US reports DU up to 7 mi). A
  non-AUTO METAR with no group at 5 to < 10 km coincides with SYNOP dust in 44 %. At >= 10 km or
  CAVOK (candidate M1): 0.79 % overall but 14.7 % Barbados, 6.6 % Niamey, 2.7 % Aktyubinsk.
  AUTO METARs carry `//` (93 %). US remarks hold dust that ISD drops (2 632 reports).
  Options for the decision (none implemented; method unchanged):
  - A. Keep "no group = not observed". METAR adds dust reports only (dusty_day_any); METAR-only
    stations (US Southwest, Australian AUTO) get no valid days.
  - B. M1 with a station guard (as V3): only where paired SYNOPs show M1 misses dust in < ~1 %;
    METAR-only stations stay "not observed" (no guard possible).
  - C. A second, visibility-limited definition, *dust at <= 5 km*: any non-AUTO METAR without a
    group but with visibility reported is a valid "no such dust" (validated <= 0.6 % at 20 of 21
    stations); SYNOP counts only dust codes with visibility <= 5 km. Comparable across SYNOP and
    METAR and usable at METAR-only stations; the current any-visibility definition stays primary.
  - Separately: decode US remarks dust (`BLDU ALQDS`, `DSNT`, `VCBLDU`) as vicinity/distant flags.
- **Ruling:** in notebook 03, SYNOP iX 3/6 is not truth for the paired METAR (the SYNOP observer did
  not look; the aviation observer may have); ww 00-03 count as nothing significant (FM 12.2.6.2).

- **FMH-1 2005 is the reference edition** (user decision): the 2019 edition's official link is
  broken and no other official copy exists; the 1995 copy was removed as redundant (same rules,
  fe4bb0b). Extract and index now cite 2005 only.

- **Only published editions** (user decision): the FMH-1 2026 draft was removed and replaced by the
  published FCM-H1-1995 and FCM-H1-2005; FMH-2 change 4 removed (a one-page notice). The METAR
  extract now quotes FCM-H1-2005 (5a731d7 had quoted the draft); the rules used are identical in
  2005 and in the draft, and 1995 has the same body/Remarks, obscuration and DS/SS rules (its
  text layer is damaged, so the "7 miles" cannot be machine-read there). The 2005 copy comes from
  a third-party mirror (everyspec.com stamp), recorded as such in references.md.

- **FMH-1 (FCM-H1-2026) and FMH-2 (1988) saved by the user**; added to `docs/references.md`; the
  ICAMS entries left the download script (manual only). US METAR rules extracted to
  `docs/references/metar-weather.md`. Findings:
  - US METAR codes an obscuration (DU, SA, HZ, BLDU, BLSA) in the body only when visibility is
    < 7 statute miles (~11 km; WMO: ≤ 5 000 m); dust seen away from the station goes to Remarks.
    A non-AUTO METAR without a weather group therefore says nothing about light dust at higher
    visibility: it cannot be read as "nothing significant" for dust without validation.
  - `AUTO` = no human involvement; a corrected report carries COR instead of AUTO. AO1/AO2 appear in
    all reports of an automated station, augmented or not, so they do not identify AUTO reports.

- **Official references saved** (1cd4d1d; user decision: local copies of official documents only; extract
  the relevant parts into docs/, keep the full copies git-ignored). Full copies in `references/`,
  re-created by `scripts/fetch_references.py`; index with checksums in `docs/references.md`;
  extracts in `docs/references/` (SYNOP group 7 and iX, BUFR 0-20-003, METAR and ASOS).
  WMO-No. 306 Vol. I.1 (2019) and I.2 (2025) downloaded by the user (library.wmo.int has a human
  check). ICAMS (FMH-1, -2, -12) refused scripted downloads twice (HTTP 403): still to obtain.
  Ruling: WMO material is summarised with short quotes (WMO allows short extracts with the
  source); US federal documents are quoted verbatim (public domain).
- **Resolved: the WMO rule for omitting group 7** (FM 12, Regulation 12.2.6, p. A–15): group 7 is
  included only if weather of significance was observed; ww 00–03 / W 0–2 are not significant; when
  omitted, iX tells "no observation" from "nothing significant". Our iX rule matches; BUFR 508/509
  are the same two cases. Table 4680 wawa 27–29 (snow *or sand*) match `classify.rank_4680`.
- **Finding: METAR rules that limit dust reporting** (WMO-No. 306 FM 15, table 4678):
  DU, SA, HZ, FU only when visibility ≤ 5 000 m (15.8.12), unlike SYNOP ww 06/07; a missing w'w'
  group means nothing to report (15.8.2) but nothing states it explicitly; `//` = not observed
  (15.8.19); CAVOK = no significant weather (15.10). Fits SYNOP reporting dust far more often.
- **Finding: ASOS (AUTO) never reports dust** (ASOS User's Guide 1998, §3.11): only FG, BR, FZFG,
  HZ, and HZ is inferred from visibility < 7 mi and dew-point depression > 4 °F, not observed.
  Consequence for the METAR rule (to implement): AUTO METAR = dust "not observed"; AUTO HZ must
  not feed the haze rule.

- **METAR first** (user decision): handle METAR before running all station-years; the user wants
  more US Southwest stations, which report mostly METAR.
- **Finding: ISD decodes METAR dust reliably.** Phoenix, El Paso, Las Vegas (2015-2024): 761 METARs
  with a dust group (DU, SA, BLDU, BLSA, DRDU, DRSA, PO, DS, SS) in the raw text; 746 (98 %) also
  have dust in ISD's decoded fields (MW/AU/AW). Raw METAR text is kept in REM ("MET…METAR …").
  None of these three stations' METARs carry "AUTO" (staffed or augmented).
- **Finding: SYNOP and METAR at the same time** (pilot_100 silver, 2015-2024): 27 % of SYNOPs
  (908k, 37 stations) have a METAR at the identical timestamp. When both carry a code they agree
  on dust 98.9 %, but SYNOP reports dust far more often (27,487 SYNOP-only vs 2,135 METAR-only vs
  19,772 both); likely METAR coding rules (e.g. DU/HZ only with reduced visibility; to verify).
  When SYNOP says "nothing significant" (iX 2/5), the METAR has no weather group in 99.4 %.
  Current handling: sampling prefers SYNOP; dusty_day_any counts both.
- **Rule V3 implemented** (user decision; method 1.3.0; this commit). `process/guard.py` computes a
  station guard from all bronze years; `classify.py` marks `pw_status = nil_inferred` (SYNOP, no
  code, no iX, visibility reported, guard passes); silver metadata `nil_guard` triggers rebuilds;
  gold adds `n_slots_nil_inferred` and `valid_day_explicit` plus two checks. 174 tests pass.
  pilot_100 2015-2024 rebuilt (940 station-years, 0 failures): guard pass 80 / fail 5 / no
  decision 13.
- **User decision: "if it cannot be verified, it is not observed."** Added to CLAUDE.md.
- **BUFR-only check:** BUFR is a transmission format, not a report type. NOAA decodes weather from
  BUFR as well as from text (codes 28.3 % vs 23.1 %, dust share 15.31 % vs 15.33 %), so V3 applies
  to BUFR-only SYNOPs; stations where formats disagree get no inference. Bachu and Tazhong changed
  reporting practice in 2020 (weather group always included since).
- **Data:** pilot_100 ingested and processed for 2015-2024 (931 downloads, 940 silver station-years,
  0 failures, 5.5 min), plus bronze for 1984-1985, 1995, 2005 (339 files).
- **Finding: iX only exists since ~2015.** Raw SYNOP text with iX: 78-94 % of SYNOPs in 2015-2024,
  0 % in 1985, 1995, 2005.
- **Validation experiment** (user request; `notebooks/02_validate_inferred_nil.ipynb`): hiding iX,
  "visibility reported" separates "nothing significant" (84.7 % have visibility) from "not
  observed" (0.4 %). Best rule V3 (visibility reported + station guard): precision 0.9998, recall
  0.84; dusty-days/yr median error 46.5 → 0. Code-habit rules recovered only ~20 %. Awaiting user
  decision on adopting V3 (`nil_inferred`).
- **Finding: reporting habit is detectable without iX.** Stations that leave out the weather group
  when nothing happens (iX 2 in 72-84 % of iX-bearing SYNOPs: Dammam, Zabol, Nasiriya, La Rioja,
  Catamarca; Nokkundi 53 %) almost never report codes 00-03 (0-7 %); stations that always include
  it (Hotan, Bachu, Tazhong, Good Hope, Shakawe) report 00-03 in 60-92 % of codes.
- **Proposal (awaiting user decision):** three confidence levels for "observed": explicit (code or
  iX 2/5), inferred nil (no code, no iX, station habit "leave out", valid visibility, optionally
  high visibility), not observed. Validate the inference on station-years with iX (hide iX,
  compare) before adopting; publish statistics with and without inferred nil. Also check the
  METAR convention (absent weather group; AUTO, CAVOK, NSW) and keep a visibility-based dust
  index as an independent check.
- Caveat: on the exFAT SSD a glob like `*.parquet` also matches macOS `._*.parquet` files; use
  `[0-9A-Z]*.parquet` in ad-hoc queries (some station IDs start with a letter).

## 2026-10-19

- **Uth insight** (user hypothesis, confirmed on Zabol): the v1 denominator (reports with a weather
  code) makes calm conditions look dusty (30-43 % at 0-6 m/s), hiding the wind threshold; with
  all observed reports the curve is 2-3 % below 8 m/s and ~50 % at 12-14 m/s. Likely cause of the
  v1 Uth failure at Saharan stations; fit Uth on raised dust (07+). Open: is iX available in
  older ISD decades and at African stations?
- Explained the iX effect as a selection bias (docs/rules.md §5): under the old rule a day was
  "valid" only if ≥ 4 slots carried a weather code, and at Zabol codes are mostly dust, so valid
  days were mostly long dust days (357 days, 92 % dusty); 1,460 clear or short-episode days were
  discarded.
- **SYNOP iX rule** (user decision; `c1ae416`, method 1.2.0). The raw SYNOP text in `REM` carries
  iX (WMO code table 1860). iX 2/5 = weather group omitted because nothing significant happened →
  observed, class none; iX 3/6 = not observed; iX 7 = automatic station → its AW codes read with
  WMO table 4680 (differs from ISD's AW list for codes 05 and 07). Confirmed in the official WMO
  Codes Registry (BUFR 0-20-003 entries 508/509; 100-199 = table 4680; 0-20-004 past weather).
  Checked on 363k pilot_20 SYNOP messages. Effect: valid days at most SYNOP stations rose from
  ~20 % to ~100 % of days; inflated dusty shares (Zabol 92 %) fell to plausible values (43 %).
  This refines the 2026-10-17 rule: missing still never means "no significant weather" unless
  iX says so explicitly.
- **Finding:** in NOAA's CSV, some sub-fields are ordered differently from the ISD document
  (OD: speed before direction). Field positions must be checked against real CSV strings.
- **Bug fixed** (`0e96759`): pruning old gold versions failed on the exFAT SSD (macOS deletes the
  hidden `._x` twin with `x`). Reproduced on the SSD, test added, fixed.
- **pilot_20** (`a51fe26`): the 2-3 dustiest stations per region from pilot_100, for end-to-end
  tests. First full run (2020-2024): 103 station-years ingested, 84 silver, gold OK; ~30 s.
  Found: 4 selected stations without usable recent data; 3 with no dust reported recently.
- Default station list is pilot_100; thin_slice.csv removed (user decision; `fdf32c3`).
- Documentation started: `docs/engineering.md`, `docs/science.md`, this log (user request; renamed
  2026-10-28: `code_design.md`, and `rules.md` + `rules_history.md`).

## 2026-10-18

- **Pilot station list** (user decisions; `e4be473` … `b71a99e`). Notebook ranks duISD v1
  stations by mean dust frequency 1991-2019 (all dust codes incl. 06), regions = box + country
  filter, dusty = ≥ 1 %, up to 12 per region at ≥ 50 km → 88 dusty + 12 contrast = `pilot_100.csv`.
  Findings on the way: duISD v1 is SYNOP-only (most North American stations missing); ≥ 30 or
  ≥ 15 valid years left whole regions empty (→ ≥ 10 in 1991-2019); every duISD station has
  `flagDU = True`; `flagDU = False` stations may simply never report weather (Hong Kong
  automatic stations), so clean controls must also report weather (Zhang Ping, Haikou).
- **Naming** (user decision; `e47f9f8`): code and commands named by action (ingest, process,
  aggregate; later enrich, forecast), data by layer (bronze, silver, gold).
- **Packaging** (`b77b6f0` … `b1679e2`): SQL and default config inside the package; user paths
  relative to the working directory; package version in run records; a test builds, installs and
  runs the wheel outside the repo.
- **Code documentation** (`f36ca4f`): module guides, docstrings and comments; verified that only
  comments changed (syntax-tree comparison).

## 2026-10-17

- **Missing weather = not observed** (user decision, "interpretation 2"; `b804f56`). Added dust
  evidence from AT/AX (daily summaries), AY/AZ (past weather) and `dusty_day_any_source`.
- **Final review fixes** (`487b19e`): summary-of-day reports dated to the day they summarise;
  slots counted on the slot's local day; last-year 31-December reports kept; one failing
  station-year no longer stops processing; gold refuses stale silver; missing-longitude sentinels.
- **Data root** (user decision; `4a37f15`): default `./data`; the SSD is a user choice via
  `DCDR_DATA`, not a rule.

## 2026-10-15

- **Design spec** (`a6cc1d9`) and **Plan 1** (`9b6e439`): scaffolding + thin slice.
- **Plan 1 implemented** (`e86de45` … `82f7644`): config, state, ingest, parse, QC, classify,
  haze/RH, sampling, silver build, gold daily, CLI; test-first throughout.
- **Thin slice on real data** (Kuwait, Las Vegas; `516ffaf`). Found: year-boundary slot selected
  in two files (fixed, `8401d24`); at METAR-heavy stations few slots carry weather codes.
- Rulings: QC ranges from the ISD document MIN/MAX; OD gust types 3 and 4; ruff excludes
  `legacy/` and `docs/`.

## 2026-10-14

- Analysis of the original scripts (`legacy/`): many bugs and undocumented choices found.
- **Revamp goals** (user): industry-grade, portfolio-quality pipeline (DS > MLE > DE) that also
  produces a citable dataset; $0 platforms; laptop compute; ISD read from AWS S3; publish and
  visualise; later environmental drivers and ML forecasting at dust source stations.
- Stack chosen: Python 3.12, uv, Polars, DuckDB, Parquet, Pydantic, Pandera, Typer, pytest, ruff,
  mypy, GitHub Actions. Cloud research-credit applications drafted (Google, AWS).
