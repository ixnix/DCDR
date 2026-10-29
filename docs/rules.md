# DCDR documentation, Part 2: Rules

The rules DCDR applies **now** to NOAA ISD station reports: how they are decoded, how DCDR decides
what was observed, how dust is classified, how reports are sampled and aggregated into daily
records, and briefly why. Part 1 ([code_design.md](code_design.md)) covers the software;
[rules_history.md](rules_history.md) records how each rule came about, with the evidence, the
superseded rules and the decisions; [worklog.md](worklog.md) is the dated log.

> Describes the method **as implemented** (method version **1.10.0**). Planned steps are marked
> *planned*. Last updated: 2026-10-29.

Authority for every field and code: the NOAA ISD format document (`legacy/isd-format-document.pdf`,
local) and the WMO *Manual on Codes* (WMO-No. 306) as published in the WMO Codes Registry
(codes.wmo.int); extracts in [references/](references/) (index: [references.md](references.md)).
Where the two differ, the source of the code decides (section 6.3).

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
  `SYN004BUFR` when NOAA received the report only in binary BUFR form (no text). METAR text comes
  in three forms: full messages since about 2000 (in the 2000s often without the word `METAR`),
  and before 2000 only the fragments ISD did not decode (e.g. `CAVOK NOSIG`).

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
| `REM` | raw SYNOP / METAR text | `syn_ix` (5); `metar_text`, `metar_auto`, `metar_wx` (5.2) | | |

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

## 5. Was the weather observed?

This decides which reports can say anything about dust, and therefore which days are "valid".
**Principle (user decisions 2026-10-17 to 2026-10-20): a missing weather field never means "no
significant weather". It means "not observed" unless the report says so explicitly, or a
validated, guarded inference says so; what cannot be verified is "not observed".**

`pw_status` per report: `reported` (a usable weather code), `nil_significant` (explicitly nothing
significant: SYNOP iX 2/5, METAR `CAVOK`), `nil_inferred` (inferred: rule V3, METAR inference),
`nil_metar` (implied: METAR without weather group above 5 km), `not_observed`. `has_pw` is true
for all but `not_observed`.

**SYNOP: the iX rule.** A SYNOP includes the weather group `7wwW1W2` only if weather of
significance was observed; when it is omitted, the indicator **iX** (group `iR iX h VV`, WMO code
table 1860) says why (FM 12 Regulation 12.2.6; extract:
[references/synop-weather-group.md](references/synop-weather-group.md)):

| iX | Station | Weather group | DCDR treatment (`pw_status`) |
|---|---|---|---|
| 1 | manned | included | classify the code (`reported`) |
| **2** | manned | **omitted: no significant phenomenon to report** | **observed, no dust** (`nil_significant`) |
| **3** | manned | **omitted: no observation, data not available** | **not observed** (`not_observed`) |
| 4 | automatic | included, tables 4677/4561 | classify the code |
| **5** | automatic | **omitted: no significant phenomenon** | **observed, no dust** |
| **6** | automatic | **omitted: no observation** | **not observed** |
| 7 | automatic | included, tables **4680/4531** | classify with table 4680 (section 6.3) |
| none (SYNOP) | BUFR-only SYNOP, older years | | **inferred nil** if visibility is reported and the station passes the guard (5.1); else not observed |
| none (other) | METAR, SPECI | | own rules (5.2, 5.3) |

`syn_ix` is read from `REM`: `SYN` + 3-digit length + 5-digit WMO number + space + `iR iX h VV`
(e.g. `SYN08040829 12960 …` → station 40829, iX 2). BUFR 0-20-003 entries 508/509 are the same two
cases. In ISD, iX exists mainly from 2013 on. Evidence and the selection bias this rule removes:
[rules_history.md](rules_history.md), method 1.2.0.

### 5.1 SYNOP without iX: rule V3

- **Rule:** a SYNOP (FM-12) with no weather code and no iX is "nothing significant, inferred"
  (`nil_inferred`) if its visibility is reported (good QC) and its station passes the **V3 guard**.
  No condition on the visibility value. Applies to all years (user decision 2026-10-27).
- **Guard** (`process/guard.py`): from all the station's bronze years, the SYNOPs with no code,
  visibility reported and iX 2/3/5/6; pass if >= 50 such reports and the iX 3/6 share is < 2 %;
  below 50, no inference. Stored in the silver metadata (`nil_guard`); a change rebuilds silver.
- **Why:** in SYNOP, visibility and iX share one group, so a reported visibility shows an
  observation was made. Validated on 2015-2024 (precision 0.9998, recall 0.84); indirect tests
  across decades and the caveats: [rules_history.md](rules_history.md), methods 1.3.0 and the
  V3-across-decades entry.
- Settings: `classify.infer_nil`, `infer_nil_report_types` ([FM-12]), `nil_guard_max_share` (0.02),
  `nil_guard_min_reports` (50). Gold keeps inferred slots visible (`n_slots_nil_inferred`,
  `valid_day_explicit`).
- **BUFR-only SYNOPs** (`SYN004BUFR`, 17 % of SYNOPs) have no iX: the BUFR original says
  "nothing significant" (508) or "not observed" (509), but ISD drops both. They follow the rows
  above (a code is used; without one, V3 or not observed). ISD decodes their codes as well as text
  SYNOPs (same dust share); no other source is pursued (user decision 2026-10-29).

### 5.2 METAR and SPECI

**Role (user decisions 2026-10-20; methods 1.5.0 and 1.6.0).** SYNOP and METAR are judged
independently in each 3-hourly slot: the slot is **dusty if either reports dust**, and observed if
either observed the weather. When the SYNOP observed the weather it is the primary input and the
METAR a supplement; when the SYNOP observed nothing, the METAR fills the gap for dust. Wind,
temperature, humidity, pressure and visibility always come from the slot's **SYNOP whenever it has
one** (observed or not), and from the METAR only in slots without SYNOP, so a station's series
does not switch report types from slot to slot (1.6.0). Each source contributes at
most one report per slot (the closest that observed the weather), so a busy airport gets no extra
chances; SPECI never fill a slot (they are sent because conditions changed, often when dust
arrives) but count in `dusty_day_any`. Section 8 has the sampling; 5.3 the tables.

Evidence for this arrangement: [rules_history.md](rules_history.md), methods 1.5.0 and 1.6.0.

**What the code rules say** (extracts with page citations:
[references/metar-weather.md](references/metar-weather.md)):
- WMO FM 15: the weather group w'w' is omitted when nothing can be reported with code table 4678
  (15.8.2); an automatic system that cannot observe weather writes `//` (15.8.19); `CAVOK` means
  visibility of 10 km or more and no significant weather (15.10); DU, SA, HZ and FU are coded only at
  visibility of 5 000 m or less (15.8.12). `AUTO` marks a report with no human involvement (15.4).
- US practice (FMH-1, 2005): obscurations (DU, SA, HZ, BLDU, BLSA) are coded only below 7 statute
  miles (11.27 km); weather away from the station goes to the remarks; `AO1`/`AO2` mark an
  automated station in every report, with or without an observer, so only `AUTO` means "no
  observer".
- ASOS (User's Guide 1998, §3.11): never reports dust, sand or smoke; its `HZ` is inferred from
  visibility and dew-point depression, not seen.

Validation of these rules (notebooks 03 and 06): [rules_history.md](rules_history.md), methods
1.4.0 and 1.8.0.

**Rules adopted** (user decisions 2026-10-20 unless marked *ruling*):
1. `AUTO` METAR/SPECI: not observed, and none of its codes is used, so an ASOS `HZ` cannot feed the
   haze rule. (*Ruling* on the codes, from the ASOS guide and the implausible AUTO dust above.)
2. `//`: not observed.
3. A weather group: classified from ISD's decoded fields like any report. **Dust counts at any
   visibility**: the observer reported dust; visibility only tells its strength. `VC` dust ("in the
   vicinity", 8-16 km away) is not dust at the station but sets the vicinity flag. `HZ` is haze and
   goes through the haze rule (6.5).
4. A weather group that ISD did not decode (e.g. the malformed `TSDU`): not observed (*ruling*:
   it cannot be classified).
5. `CAVOK`: observed, nothing significant (`nil_significant`), explicit like iX 2/5.
6. No weather group, visibility > 5 km with good QC: "nothing significant, implied"
   (`pw_status = nil_metar`). Counted apart; `valid_day_explicit` ignores it.
7. No weather group, visibility <= 5 km, missing or rejected: not observed (*ruling*: at <= 5 km
   WMO requires an obscuration to be coded, so something went unnamed; the SYNOP saw dust in
   15-33 % of these).
8. **METARs without full text** (method 1.8.0, user decisions 2026-10-28; tests in 8.2). Before
   2000 ISD keeps only fragments of the text (`metar_text = fragment`):
   - a `CAVOK` in the fragment, before any trend word or remarks (`BECMG CAVOK` is a forecast),
     is an explicit "nothing significant" (`nil_significant`);
   - otherwise, no code and visibility > 5 km is "nothing significant, inferred" (`nil_inferred`,
     as rule V3) if the station passes its **METAR guard**: among its full-text METARs with no
     code and visibility > 5 km, < 2 % are "not observed" (mostly AUTO), on >= 50 reports
     (`process/guard.py`; stored in the silver metadata as `metar_guard`).
9. **Derived SYNOPs** (user decision 2026-10-28): in a station-year whose SYNOPs copy the METAR
   just before them (temperature >= 95 % and wind speed >= 80 % identical, >= 100 pairs within
   15 min; `process/derived.py`), the SYNOP's codes and iX are not used (`not_observed`, flag
   `synop_derived`): dust and validity come from the METAR. US stations since ~2005 (94.7 % of
   their station-years with both types), 0.7 % elsewhere. Their wind, temperature and pressure
   are still the slot's weather values (they equal the METAR's).
10. **Visibility for the day** (user decision 2026-10-28): from the slot's primary report, or from
   its METAR when the primary SYNOP has none (`n_vis_from_metar` counts these slots).

Parsing (*rulings*): only the observed part of the message counts; the station identifier, the
remarks (`RMK ...`), trend forecasts (`NOSIG`, `BECMG`, `TEMPO ...`) and recent weather (`RE..`)
are ignored. A weather group is any token built from table-4678 letter pairs (deliberately broad,
so malformed groups such as `-TSBLSA` still count as weather; an unrecognised group can only lead
to "not observed", never to "no dust"). US messages start with a local date and time.
In the 2000s the word `METAR` is often missing and the text ends with `;` (`MET046LROP 282200Z
... NOSIG;`): a full message is recognised by its station identifier and time. Before 2000, `REM`
holds only the fragments ISD did not decode (`MET012CAVOK NOSIG?`): only their `CAVOK` is read
(rule 8). Silver columns: `metar_text` (`full`, `fragment`), `metar_auto`, `metar_wx` (`missing`,
`group`, `cavok`, `none`), `synop_derived`.
Settings: `classify.metar_rules`, `metar_report_types` ([FM-15, FM-16]), `metar_nil_min_vis_m`
(5000).

**Known limitation: light dust where only METAR observed.** SYNOP codes 06 and 07 have no
visibility limit (WMO table 4677; the automatic table 4680 splits only at 1 km), but METAR codes
dust only at <= 5 km (US: < 7 mi). Where a slot's only observation is a METAR, light dust above
5 km is mostly missed, so `dusty_day` is a lower bound at stations or in years with few observed
SYNOPs (see `n_slots_pw_synop` against `n_slots_pw`). Two measures handle this (user decisions):
the per-source flags (`dusty_day_synop`, `dusty_day_metar`) and `dusty_day_vis5`, which counts
only dust with visibility <= 5 km, caught alike by SYNOP and METAR. For SYNOP the 5 km limit is
applied with the visibility of the same report: a threshold we derive to match METAR, not a SYNOP
rule. Use `dusty_day_vis5` to compare stations or years with different report mixes;
`dusty_day_synop` to compare with duISD v1 or SYNOP-only stations; `dusty_day` for the fullest
count at a station.

**Vicinity dust.** `VC` dust groups are rare (370 of 3.18 M METARs) but concentrated: they would
add 80 dust days at Las Vegas (40 dust days at the station in 2015-2024) and 33 at El Paso. They
are kept out of the dust counts and reported as the daily flag `vicinity_dust_day` (user decision),
together with SYNOP vicinity codes (MV).

**Dust in the remarks** (method 1.9.0, user decision 2026-10-29). US practice (FMH-1) puts
weather seen away from the station in the remarks (`BLDU ALQDS`, `BLDU DSNT SW`, `VCBLDU`,
`BLDUB1845E1915`); Malta adds `DU` above 5 km. ISD does not decode remarks, so `parse.py` reads them
from the full text (`metar_rmk_dust`, regex `METAR_RMK_DUST`): DU, BLDU, DRDU, BLSA, DRSA, DS, PO,
DUST, with an optional intensity or `VC` and begin/end times. A forecast inside the remarks
(`TEMPO`, `BECMG`, `NOSIG`, `FMhhmm`) is cut off; a bare `SA` and `SS` are not used (on pilot_100
they were an observer's initials and a direction). Not counted as dust (not at the station); the
daily flag `remarks_dust_day` keeps it, apart from `vicinity_dust_day` (beyond 16 km vs 8-16 km).
pilot_100, all years: 8,333 reports, mostly El Paso, Las Vegas, Phoenix, Luqa, Mount Isa, Woomera.

**Station caveat: Niamey** (user decision 2026-10-29, no rule change). When its SYNOP reports dust
at <= 5 km, its METAR of the same time has no weather group in 5.8 % of cases (other stations
<= 0.6 %). Its SYNOPs fill almost every slot and either source's dust counts, so `dusty_day` is
barely affected; `dusty_day_metar` there is somewhat low.

### 5.3 Rule tables: every report type and situation

`pw_status` decides whether a report observed the weather (`has_pw`): `reported`,
`nil_significant`, `nil_inferred` and `nil_metar` count as observed; `not_observed` does not.
"Dusty" = a dust class (suspended, blowing, whirls, storms) or `dust_haze`.

**Which reports judge a 3-hourly slot** (section 8; method 1.5.0):

| Source | Report types (in order of preference) | Report used in the slot |
|---|---|---|
| SYNOP | FM-12, SY-MT, SY-SA, SY-AE, SY-AU | the closest that observed the weather (else the closest) |
| METAR | FM-15, SAO, AUTO | the same rule |
| none | SPECI (FM-16), SOD, SOM, any other type | never used for slots (SPECI and others still count in `dusty_day_any`) |

| SYNOP in the slot | METAR in the slot | Slot dusty? | Slot observed? | Primary report (wind, T, pressure, visibility) |
|---|---|---|---|---|
| observed, dust | any | yes | yes | SYNOP |
| observed, no dust | dust | **yes** (either is enough) | yes | SYNOP |
| observed, no dust | no dust or not observed | no | yes | SYNOP |
| not observed (iX 3/6, iX 1 without group, no code) | dust | **yes** | yes | SYNOP |
| not observed | no dust | no | yes | SYNOP |
| not observed | not observed or none | no | no | SYNOP |
| none | any | as the METAR | as the METAR | METAR |

A missing value in the slot's SYNOP (e.g. no wind) stays missing; it is not taken from the METAR.

**SYNOP (FM-12)**

| # | Situation | `pw_status` | Dust class | Notes |
|---|---|---|---|---|
| S1 | weather code present, iX 1/4 or no iX | reported | from the field's table (MW: 4677) | 6.2 |
| S2 | weather code present, iX 7 (automatic) | reported | from WMO 4680: 04/05 haze_or_suspended, 27-29 blowing if warm | 6.3, 6.4 |
| S3 | code present but iX 2/5 (inconsistent) | reported | the code wins | |
| S4 | no code, iX 2/5 | nil_significant | none | 5 |
| S5 | no code, iX 3/6 | not_observed | unknown | 5 |
| S6 | no code, no iX (BUFR-only, older years), visibility reported, station guard passes | nil_inferred | none | rule V3, 5.1 |
| S7 | no code, no iX, otherwise | not_observed | unknown | |
| S8 | code 05 (haze) | reported | haze; dust_haze if dust in the previous 24 h | 6.5 |
| S9 | dust code; visibility <= 5 km / > 5 km / unknown | reported | dusty | `dusty_day_vis5`: dust / not dust / slot left out |
| S10 | vicinity code (MV 03, 04, 05, 07, 08) | (as the rest) | not dust at the station | `vicinity_dust_day` |
| S11 | RH > 90 % with haze, haze_or_suspended or dust_haze (not code 06) | reported | screened (not dusty) | 6.6 |
| S12 | derived SYNOP (station-year copies its METARs; US since ~2005) | not_observed | unknown (codes, iX not used) | 5.2 rule 9 |

**METAR (FM-15) and SPECI (FM-16)**

| # | Situation | Example | `pw_status` | Dust | `dusty_day_vis5` |
|---|---|---|---|---|---|
| M1 | AUTO, weather `//` | `AUTO 9999 //` | not_observed | unknown | — |
| M2 | AUTO, no group | `AUTO 9999 NCD` | not_observed | unknown | — |
| M3 | AUTO with a group (dust, HZ, rain) | `AUTO 6SM HZ` | not_observed | unknown (codes not used) | — |
| M4 | non-AUTO, `//` | `9999 //` | not_observed | unknown | — |
| M5 | no group, visibility > 5 km | `10SM CLR`, `9999 FEW020` | nil_metar | none | no dust |
| M6 | no group, visibility <= 5 km | `4000 SCT030` | not_observed | unknown | — |
| M7 | no group, visibility missing or rejected | `//// SCT030` | not_observed | unknown | — |
| M8 | `CAVOK` | `CAVOK` | nil_significant | none | no dust |
| M9 | dust group, visibility <= 5 km | `3SM BLDU`, `4000 DU`, `0400 +DS` | reported | dusty | dust |
| M10 | dust group, visibility > 5 km | `6SM BLDU`, `8000 DU` | reported | dusty | no dust |
| M11 | dust only in the vicinity | `VCBLDU`, `VCSS`, `VCPO` | reported | none | no dust; `vicinity_dust_day` |
| M12 | haze | `5000 HZ` | reported | haze; dust_haze if dust in the previous 24 h | dust if dust_haze at <= 5 km |
| M13 | other weather only | `2SM BR`, `-RA`, `TS` | reported | none | no dust |
| M14 | dust and other weather | `TSRA BLDU 3000` | reported | dusty | by visibility |
| M15 | group ISD did not decode | `TSDU` | not_observed | unknown | — |
| M16 | recent weather only | `RESS`, `REDU` | ignored: the rest of the message decides | | |
| M17 | trend forecast | `TEMPO 3000 DU`, `BECMG`, `NOSIG` | ignored | | |
| M18 | dust only in the remarks | `RMK BLDU ALQDS`, `RMK DU` | the rest of the message decides | none | no dust; `remarks_dust_day` |
| M19 | corrected report | `METAR COR ...` | as non-AUTO (`COR` replaces `AUTO` in the US: a rare unattended report may slip through) | | |
| M20 | no full text (fragment or no `REM`), with a code | | reported | by the code | |
| M21 | fragment with `CAVOK` before any trend word | `MET012CAVOK NOSIG` | nil_significant | none | no dust |
| M22 | no full text, no code, visibility > 5 km, METAR guard passes | | nil_inferred | none | no dust |
| M23 | no full text, no code, otherwise | | not_observed | unknown | — |

**Other report types** (meanings from the ISD format document, GEOPHYSICAL-REPORT-TYPE):

| Type | Meaning | Treatment |
|---|---|---|
| SY-MT, SY-SA, SY-AE, SY-AU | SYNOP merged with METAR, airways, aero or automatic report | slot priority right after FM-12; code → reported, no code → not observed (rule V3 applies to FM-12 only) |
| SAO | airways report (US, older) | after METAR; code → reported, else not observed |
| AUTO | report from an automatic station | after SAO; codes read with ISD's AW list (6.3); no code → not observed |
| SOD, SOM | summary of day / month (US ASOS/AWOS) | no slot; daily dust codes (AT, AX) → `daily_summary_dust` |
| any other type | e.g. FM-13 SHIP, CRN, MESONET (not in the pilot data) | lowest slot priority; code → reported, else not observed |

Pilot data, bronze 2020: FM-12 343 587, FM-15 305 980, FM-16 9 093, SOD 1 335, SOM 36, SY-MT 12.

**Where each case ends up in the daily table** (section 9):

| Column | Counts |
|---|---|
| `n_slots_pw`, `valid_day` | slots observed by either source (reported, nil_significant, nil_inferred, nil_metar) |
| `n_slots_pw_explicit`, `valid_day_explicit` | slots with an explicit observation (a code, iX 2/5, CAVOK) from either source |
| `dusty_day` | a slot where either source reports dust (any visibility) |
| `dusty_day_synop`, `n_slots_pw_synop`, `valid_day_synop` | the SYNOP report of each slot alone |
| `dusty_day_metar`, `n_slots_pw_metar`, `valid_day_metar` | the METAR report of each slot alone |
| `dusty_day_vis5`, `n_slots_pw_vis5`, `valid_day_vis5` | dust at <= 5 km from either source; slots dusty only through a report of unknown visibility are left out |
| `n_slots_nil_inferred`, `n_slots_nil_metar` | slots with S6 / M5 |
| `dusty_day_any` | any report of the day dusty (all types, selected or not, SPECI included) |
| `vicinity_dust_day` | any report with vicinity dust (M11, S10) |
| `remarks_dust_day` | any METAR with dust only in its remarks (M18) |

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
`HZ` in an AUTO METAR is not used at all (5.2): ASOS infers it from visibility and humidity.

### 6.6 Humidity screen
If relative humidity > 90 %, the classes built from haze reports (haze, haze_or_suspended,
dust_haze) become `screened` (not dusty). Dust an observer named (suspended, code 06 or `DU`),
blowing dust and storms are never screened (method 1.9.0, user decision 2026-10-29).
*Why:* in very humid air, haze is more likely humidity haze or mist; but an observer who codes 06
has named dust, and transported dust often arrives with humid air. Before 1.9.0 suspended dust was
screened too: 0.6 % of it overall (3,724 of 616,000 reports, pilot_100, all years) but 14 % at Luqa,
4.5 % at Sal and 4.6 % at Bhairahawa.

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
reports would make busy airports look dustier. So, as in duISD v1, dust is judged in 3-hourly
slots (00, 03, ..., 21 UTC), with a fixed number of reports per slot.

Rules (user decisions 2026-10-20):
- Candidates: reports within ±30 min of the slot whose type belongs to a source
  (`sampling.sources`: SYNOP = FM-12, the merged SY-* types and `AUST`; METAR = FM-15, SAO, AUTO).
  `AUST` ("Dataset from Australia", ISD format document p. 6; 1945-1984) carries the same
  observations as FM-12 at complementary hours (dust in 1.25 % vs 1.45 % of observed reports in
  the same station-years); it was not a source before 1.10.0.
- **Off-grid schedules** (method 1.10.0, user decision 2026-10-29). Some stations observe every
  3 hours but one hour off the synoptic hours, by local clock time: Australia (east and centre
  02, 05, ... UTC; west 01, 04, ... UTC; the offset moves with daylight saving), Sydney, and
  Central Asian stations in the 1930s. Per station and UTC month, from the SYNOP-source reports
  that **observed the weather**: if >= 30 of them, < 10 % within ±30 min of the synoptic hours and
  >= 80 % within ±30 min of one offset (+60 or -60 min), that month's slots move by the offset
  (`slot_offset_min`; e.g. 01, 04, ..., 22 UTC). Each slot still takes one report per source, 8 a
  day. *Why observed reports:* since 2000 these stations also send automatic on-grid SYNOPs that
  rarely observe the weather (iX 6, 91-96 % not observed); counting them would hide the schedule.
  *Why per month:* the schedule follows the local clock (Woomera 2005: +60 min in summer, -60 min
  in winter) and can change within a year (Meekatharra 1975). Settings: `sampling.schedule_*`.
  *Validation* (notebook 07 follow-up): where the METAR at the same minute reports dust, the
  offset SYNOP does too in 91 % (-60 min) and 68 % (+60 min, 19 cases), against 94 % for on-grid
  SYNOPs at the other pilot stations. On pilot_100, 7,084 station-months at 16 stations are
  shifted, none elsewhere.
  Summaries (SOD, SOM) and SPECI are never candidates.
- Per source, one report per slot: weather observed (`has_pw`) first, then the type order, then
  the closest in time, then fixed tie-breakers (`source_rank`, `is_source_selected`).
- The slot's **primary** report (`is_synoptic_selected`): the slot's SYNOP whenever it has one,
  observed or not, otherwise its METAR. It supplies wind, temperature, humidity, pressure
  and visibility statistics, which therefore come from one report type per slot and are never
  double counted. *Why:* otherwise a station's series would switch between SYNOP and METAR values
  (different instruments, times and rounding; up to 2.4 m/s and 1.0 °C apart at some stations).
- Gold combines the two sources per slot: dusty if either reports dust (table in 5.3).
- Every candidate keeps its ranks and a `selection_reason`.
- *Why `has_pw` first:* a report that observed the weather beats a closer one that did not.
  "Observed" includes "nothing significant", so this does not favour dusty reports (kept, user
  decision 2026-10-29).

Example: Zabol 2023 has 8,226 METAR and 2,840 SYNOP. Its SYNOPs observe almost every slot and
carry nearly all the dust (2020: 126 dusty days from SYNOP, 34 from METAR alone). Phoenix 2020 has
4 SYNOPs a day, so half its slots are judged by METAR alone.

*Trade-off:* a station with both SYNOP and METAR gets two looks per slot, a SYNOP-only station
one, so `dusty_day` is somewhat more sensitive where both exist; `dusty_day_synop` stays
comparable across SYNOP stations.

## 9. Daily aggregation (gold `daily`, `sql/daily.sql`)

One row per station per local (LMST) day.

| Column(s) | Definition | Why |
|---|---|---|
| `n_slots`, `n_slots_pw` | slots with a SYNOP or METAR on the day; of those, slots observed by either | basis of validity |
| `valid_day` | `n_slots_pw ≥ 4` (of 8) | a day can only be judged dusty or not if enough was observed |
| `dusty_day` | a slot where either source reports dust, at any visibility (**primary definition**) | all dust the observers reported (5.2) |
| `n_slots_pw_explicit`, `valid_day_explicit` | slots with an explicit observation (code, iX 2/5, CAVOK); `≥ 4` | validity without inferred or implied "nothing", for sensitivity checks |
| `n_slots_nil_inferred`, `n_slots_nil_metar` | slots with "nothing significant" inferred (V3) or implied by a METAR without group | keeps them visible |
| `n_slots_synop`, `n_slots_pw_synop`, `valid_day_synop`, `dusty_day_synop` | the same from each slot's SYNOP alone | comparable with duISD v1 and SYNOP-only stations |
| `n_slots_metar`, `n_slots_pw_metar`, `valid_day_metar`, `dusty_day_metar` | the same from each slot's METAR alone | METAR's contribution; misses light dust (5.2) |
| `n_slots_pw_vis5`, `valid_day_vis5`, `dusty_day_vis5` | dust with visibility ≤ 5 km, either source | caught alike by SYNOP and METAR: compare stations and years with different report mixes |
| `vicinity_dust_day` | any report with dust in the vicinity (METAR `VC`, SYNOP MV codes) | dust near, not at, the station; kept apart |
| `remarks_dust_day` | any METAR with dust named only in its remarks (5.2) | distant dust (US `BLDU DSNT`, `ALQDS`); kept apart |
| `dusty_day_any` | any report of the day dusty (all report types, SPECI included) | more sensitive; depends on reporting rate |
| `past_weather_dust`, `daily_summary_dust` | past-weather or daily-summary dust evidence | evidence from other fields |
| `dusty_day_any_source` | `dusty_day_any` or either evidence flag | most sensitive |
| `n_dust_slots_<class>`, `n_slots_rh_screened` | slots per class (a slot counts in each class one of its two reports has) | keeps code 06 etc. visible |
| `dust_first_lmst_hour` | first dusty slot of the day | diurnal timing |
| `wsd_mean`, `wsd_max`, `gust_max` | wind from each slot's primary report (gust from all reports) | driver |
| `vis_min`, `vis_hmean`, `n_vis_zero`, `n_vis_from_metar` | minimum and harmonic-mean visibility (primary report; the slot's METAR when the SYNOP has none, counted in `n_vis_from_metar`) | harmonic mean averages extinction (≈ 1/visibility) |
| `t_*`, `td_mean`, `rh_mean`, `slp_mean` | means and extremes over the slots' primary reports | drivers |
| `dup6_mean` | dust uplift potential U³(1 + 6/U)(1 − 36/U²) for U > 6 m/s, else 0 | wind's capacity to lift dust (Marsham et al. 2011 form; fixed 6 m/s threshold) |
| `d_t_mean`, `d_slp_mean` | change from the previous day, only between two consecutive valid days | fronts often bring dust |

Analyses of dust frequency use **valid days only** (e.g. dusty days per year =
365 × dusty valid days / valid days); each dusty-day flag goes with its own validity
(`dusty_day_synop` with `valid_day_synop`, `dusty_day_vis5` with `valid_day_vis5`, ...). A day known
only from a daily summary has no row. Rule tables for every report type: section 5.3.

*Planned:* monthly and annual products, coverage, station catalog, climatology, diurnal cycle,
trends, wind threshold Uth (source stations only), code-06 diagnostic.

**Uth (threshold wind)** must be fitted with all observed reports in the denominator and on raised
dust (codes 07 and up); why: [rules_history.md](rules_history.md), method 1.2.0.

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
   US Southwest airports (ASOS augmented by observers: AO2, never AUTO; hourly METAR at :51 plus
   SYNOP at :00, 4 or 8 a day); clean controls (Shannon; Zhang Ping and Haikou in southern China,
   which never reported dust **and** do report weather). `flagDU = False` alone is not enough:
   stations that never report weather have it too.
7. `pilot_20`: the 2-3 dustiest per region (88 → 20) for end-to-end tests.

Known issue (pilot_20 test, 2026-10-19): Wadi Halfa, Abu Hamed and Papunya have no ISD data in
2020-2024; Dongola has no weather codes; Rabbit Flat (automatic) rarely reports weather; Ashchysai,
Shakawe and Good Hope report no dust in 2020-2024. pilot_100 is kept as it is (user decision
2026-10-29): it is a test set, and these gaps are useful test cases; stations with older data
still count for a 1931-2025 climatology. Coverage criteria (valid days per year and decade) will
be set for the published products, from the station catalog.

## 11. Open questions

1. NOAA's ISD files end on 2025-08-27: is ISD still updated?
2. Coverage criteria for the published products (valid days per year and decade).


Resolved questions: [rules_history.md](rules_history.md), "Resolved questions".

## 12. References

Official code manuals and tables, with URLs and checksums of the local copies:
[references.md](references.md).

- NOAA NCEI. *Federal Climate Complex Data Documentation for Integrated Surface Data (ISD)*
  (ISD format document), `legacy/isd-format-document.pdf` (local).
- WMO (2019). *Manual on Codes*, WMO-No. 306, Vol. I.1, Part A; code tables 1860, 4677, 4680,
  4561, 4531. WMO Codes Registry: https://codes.wmo.int (BUFR tables 0-20-003, 0-20-004).
- WMO (2025). *Manual on Codes*, WMO-No. 306, Vol. I.2 (BUFR/CREX); code table 0 20 003.
- WMO BUFR4 tables, release v46 (2026), https://github.com/wmo-im/BUFR4 (code table 0-20-003
  embeds tables 4677 as 0-99 and 4680 as 100-199; 508/509 = nothing significant / no observation).
- OFCM (2005). *Federal Meteorological Handbook No. 1: Surface Weather Observations and Reports*
  (FCM-H1-2005): US METAR/SPECI practice.
- OFCM (1988). *Federal Meteorological Handbook No. 2: Surface Synoptic Codes* (FCM-H2-1988).
- OFCM. *Federal Meteorological Handbook No. 12: United States Meteorological Codes and Coding
  Practices*.
- NOAA/FAA/US Navy/US Air Force (1998). *Automated Surface Observing System (ASOS) User's Guide*.
- Alduchov, O. A., and R. E. Eskridge (1996). Improved Magnus form approximation of saturation
  vapor pressure. *J. Appl. Meteor.*, 35, 601-609.
- Marsham, J. H., et al. (2011). The importance of the representation of deep convection for
  modeled dust-generating winds over West Africa during summer. *Geophys. Res. Lett.*, 38, L16803.
- Xi, X. (2021). [title to be filled in by the author]. *J. Geophys. Res. Atmos.*,
  doi:10.1029/2021JD034687. Data: duISD v1, Zenodo 10.5281/zenodo.4107346.
