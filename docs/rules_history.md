# DCDR documentation: Rules history

How the rules in [rules.md](rules.md) came about: what changed in each method version, why, the
evidence (validation notebooks), the rule it replaced, and who decided (*user decision* or
*ruling*, a choice made during implementation and open to review). The dated log of all work is
[worklog.md](worklog.md); notebooks are kept locally (not in the public repository).

> Last updated: 2026-10-29 (current method version 1.9.0). Commit hashes refer to this repository.

## Versions at a glance

| Method | Date | Change | Decision | Commit |
|---|---|---|---|---|
| 1.0.0 | 2026-10-15 | Plan 1: decoding, QC, classification, haze/RH, one report per 3-hourly slot, daily gold | design | 9b6e439 (plan) |
| 1.1.0 | 2026-10-17 | a missing weather field = "not observed"; past-weather and daily-summary dust evidence | user | b804f56 |
| 1.2.0 | 2026-10-19 | SYNOP iX rule: 2/5 nothing significant, 3/6 not observed, 7 = WMO table 4680 | user | c1ae416 |
| 1.3.0 | 2026-10-20 | rule V3: SYNOP without iX inferred "nothing significant" if visibility is reported (station guard) | user | df90c43 |
| 1.4.0 | 2026-10-20 | METAR/SPECI rules (AUTO, `//`, CAVOK, no group by visibility); `dusty_day_vis5`; vicinity flag | user + rulings | 20d7575 |
| 1.5.0 | 2026-10-20 | SYNOP and METAR judged independently per slot; dusty if either reports dust | user | cb3b298 |
| 1.6.0 | 2026-10-20 | weather values from the slot's SYNOP whenever present | user | 13c4674 |
| (no change) | 2026-10-27 | V3 kept for all years after tests across decades | user | 34e633f |
| 1.7.0 | 2026-10-27 | fix: METAR text without the word METAR (2000s) is read | bug fix | 6329264 |
| 1.8.0 | 2026-10-28 | METAR fragments (`CAVOK`), guarded METAR inference, derived SYNOPs, METAR visibility fallback | user | 39d6c88 |
| 1.9.0 | 2026-10-29 | humidity screen only on haze-based classes; `remarks_dust_day` | user | 7acc64d |
| (no change) | 2026-10-29 | daily product checked as a whole (notebook 07); off-grid Australian SYNOPs found | — | (worklog) |

---

## 1.0.0 (2026-10-15): the first pilot method

Design spec and Plan 1: ISD decoded field by field, QC flags, dust class per report with a
severity rank, haze rule and humidity screen, one report per 3-hourly synoptic slot (SYNOP first),
daily gold with `valid_day` = at least 4 of 8 slots with a weather **code**. Found on real data
(thin slice): at METAR-heavy stations few slots carry weather codes.

## 1.1.0 (2026-10-17): a missing weather field means "not observed"

*User decision ("interpretation 2").* A report without a weather field is not assumed to mean
"no significant weather". Dust evidence added from daily summaries (AT, AX) and past weather
(AY, AZ), and `dusty_day_any_source`.

## 1.2.0 (2026-10-19): the SYNOP iX rule

*User decision.* Superseded: "valid = >= 4 slots with a code". The raw SYNOP text in `REM` carries
iX (WMO code table 1860): 2/5 = group omitted because nothing significant happened (observed, no
dust), 3/6 = not observed, 7 = automatic station whose AW codes are read with WMO table 4680.
Confirmed in the WMO Codes Registry (BUFR 0-20-003 entries 508/509) and later in WMO-No. 306
(FM 12 Regulation 12.2.6).

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

**Implication for Uth (threshold wind).** Uth is estimated from how the dusty fraction rises with
wind speed, so the denominator must include clear-weather reports. With only reports carrying a
weather code (duISD v1's denominator), Zabol 2020-2024 looks 30-43 % dusty even at 0-6 m/s and
no threshold appears; with all observed reports (iX rule) it is 2-3 % below 8 m/s, about 50 % at
12-14 m/s and 95 % above 16 m/s, a clear threshold curve. This likely explains why the v1 Uth
method failed at Saharan stations. Fit Uth on **raised dust** (codes 07 and up): suspended dust
(06) is often transported and reported at low local winds (e.g. Harmattan, Sudan).

## 1.3.0 (2026-10-20): rule V3, inferring "nothing significant" without iX

*User decision* (adopted with the flag kept everywhere: "keep the inferred flag whenever it's
inferred"; principle: "if it cannot be verified, it is 'not observed'").

Experiment: `notebooks/02_validate_inferred_nil.ipynb` (pilot_100, 2015-2024). Ground truth: SYNOP
reports with an omitted weather group and a known iX (1.53 M "nothing significant", 0.49 M "not
observed"). iX was hidden and predicted from information available in every decade.

- In SYNOP, visibility (VV) is in the same group as iX. Visibility is reported in 84.7 % of
  "nothing significant" reports but in only 0.4 % of "not observed" ones.
- **Rule V3:** no weather code and no iX → "nothing significant" if visibility is reported (good
  QC) **and** the station's iX years show "not observed although visibility reported" in < 2 %
  of such reports (station guard; excludes e.g. Tidjikja, Good Hope, Letlhakane).
- Precision 0.9998, recall 0.84. Dusty days per year (iX hidden): median absolute error 46.5
  days/yr without inference, 0 with V3. Rules based on the share of codes 00-03 were precise but
  recovered only ~20 %.
- Caveats: no truth before 2015 (assumes stable visibility-reporting practice); stations without
  any iX years get no guard; some Sudanese stations' clear reports lack visibility.

Production differences: (The notebook used leave-year-out evidence and no minimum; production uses all years plus the
  minimum, slightly more conservative.)

**BUFR-only reports.** BUFR is WMO's binary format for the same SYNOP content; when NOAA received a
SYNOP only as BUFR, `REM` holds just `SYN004BUFR` (no text, no iX). Risk checked: if NOAA's decoder
dropped weather codes from BUFR, V3 would wrongly call dusty reports clear. Evidence (pilot_100,
2015-2024): BUFR-only SYNOPs carry a weather code in 28.3 % of reports vs 23.1 % for text, and the
dust share among codes is the same (15.31 % vs 15.33 %); station by station and year by year the
rates agree (e.g. Bachu and Tazhong changed practice in 2020: 13-34 % coded in both formats
before, 100 % after). Stations where the formats disagree (Sydney; Australian automatic stations)
get no inference (guard: no decision).

**Effect on pilot_100 (2015-2024):** guard pass 80, fail 5 (Tidjikja, Khasab Port, Good Hope,
Letlhakane, Carnarvon), no decision 13 (mostly Australian automatic and some Indian stations).
Inference touches 44 of 98 stations; where it matters most, dust frequency falls toward
plausible values (Nokkundi 325 → 155 dusty days/yr; Bachu 85 → 71).

## 1.4.0 (2026-10-20): METAR and SPECI rules

*User decisions* (dust at any visibility; HZ through the haze rule; vicinity dust as a separate
flag; `dusty_day_vis5` and `n_slots_metar`), with *rulings* (AUTO codes not used; no group at
<= 5 km not observed; undecoded groups not observed; CAVOK explicit; broad weather-group parser).
Before: a METAR without a weather field was "not observed".

**Validation** (`notebooks/03_metar_present_weather.ipynb`; pilot_100 2015-2024: 3.18 M
METAR/SPECI, 680 k at the same minute as a SYNOP that observed the weather):
- When the SYNOP reports dust, the METAR reports it in 95-96 % if its visibility is <= 5 km and in
  11 % above 5 km. SYNOP code 06 is mostly reported at visibility above 5 km.
- A non-AUTO METAR without a weather group: with visibility 5 to < 10 km the SYNOP saw dust in
  44 % (light dust METAR is not allowed to code); with visibility >= 10 km in 0.8 % overall, but
  14.7 % at Barbados and 6.6 % at Niamey (transported dust, code 06).
- Counting only dust with visibility <= 5 km, a METAR without a group missed such dust in <= 0.6 %
  at 20 of 21 stations (Niamey 5.8 %).
- AUTO METARs (mainly Australian automatic stations) carry `//` in 93 %; the 157 that carry dust are
  implausible (`SA` with visibility 9 999 m at Kashi; `DU` with visibility 0 m at Learmonth).
- The US stations' METARs (at :51) never share a minute with their SYNOPs (at :00), so they are
  not in the paired sample (open item: pair within minutes).

Correction made on the way: the US Southwest stations (Phoenix, El Paso, Las Vegas) are not
METAR-only; they send SYNOPs at :00 (4-8 a day since at least 2005) besides METARs at :51, so
notebook 03's same-minute pairs missed them.

## 1.5.0 (2026-10-20): SYNOP and METAR judged independently per slot

*User decision.* Superseded: one report per slot, SYNOP first (an unobserved SYNOP filled the slot).

*Why (method 1.4.0 took one report per slot, SYNOP first):* a SYNOP that observed nothing then hid
the METAR next to it. US SYNOPs show the problem: after a METAR reporting dust, the next SYNOP
(9 minutes later) carried dust in 1 of 103 cases at El Paso and 1 of 12 at Phoenix (2015-2024);
92 of El Paso's say iX 1 ("weather group included") but contain no weather group, so they are
"not observed" (they seem to be generated automatically). With 1.5.0, El Paso 2020 has 5 dusty
days in the slots instead of 1. The US stations sent no SYNOP in the 1985 and 1995 samples and
4-8 a day since at least 2005.

## 1.6.0 (2026-10-20): weather values from the SYNOP whenever present

*User decision.* Superseded (1.5.0): the primary report was the SYNOP if it observed the weather,
else the METAR, so a station's wind, temperature and pressure could switch report types from slot
to slot. Now the slot's SYNOP always supplies them, the METAR only in slots without SYNOP;
*ruling*: a value missing from that SYNOP is not filled from the METAR.

## Full-record test of the slot sources (2026-10-20, notebook 04)

pilot_100, 1931 to 2025-08-27, 5 651 station-years, method 1.6.0:


`notebooks/04_slot_sources_full_record.ipynb`: pilot_100, 1931 to 2025-08-27, 5 651 station-years,
method 1.6.0 (the choice of dust and weather sources is still under test). Main results:
- **METARs without raw text (all before 2000) bias dust frequency.** Without text a METAR counts
  as observed only when it carries a code, so clear slots drop out (44-51 % of METAR slots
  observed before 2000, 85-90 % since). Counting only coded METAR slots on 2010-2024 data cuts
  valid days by ~90 % and raises dust frequency by a median factor of 4.6. Affected: stations or
  years relying on METAR before 2000 (Gimpo; El Paso before 2005; Dammam, Ilam, In Salah have no
  SYNOP in ISD before 2005). Old SAO reports (before 1960) always carry a code (mostly 00) and are
  not affected.
- **SYNOP and METAR values differ at some stations** (same slot): wind +2.4 m/s (Aktyubinsk),
  temperature +1.0 °C (Kashi); median +0.05 °C, 0.0 m/s. Supports one source for weather values.
- **SYNOP-first weather values lose visibility** where the SYNOP has none but the METAR has: 11.5 %
  of slots with both (Australian automatic SYNOPs 50-63 %); temperature 0.2 %, wind 1.1 %.
- **METAR adds 1.6 % of dusty days overall**, mostly where the SYNOP did not observe; it is the
  main source at El Paso (79 %), Las Vegas, Phoenix and Gimpo (no SYNOP).
- Rule V3 infers "nothing significant" for some US SYNOPs while the METAR reports dust (151 slots).
- NOAA's S3 data end on 2025-08-27 (no 2026 files).

## V3 across decades (2026-10-27, notebook 05): kept for all years

*User decision:* keep V3 for all years, with no condition on the visibility value. Options
considered: V3 only from 1980; V3 only in the iX years (the strict reading of "verify or not
observed", which would bring back the selection bias of 1.1.0 for 1980-2012).

**V3 across decades** (`notebooks/05_v3_across_decades.ipynb`, local; pilot_100 1931-2025; no
ground truth before 2010 except 265 reports, so the tests are indirect):
- V3 is barely used in 1950-1979 (99 % of SYNOPs carry a code) but supplies 0.4-1.0 M "nothing
  significant" slots per decade from the 1980s, when observers leave out the weather group. In
  the 1930s-40s it infers 65-91 % of the reports of a few Central Asian stations.
- At the year iX appears (2013-2015), dust frequency (SYNOP alone) drops by a median 1.9 points
  (normal year-to-year change 0.0; 79 stations with >= 100 valid days both years), with large
  drops at some dusty stations (Niamey -38, Zahedan -36, Kayes -29 points). The year coincides with
  a change in ISD's SYNOP intake, so V3 cannot be separated from it; a downward step is what V3's
  recall (0.84) predicts.
- The only old ground truth (1970s-80s, mostly Chinese and Kazakh stations): precision 0.81 on
  156 inferences, against 0.9998 in 2015-2024; visibility did not separate "nothing significant"
  from "not observed" there. Too few reports to conclude.
- V3 needs visibility reported, not a value. Precision in 2015-2024 is 0.986 at <= 1 km and
  0.998-0.9996 above; older inferences more often come with low visibility (<= 1 km: 4-13 % in
  the 1930s-70s, 0.1-0.3 % since 1980).
- **Decision (user, 2026-10-27): keep V3 for all years, with no condition on the visibility
  value.** Before 1980 SYNOPs carry a code, so V3 is rarely used; its main effect is 1980-2013, when
  neither a code nor iX is reported; the visibility value barely changes precision, so its presence
  is the test. Caveat: in the 1930s-40s V3 infers most reports of a few Central Asian stations
  (Aralsk, Zhosaly, Naryn). `valid_day_explicit` (no inferred slots) remains for sensitivity checks.

## 1.7.0 (2026-10-27): METAR text without the word METAR

*Bug fix*, found while testing METARs without text: in the 2000s, 47 % of full METAR texts start
with the station identifier (`MET046LROP 282200Z ... NOSIG;`) and were skipped, so the 1.4.0
METAR rules were not applied to them. Before 2000, `REM` holds only fragments (`CAVOK NOSIG`).

## 1.8.0 (2026-10-28): METARs without full text, derived SYNOPs, visibility

*User decisions* (all four recommended options). Notebook 06 (local): derived SYNOPs on the whole
archive (bronze), METARs without text on pilot_100 plus 100 dryland METAR stations
(`config/stations/metar_test_100.csv`), all years:


`notebooks/06_metar_no_text_and_derived_synop.ipynb` (local): (b) on the whole archive, (a) on
pilot_100 plus 100 dryland METAR stations (`config/stations/metar_test_100.csv`), all years.
- **Derived SYNOPs** (SYNOP values copied from the METAR 4-9 min earlier: temperature >= 95 % and
  wind speed >= 80 % identical): 94.7 % of US station-years with both report types (since ~2005),
  0.7 % elsewhere. They lose dust: after a dusty METAR the SYNOP has dust in 17 % (independent
  SYNOPs: 91 %) and says "nothing significant" in 32 %.
- **METAR text in ISD:** before 2000 only fragments ISD did not decode (93-98 % of METARs;
  `CAVOK` in 30-37 %); full text from the 2000s (47 % of it without the word METAR, read since
  1.7.0).
- **Without text** a METAR counts as observed only when coded: dust frequency x2.6 (2000-2025
  simulation, 40 stations with METAR dust). Reading `CAVOK` in the fragments gives x1.05; a guarded
  inference "no code + visibility > 5 km = nothing significant" (precision 0.993-0.999; 102 of 154
  stations pass the guard) gives x1.07; both together x1.00.
- Old US airways reports (SAO) carry an explicit "nothing" code in 84-87 % of their codes: not
  biased.

Adopted: `CAVOK` in pre-2000 fragments = explicit "nothing significant"; the guarded METAR
inference for METARs without full text (`nil_inferred`); derived SYNOPs not used for present
weather; visibility from the slot's METAR when the SYNOP has none. *Rulings:* the METAR inference
reuses `nil_inferred`; derived detection compares FM-12 with FM-15 only. Real-data check: Gimpo
1985, of 16,076 METARs: 5,656 `CAVOK` from fragments and 4,073 inferred, 93 not observed (before,
most of the ~10,000 uncoded ones were not observed).

## 1.9.0 (2026-10-29): humidity screen and dust in the remarks

*User decisions* (the recommended options for the open questions of 1.8.0).
- **Humidity screen.** *Superseded:* above 90 % relative humidity, suspended, haze,
  haze_or_suspended and dust_haze were screened (from duISD v1). *Evidence* (pilot_100 silver,
  method 1.7.0, all years): screened were 3,724 of 615,979 suspended-dust reports (0.6 %),
  1,603 of 132,771 dust_haze, 134 haze_or_suspended (and 27,246 haze, not dusty anyway); but at
  humid sites with transported dust much more: Luqa 675 of 4,879 (14 %), Sal 1,002 of 22,115,
  Bhairahawa 190 of 4,160, Gimpo 241 of 4,445, Gran Canaria 182 of 7,559. An observer coding 06
  has named dust, not haze. *Adopted:* only haze, haze_or_suspended and dust_haze are screened.
- **Dust in the remarks.** *Superseded:* ignored (ISD does not decode remarks). *Evidence:*
  notebook 03 found 2,632 reports with dust only in the remarks (El Paso 1,178 vs 568 in the
  weather group); FMH-1 puts weather away from the station there. *Adopted:* a separate daily flag
  `remarks_dust_day`, not counted as dust. *Rulings,* from a check on pilot_100 bronze, all years
  (6.5 M full METARs, 1.1 M with remarks): forecasts inside the remarks are cut off (`TEMPO 1500
  BLDU` at Niamey, `FM0400 ... DU` in Australia); a bare `SA` is not used (all 102
  matches were an observer's initials at Woomera, `OBS SA`), nor `SS` (its one match was a
  direction); begin/end times (`BLDUB1845`) are matched. Result: 8,333 reports (BLDU 4,991, DU
  1,284, VCBLDU 1,138, DUST 964, PO 77, DS 27).

## Daily product check (2026-10-29, notebook 07): rules pass, a sampling gap found

Gold for pilot_100 under 1.9.0 (1,916,268 station-days, 1931 to 2025-08), checked as a whole.
- **V3 does not distort the series.** The share of valid days that need inferred "nothing" jumps
  from ~0 to 0.65 in 1985 and falls to ~0.1 in 2013; the dusty share among valid days runs
  smoothly through both (~17-20 %); the explicit-only share jumps to ~35 % (selection bias).
- **Physics:** expected seasonal cycles per region; raised dust peaks in the afternoon; 86 of 88
  stations windier on dusty days.
- **duISD v1:** Spearman 0.86 for station means (1991-2019); median year-to-year r = 0.81.
- **Gap:** at all 12 Australian stations and Sydney the weather reports miss the +-30 min UTC
  slots: FM-12 on a 3-hourly schedule one hour off the synoptic hours (1970-1999, and some
  automatic stations since 2002), and type `AUST` (1945-1984), not a slot source. Open question.

---

## Resolved questions

- Exact WMO regulation for omitting the SYNOP weather group: FM 12 Regulation 12.2.6 (1.2.0).
- METAR without a weather group: rules 5-7 of 1.4.0, extended in 1.8.0.
- US SYNOPs that hide METAR dust: per-slot sources (1.5.0); derived SYNOPs (1.8.0).
- Rule V3 before 2013: kept for all years (2026-10-27).
- METARs without full text and their selection bias: 1.8.0.
- Visibility under SYNOP-first weather values: from the METAR when the SYNOP has none (1.8.0).
- BUFR-only SYNOPs without iX (17 %): current handling kept, no other source (2026-10-29).
- Dust in METAR remarks: separate flag `remarks_dust_day` (1.9.0).
- Niamey's METARs missing dust at <= 5 km (5.8 %): a station caveat, no rule change (2026-10-29).
- Humidity screen at humid dust sites: haze-based classes only (1.9.0).
- `has_pw` before time distance in sampling: kept; "observed" includes "nothing" (2026-10-29).
- Recent-data criterion: pilot_100 kept as a test set; coverage criteria come with the products
  (2026-10-29).
