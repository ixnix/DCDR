# DCDR work log

A dated record of what was done, what was found and what was decided, newest first.
"User decision" = decided by Xin Xi; "Ruling" = a choice made by Claude during implementation,
open to review. Details of the current method: [science.md](science.md); of the software:
[engineering.md](engineering.md). Commits are referenced by their short hash.

## Open items

- [ ] **Decisions after the full-record test** (notebook 04): (a) METARs without text before
      2000; (b) visibility under SYNOP-first weather values; (c) keep or change the dust and
      weather sources (still open by user decision).
- [ ] NOAA's S3 ISD files end on 2025-08-27 (no 2026): check whether ISD is still updated or
      replaced (e.g. by another NCEI hourly product) before relying on recent years.
- [ ] **Run the pipeline for method 1.6.0** (user): `dcdr process` rebuilds all silver (new method
      version), then `dcdr aggregate`; per station compare `dusty_day`, `dusty_day_synop`,
      `dusty_day_metar` and `dusty_day_vis5`, and `n_slots_pw_synop` against `n_slots_pw`.
- [ ] Decode dust in US remarks (`BLDU ALQDS`, `DSNT`, `VCBLDU`) as a "nearby/distant" flag.
- [ ] Niamey: why its METARs miss dust at <= 5 km in 5.8 % of SYNOP dust cases.
- [ ] FMH-1: replace the 2005 mirror copy (everyspec.com) with an ICAMS/NOAA copy if one appears.
- [ ] Station selection: add a recent-data criterion (data and weather reports in ≥ 3 of the last
      5 years); replace Wadi Halfa, Abu Hamed, Papunya, Dongola, Rabbit Flat in pilot lists.
- [ ] Investigate stations with no dust in 2020-2024 despite high duISD ranks (Ashchysai, Shakawe,
      Good Hope): change of observing practice or real decline?
- [ ] BUFR-only SYNOP (17 %, no iX): any source for the "omitted" information?
- [ ] Check visibility reporting in older decades (V3 assumes it is stable).
- [ ] Review humidity screen at humid dust sites and the `has_pw` sampling tie-break.
- [ ] Plan 2: monthly/climatology gold products, station catalog, coverage, Uth, code-06 diagnostic.
- [ ] Set up a GitHub remote; publish documentation (e.g. MkDocs) when stable.

---

## 2026-10-20

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
  station-days, all checks passed). Findings (science.md 8.1):
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
  f0b5bab; user decision). In 1.5.0 the primary report (wind, temperature, humidity, pressure,
  visibility statistics) was the observed SYNOP, else the METAR, so an unobserved SYNOP gave way
  to the METAR and a station's series could switch report types from slot to slot. Now the
  primary report is the slot's SYNOP, observed or not, and the METAR only in slots without SYNOP.
  Dust is unchanged (either source). *Ruling:* a value missing from that SYNOP stays missing (no
  per-variable filling from the METAR). Tests: 2 new, 226 pass.

- **SYNOP and METAR judged independently per slot** (method **1.5.0**, follows 328eb4d;
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
  for every report type: science.md 5.3. Decisions:
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
  in the notebook text, science.md, and the notes in `config/stations/contrast.csv`. METAR fills
  about half of Phoenix's 2020 slots.
- **Docs fix:** science.md 5.2 had been inserted after section 10 (6610445); moved under section 5.

- **Notebook 03: METAR present weather validated** (`notebooks/03_metar_present_weather.ipynb`;
  pilot_100 2015-2024, 3.18 M METAR/SPECI, 680 k paired with a SYNOP that observed the weather).
  Findings (science.md 5.2): METAR reports dust when visibility <= 5 km (95-96 % of SYNOP dust
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
  0f97f59). Extract and index now cite 2005 only.

- **Only published editions** (user decision): the FMH-1 2026 draft was removed and replaced by the
  published FCM-H1-1995 and FCM-H1-2005; FMH-2 change 4 removed (a one-page notice). The METAR
  extract now quotes FCM-H1-2005 (6c60d7b had quoted the draft); the rules used are identical in
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

- **Official references saved** (0416b0c; user decision: local copies of official documents only; extract
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
  `[0-9]*.parquet` in ad-hoc queries.

## 2026-10-19

- **Uth insight** (user hypothesis, confirmed on Zabol): the v1 denominator (reports with a weather
  code) makes calm conditions look dusty (30-43 % at 0-6 m/s), hiding the wind threshold; with
  all observed reports the curve is 2-3 % below 8 m/s and ~50 % at 12-14 m/s. Likely cause of the
  v1 Uth failure at Saharan stations; fit Uth on raised dust (07+). Open: is iX available in
  older ISD decades and at African stations?
- Explained the iX effect as a selection bias (docs/science.md §5): under the old rule a day was
  "valid" only if ≥ 4 slots carried a weather code, and at Zabol codes are mostly dust, so valid
  days were mostly long dust days (357 days, 92 % dusty); 1,460 clear or short-episode days were
  discarded.
- **SYNOP iX rule** (user decision; `f9bc8b5`, method 1.2.0). The raw SYNOP text in `REM` carries
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
- **Bug fixed** (`a1181d2`): pruning old gold versions failed on the exFAT SSD (macOS deletes the
  hidden `._x` twin with `x`). Reproduced on the SSD, test added, fixed.
- **pilot_20** (`fe76716`): the 2-3 dustiest stations per region from pilot_100, for end-to-end
  tests. First full run (2020-2024): 103 station-years ingested, 84 silver, gold OK; ~30 s.
  Found: 4 selected stations without usable recent data; 3 with no dust reported recently.
- Default station list is pilot_100; thin_slice.csv removed (user decision; `cb309d7`).
- Documentation started: `docs/engineering.md`, `docs/science.md`, this log (user request).

## 2026-10-18

- **Pilot station list** (user decisions; `5819f3f` … `e343119`). Notebook ranks duISD v1
  stations by mean dust frequency 1991-2019 (all dust codes incl. 06), regions = box + country
  filter, dusty = ≥ 1 %, up to 12 per region at ≥ 50 km → 88 dusty + 12 contrast = `pilot_100.csv`.
  Findings on the way: duISD v1 is SYNOP-only (most North American stations missing); ≥ 30 or
  ≥ 15 valid years left whole regions empty (→ ≥ 10 in 1991-2019); every duISD station has
  `flagDU = True`; `flagDU = False` stations may simply never report weather (Hong Kong
  automatic stations), so clean controls must also report weather (Zhang Ping, Haikou).
- **Naming** (user decision; `8436044`): code and commands named by action (ingest, process,
  aggregate; later enrich, forecast), data by layer (bronze, silver, gold).
- **Packaging** (`b277a82` … `a25cf34`): SQL and default config inside the package; user paths
  relative to the working directory; package version in run records; a test builds, installs and
  runs the wheel outside the repo.
- **Code documentation** (`b960f02`): module guides, docstrings and comments; verified that only
  comments changed (syntax-tree comparison).

## 2026-10-17

- **Missing weather = not observed** (user decision, "interpretation 2"; `caea9ed`). Added dust
  evidence from AT/AX (daily summaries), AY/AZ (past weather) and `dusty_day_any_source`.
- **Final review fixes** (`9cc5d21`): summary-of-day reports dated to the day they summarise;
  slots counted on the slot's local day; last-year 31-December reports kept; one failing
  station-year no longer stops processing; gold refuses stale silver; missing-longitude sentinels.
- **Data root** (user decision; `37fb9b7`): default `./data`; the SSD is a user choice via
  `DCDR_DATA`, not a rule.

## 2026-10-15

- **Design spec** (`c606c95`) and **Plan 1** (`ee1ed59`): scaffolding + thin slice.
- **Plan 1 implemented** (`7f19ec1` … `5ed6049`): config, state, ingest, parse, QC, classify,
  haze/RH, sampling, silver build, gold daily, CLI; test-first throughout.
- **Thin slice on real data** (Kuwait, Las Vegas; `7085878`). Found: year-boundary slot selected
  in two files (fixed, `e0cc920`); at METAR-heavy stations few slots carry weather codes.
- Rulings: QC ranges from the ISD document MIN/MAX; OD gust types 3 and 4; ruff excludes
  `legacy/` and `docs/`.

## 2026-10-14

- Analysis of the original scripts (`legacy/`): many bugs and undocumented choices found.
- **Revamp goals** (user): industry-grade, portfolio-quality pipeline (DS > MLE > DE) that also
  produces a citable dataset; $0 platforms; laptop compute; ISD read from AWS S3; publish and
  visualise; later environmental drivers and ML forecasting at dust source stations.
- Stack chosen: Python 3.12, uv, Polars, DuckDB, Parquet, Pydantic, Pandera, Typer, pytest, ruff,
  mypy, GitHub Actions. Cloud research-credit applications drafted (Google, AWS).
