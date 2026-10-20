# DCDR documentation, Part 2: Science and method

How DCDR interprets NOAA ISD station reports, converts them, decides what was observed, classifies
dust, samples reports and aggregates them into daily records, and **why** each choice was made.
Part 1 ([engineering.md](engineering.md)) covers the software; [worklog.md](worklog.md) records
how and when each decision was made.

> Living document. It describes the method **as implemented now** (method version **1.2.0**).
> Planned steps are marked *planned*. Last updated: 2026-10-19.

Authority for every field and code: the NOAA ISD format document (`legacy/isd-format-document.pdf`)
and the WMO *Manual on Codes* (WMO-No. 306) as published in the WMO Codes Registry
(codes.wmo.int). Where the two differ, the source of the code decides (section 6.3).

---

## 1. Goal and principles

Goal: a homogeneous, traceable record of dust events at weather stations worldwide, suitable for
climatology, trends and (later) forecasting.

Principles, in order of importance:
1. **Never assume an observation.** A missing value means "not observed" unless the report says
   explicitly that nothing significant happened (section 5).
2. **Interpret each code with its own table.** The same number means different things in
   different WMO tables (e.g. 30-35: duststorm in table 4677, fog in table 4680).
3. **Flag, don't delete.** Every report is kept with flags explaining what was rejected and why.
4. **Make stations comparable.** Count dust in the same 3-hourly synoptic reports everywhere, so
   reporting frequency does not change the result (section 8).
5. **Every choice is a setting.** Thresholds and rules are configuration, recorded with each run.

## 2. Source data: NOAA ISD

ISD merges surface weather reports from many networks. NOAA publishes one CSV per station per
year (`noaa-global-hourly-pds` on AWS S3). Each row is one report; each report has a
**report type**, e.g.:

| Type | Report | Typical station |
|---|---|---|
| `FM-12` | SYNOP: international synoptic report, every 3 h (sometimes hourly) | national weather services worldwide |
| `FM-15` | METAR: routine aviation report, hourly or half-hourly | airports |
| `FM-16` | SPECI: special aviation report when weather changes | airports |
| `SY-MT`, `SY-SA`, ... | SYNOP merged with another report by NOAA | |
| `SOD`, `SOM` | summary of day / month | mainly US stations |

Observations are stored as **packed text groups**: one column per group, parts separated by
commas, e.g. `WND = "340,1,N,0060,1"` (direction 340°, QC 1, type N, speed 6.0 m/s, QC 1).

Two properties of the CSV matter:
- **Sub-field order can differ from the ISD document.** The document lists OD (supplementary
  wind) as type, period, direction, speed, QC; the CSV has type, period, **speed**, QC,
  **direction** (`4,99,0149,1,310`). Field positions are therefore checked against real CSV
  strings, not only against the document.
- **`REM` keeps the original message text**: `SYN…` for SYNOP, `MET…` for METAR, or
  `SYN004BUFR` when NOAA received the report only in binary BUFR form (no text).

Example, Zabol (Iran) 2023: 2,840 SYNOP (97 % of 3-hourly slots), 8,226 METAR, 11 SPECI.

## 3. Ingest (bronze)

The whole CSV is stored losslessly, every column as text. No interpretation happens here, so any
later change in interpretation can be re-run from bronze without downloading again.

## 4. Decoding and time (`parse.py`)

### 4.1 Decoded fields

| ISD group | Parts used (CSV order) | Result | Missing code | Scale |
|---|---|---|---|---|
| `WND` | dir, dir QC, type, speed, speed QC | `wind_dir` (°), `wsd` (m/s) | 999 / 9999 | speed ÷10 |
| `VIS` | distance, QC | `vis_raw` (m) | 999999 | |
| `TMP`, `DEW` | value, QC | `t`, `td` (°C) | +9999 | ÷10 |
| `SLP` | value, QC | `slp` (hPa) | 99999 | ÷10 |
| `OC1` | speed, QC | gust candidate (m/s) | 9999 | ÷10 |
| `OD1-3` | type, period, speed, QC (, direction) | gust candidate if type 3 or 4 | 9999 | ÷10 |
| `MW1-7` | code, QC | manual present weather (WMO 4677) | | |
| `AW1-4` | code, QC | automated present weather | | |
| `AU1-9` | intensity, descriptor, precipitation, obscuration, other, combination, QC | METAR-style present weather | | |
| `MV1-7` | code, QC | weather in the vicinity | | |
| `AY1-2` | code, QC, period | manual past weather (WMO 4561) | period 99 | |
| `AZ1-2` | code, QC, period | automated past weather (WMO 4531) | period 99 | |
| `AT1-8` | source, weather type, abbreviation, QC | daily present weather (summary of day) | | |
| `AX1-6` | code, QC, period | daily past weather | | |
| `AA1-4`, `AJ1` | precipitation, snow depth | kept, not yet used (low reliability) | | |
| `REM` | raw SYNOP header | `syn_ix` (section 5) | | |

Text that is not a number and not a missing code is recorded in `parse_status` (bit mask:
WND 1, VIS 2, TMP 4, DEW 8, SLP 16) and becomes null.

Exact duplicate rows (identical in every source column) are collapsed (`dup_count`); every report
gets a content fingerprint `row_key` (SHA-1) so results never depend on file row order.

### 4.2 Local mean solar time (LMST)
`time_lmst = time_utc + longitude / 15 h` (4 min per degree), using each report's longitude
(station-year median if missing; coordinates outside ±90/±180 are treated as missing).

*Why:* dust has a strong daily cycle (afternoon winds), so a "day" should be the local day.
Solar time depends only on longitude: the same rule everywhere, unlike time zones and daylight
saving. The difference from apparent solar time (±16 min) does not matter at 3-hourly resolution.

Special cases:
- A summary-of-day (SOD) report is stamped 23:59 local *standard* time, which can fall after
  LMST midnight; it is dated to the day it summarises: `date(time_lmst − 12 h)`.
- A synoptic slot is counted on the local day of the **slot**, not of the report that fills it
  (a report up to 30 min early can sit across midnight).

## 5. Was the weather observed? (the SYNOP iX rule)

This decides which reports can say anything about dust, and therefore which days are "valid".

**Problem.** A SYNOP report includes the weather group `7wwW1W2` (ww = present weather, W1W2 =
past weather) only when there is something to report or the station chooses to. A missing
weather group can mean "nothing significant happened" or "not observed".

**Rule (user decisions 2026-10-17 and 2026-10-19).** A missing weather field is **never assumed**
to mean "no significant weather". It means "not observed", **unless the report says so
explicitly**. SYNOP says so in the indicator **iX** (group `iR iX h VV`, the first group after the
station number), WMO code table 1860:

| iX | Station | Weather group | DCDR treatment (`pw_status`) |
|---|---|---|---|
| 1 | manned | included | classify the code (`reported`) |
| **2** | manned | **omitted: no significant phenomenon to report** | **observed, no dust** (`nil_significant`) |
| **3** | manned | **omitted: no observation, data not available** | **not observed** (`not_observed`) |
| 4 | automatic | included, tables 4677/4561 | classify the code |
| **5** | automatic | **omitted: no significant phenomenon** | **observed, no dust** |
| **6** | automatic | **omitted: no observation** | **not observed** |
| 7 | automatic | included, tables **4680/4531** | classify with table 4680 (section 6.3) |
| none | METAR, BUFR-only SYNOP, no text | | not observed when no weather field |

Official confirmation: WMO Codes Registry, BUFR table 0-20-003: entry 508 "No significant
phenomenon to report, present and past weather omitted" (= iX 2/5) and 509 "No observation, data
not available, present and past weather omitted" (= iX 3/6).

`syn_ix` is read from `REM`: `SYN` + 3-digit length + 5-digit WMO number + space + `iR iX h VV`.
Example: `SYN08040829 12960 03406 …` → station 40829 (Zabol), iR 1, **iX 2**.

Silver columns: `has_pw` (true if `reported` or `nil_significant`), `pw_status`, `syn_ix`.

**Evidence** (pilot_20 bronze, 363k SYNOP messages):
- iX 1/4 → manual weather code (MW) present in 99-100 % of reports; iX 2/3/5/6 → present in
  ≤ 0.2 %. ISD files the codes consistently with iX.
- Effect on Zabol 2020-2024: share of synoptic slots with weather observed rose from 0.21 to 0.98;
  valid days from 357 to 1,802; dusty share of valid days fell from an implausible 92 % to 43 %
  (156 dusty days a year). Chinese stations, which always include the weather group, were
  unchanged.
- *Why the old rule inflated dust (a selection bias).* Of Zabol's ~14,400 synoptic slots, only
  3,052 (21 %) carried a weather code; 11,096 (77 %) said "nothing significant" (iX 2). A code is
  reported mostly when something happens, and at Zabol that is mostly dust (~88 % of reported
  codes). Requiring ≥ 4 slots *with a code* therefore kept only days with long-lasting dust:

  | Old rule | Days | Dusty | % dusty |
  |---|---|---|---|
  | "valid" (≥ 4 slots with a code) | 357 | 330 | 92 % |
  | discarded | 1,460 | 441 | 30 % |

  The discarded days were ~1,000 explicitly clear days and 441 shorter dust episodes. With iX,
  clear slots count as observed and nearly every day is valid: 771 dusty of ~1,800 days. Stations
  that always report a code (China) were never filtered this way, so the old rule also made
  countries with different reporting habits incomparable.

**Limitations.**
- 17 % of SYNOP messages reached NOAA only as BUFR (`SYN004BUFR`): no text, no iX; they fall
  back to "not observed" when no code is present (e.g. Nokkundi, Pakistan).
- METAR has no iX: an absent weather group in METAR stays "not observed" (explicit `CAVOK` or
  `NSW` could be used later).
- Open: quote the exact WMO-No. 306 regulation on when group 7 may be omitted (expected: present
  weather 00-03 and past weather 0-2, i.e. haze and all dust codes must be reported).

## 6. Dust classification (`classify.py`)

### 6.1 Classes and severity
Each code is mapped to a class with a severity rank. When a report has several codes, the most
severe wins.

| Rank | Class | Meaning |
|---|---|---|
| 0 | `none` | no dust |
| 1 | `haze` | haze (could be smoke or pollution) |
| 2 | `haze_or_suspended` | automated: haze, smoke **or** dust in suspension (ambiguous) |
| 3 | `suspended` | dust in suspension, not raised at the station (often transported) |
| 4 | `blowing` | dust or sand raised by wind at the station |
| 5 | `whirls` | dust or sand whirls |
| 6 | `storm` | duststorm or sandstorm |
| 7 | `storm_severe` | severe duststorm or sandstorm |
| 8 | `storm_thunder` | thunderstorm with duststorm or sandstorm |

Special classes: `unknown` (not observed), `dust_haze` (haze after dust, 6.5), `screened` (humid,
6.6).

**Dusty classes**: suspended, blowing, whirls, storm, storm_severe, storm_thunder, dust_haze.
Code 06 (suspended dust) counts as dust (user decision; it is about 60 % of all duISD v1 dust
reports, so it is kept visible in separate counts).

### 6.2 Code tables

**MW, manual present weather (WMO 4677)**: 05 haze; 06 suspended; 07 blowing; 08 whirls;
09 storm (duststorm within sight or in the past hour); 30-32 storm; 33-35 storm_severe;
98 storm_thunder; everything else none.

**AU, METAR-style present weather** (components, ISD document): other weather 4 (SS) or 5 (DS) →
storm (severe if intensity 3 "+", thunder if descriptor 7 "TS"); other 1 (PO) → whirls;
obscuration 5/6 (DU/SA) with descriptor 4/5 (DR/BL) → blowing; DU/SA alone → suspended;
obscuration 7 (HZ) → haze; intensity 4 (VC, in the vicinity) → not counted (evidence only).

**MV, weather in the vicinity**: 03 sandstorm, 04 whirls, 05 duststorm, 07 blowing sand,
08 blowing dust in the vicinity → evidence only (`flag_vicinity`), not station dust.

**Past weather** (evidence only, `flag_past`): AY (WMO 4561) code 3 "sandstorm, duststorm or blowing
snow"; AZ (WMO 4531) code 2 "blowing phenomena, visibility reduced"; both counted as dust only
if resolved as sand (6.4).

**Daily summaries** (evidence only, `flag_daily_dust`): AT 07 and AX 07 (dust, blowing dust/sand).

### 6.3 Automated present weather: the code table depends on the source
ISD's AW field holds automated present weather from two sources, with different tables:

| Source | Table | 04 | 05 | 07 | 27-29 | 30-35 |
|---|---|---|---|---|---|---|
| ISD's own automated reports | ISD document AW list | haze_or_suspended | smoke → none | blowing | snow or sand | fog → none |
| SYNOP with **iX = 7** | **WMO 4680** | haze_or_suspended | **haze/smoke/dust < 1 km → haze_or_suspended** | **not defined → none** | snow or sand | fog → none |

Evidence: for iX = 7 (e.g. Rabbit Flat, Australia) ISD stores the present weather in AW and past
weather in AZ (raw `7//21` → `AW1 = 21`), so ISD already routes codes to the field of the right
table family; only the AW code meanings differ. WMO 4680 checked in the WMO Codes Registry (BUFR
0-20-003, values 100-199).

*Why it matters:* in table 4677, 30-35 are duststorms; in table 4680 they are fog. Reading an
automated code with the manual table would turn fog into duststorms.

### 6.4 Snow or sand?
Some codes mean "blowing snow **or** sand" (AW 27-29, AY 3, AZ 2). Sand if the temperature is
reliably ≥ 3 °C and no snow is reported in the same report (MW 70-79 or AU precipitation SN);
snow if ≤ 0 °C; otherwise ambiguous (`flag_ambiguous`, not dust). Settings
`classify.sand_min_t_c`, `classify.snow_max_t_c`.

### 6.5 Haze rule
Haze alone is not dust (smoke, pollution). A haze or haze_or_suspended report becomes
`dust_haze` (dusty) if the same station reported dust (rank ≥ suspended) within the previous 24 h,
window (t − 24 h, t]. Setting `classify.haze_window_h` (0 switches it off).
*Why:* haze after a dust event is usually the dust settling or lingering.

### 6.6 Humidity screen
If relative humidity > 90 %, the classes suspended, haze, haze_or_suspended and dust_haze become
`screened` (not dusty). Blowing dust and storms are never screened.
*Why:* in very humid air, haze-like reports are more likely humidity haze or mist.
*To review:* many reports are screened at humid coastal or monsoon dust sites (pilot_20:
Ciudad Constitución 375, Jaisalmer 171 in 2020-2024).

## 7. Quality control (`qc.py`)

- NOAA's quality code per value → `ok` (0, 1, 4, 5, 9 and validator letters A, C, I, M, P, R, U),
  `suspect` (2, 6), `erroneous` (3, 7); unknown codes → suspect.
- Physically possible ranges from the ISD document (e.g. wind speed 0-90 m/s, temperature
  −93.2 to 61.8 °C) → outside = erroneous.
- Dew point more than 0.5 °C above temperature → dew point erroneous.
- Only `ok` values are used (`qc.accept`); weather codes need an accepted QC code too.
- Relative humidity from the Magnus formula (Alduchov and Eskridge 1996).
- Visibility capped at 10 km (stations report "unlimited" differently).
- Gust = strongest valid of OC1 and OD types 3 and 4.

*Why flags, not deletion:* rejected data stay countable and the policy is a setting.

## 8. Synoptic sampling (`sampling.py`)

*Why:* stations report at very different rates (SYNOP every 3 h; airports every 30-60 min plus
special reports when weather changes, often exactly when dust arrives). Counting dust in all
reports would make busy airports look dustier. So, as in duISD v1, one report is chosen per
3-hourly slot (00, 03, ..., 21 UTC) and the primary dusty-day flag uses only those.

Rules:
- Candidates: reports within ±30 min of the slot, excluding summaries (SOD, SOM).
- Ranking: report-type priority (FM-12 SYNOP first, then merged SYNOP types, METAR, older and
  automated formats, SPECI last) → weather observed (`has_pw`) → closest in time → fixed
  tie-breakers. Rank 1 is selected; every candidate keeps its rank and a `selection_reason`.

Example: Zabol 2023 has 8,226 METAR and 2,840 SYNOP; nearly all slots are filled by SYNOP.

*To review:* `has_pw` ranks before time distance. At METAR-only stations this can prefer a later
report that mentions weather (e.g. dust) over a closer one without, possibly raising dust
frequency.

## 9. Daily aggregation (gold `daily`, `sql/daily.sql`)

One row per station per local (LMST) day.

| Column(s) | Definition | Why |
|---|---|---|
| `n_slots`, `n_slots_pw` | selected slots on the day; of those, slots with weather observed | basis of validity |
| `valid_day` | `n_slots_pw ≥ 4` (of 8) | a day can only be judged dusty or not if enough was observed |
| `dusty_day_synoptic` | any selected slot dusty (**primary definition**) | comparable across stations and decades |
| `dusty_day_any` | any report of the day dusty (all report types) | more sensitive; depends on reporting rate |
| `past_weather_dust`, `daily_summary_dust` | past-weather or daily-summary dust evidence | evidence from other fields |
| `dusty_day_any_source` | `dusty_day_any` or either evidence flag | most sensitive |
| `n_dust_slots_<class>`, `n_slots_rh_screened` | slots per class | keeps code 06 etc. visible |
| `dust_first_lmst_hour` | first dusty slot of the day | diurnal timing |
| `wsd_mean`, `wsd_max`, `gust_max` | wind (gust from all reports) | driver |
| `vis_min`, `vis_hmean`, `n_vis_zero` | minimum and harmonic-mean visibility | harmonic mean averages extinction (≈ 1/visibility) |
| `t_*`, `td_mean`, `rh_mean`, `slp_mean` | means and extremes at selected slots | drivers |
| `dup6_mean` | dust uplift potential U³(1 + 6/U)(1 − 36/U²) for U > 6 m/s, else 0 | wind's capacity to lift dust (Marsham et al. 2011 form; fixed 6 m/s threshold) |
| `d_t_mean`, `d_slp_mean` | change from the previous day, only between two consecutive valid days | fronts often bring dust |

Analyses of dust frequency use **valid days only** (e.g. dusty days per year =
365 × dusty valid days / valid days). A day known only from a daily summary has no row.

*Planned:* monthly and annual products, coverage, station catalog, climatology, diurnal cycle,
trends, wind threshold Uth (source stations only), code-06 diagnostic.

**Implication for Uth (threshold wind).** Uth is estimated from how the dusty fraction rises with
wind speed, so the denominator must include clear-weather reports. With only reports carrying a
weather code (duISD v1's denominator), Zabol 2020-2024 looks 30-43 % dusty even at 0-6 m/s and
no threshold appears; with all observed reports (iX rule) it is 2-3 % below 8 m/s, about 50 % at
12-14 m/s and 95 % above 16 m/s, a clear threshold curve. This likely explains why the v1 Uth
method failed at Saharan stations. Fit Uth on **raised dust** (codes 07 and up): suspended dust
(06) is often transported and reported at low local winds (e.g. Harmattan, Sudan).

## 10. Station selection (pilot)

Method (`notebooks/01_duisd_v1_dusty_stations.ipynb`):
1. Ranking data: duISD v1 (SYNOP reports only, monthly counts, 1940-2019), used **only to rank**.
2. Valid month ≥ 30 SYNOP weather reports; valid year ≥ 6 such months; station needs ≥ 10 valid
   years in **1991-2019** (stricter thresholds left whole regions empty; sensitivity table in the
   notebook).
3. Score: mean yearly dust frequency (% of reports with any dust code, including 06).
4. Regions (`config/stations/regions.yaml`): lat/lon box + country filter where convention needs
   it (China and Mongolia = East Asia; the five Central Asian republics + Afghanistan = Central
   Asia; Pakistan, India, Bangladesh, Nepal = South Asia; Sudan = North Africa).
5. Dusty station: score ≥ 1 % (roughly ≥ 14 dusty days a year); up to 12 per region, ≥ 50 km
   apart → 88 stations.
6. Contrast stations (12, hand-picked): downwind of the Sahara, Asian and Australian sources;
   US automated METAR stations; clean controls (Shannon; Zhang Ping and Haikou in southern China,
   which never reported dust **and** do report weather). `flagDU = False` alone is not enough:
   stations that never report weather have it too.
7. `pilot_20`: the 2-3 dustiest per region (88 → 20) for end-to-end tests.

Known issue (pilot_20 test, 2026-10-19): Wadi Halfa, Abu Hamed and Papunya have no ISD data in
2020-2024; Dongola has no weather codes; Rabbit Flat (automatic) rarely reports weather; Ashchysai,
Shakawe and Good Hope report no dust in 2020-2024. A recent-data criterion is being considered.

## 11. Open questions

1. Exact WMO regulation for omitting group 7 (WMO-No. 306, FM 12) to be quoted.
2. BUFR-only SYNOP (17 %): recover "omitted" information from the BUFR original?
3. METAR: use `CAVOK`/`NSW` as explicit "no significant weather"?
4. Humidity screen at humid dust sites (6.6); `has_pw` before time distance in sampling (8).
5. Station selection: require recent data and weather reporting.
6. Does ISD keep the raw SYNOP text (and so iX) in older decades and at African stations?
   If not, older years keep the biased denominator.
7. When iX is missing: infer "nothing significant" from the station's reporting habit (stations
   that leave out the group almost never report codes 00-03), a valid visibility and possibly
   high visibility; validate against station-years that have iX before adopting (proposal
   2026-10-20, see worklog).

## 12. References

- NOAA NCEI. *Federal Climate Complex Data Documentation for Integrated Surface Data (ISD)*
  (ISD format document), `legacy/isd-format-document.pdf`.
- WMO (2019). *Manual on Codes*, WMO-No. 306, Vol. I.1, Part A; code tables 1860, 4677, 4680,
  4561, 4531. WMO Codes Registry: https://codes.wmo.int (BUFR tables 0-20-003, 0-20-004).
- Alduchov, O. A., and R. E. Eskridge (1996). Improved Magnus form approximation of saturation
  vapor pressure. *J. Appl. Meteor.*, 35, 601-609.
- Marsham, J. H., et al. (2011). The importance of the representation of deep convection for
  modeled dust-generating winds over West Africa during summer. *Geophys. Res. Lett.*, 38, L16803.
- Xi, X. (2021). [title to be filled in by the author]. *J. Geophys. Res. Atmos.*,
  doi:10.1029/2021JD034687. Data: duISD v1, Zenodo 10.5281/zenodo.4107346.
