# DCDR work log

A dated record of what was done, what was found and what was decided, newest first.
"User decision" = decided by Xin Xi; "Ruling" = a choice made by Claude during implementation,
open to review. Details of the current method: [science.md](science.md); of the software:
[engineering.md](engineering.md). Commits are referenced by their short hash.

## Open items

- [ ] Download WMO-No. 306 *Manual on Codes* Vol. I.1 Part A (2019) from
      https://library.wmo.int/idurl/4/35713 (the site blocks automated downloads) and quote the
      FM 12 rule for omitting group 7wwW1W2.
- [ ] Station selection: add a recent-data criterion (data and weather reports in ≥ 3 of the last
      5 years); replace Wadi Halfa, Abu Hamed, Papunya, Dongola, Rabbit Flat in pilot lists.
- [ ] Investigate stations with no dust in 2020-2024 despite high duISD ranks (Ashchysai, Shakawe,
      Good Hope): change of observing practice or real decline?
- [ ] BUFR-only SYNOP (17 %, no iX): any source for the "omitted" information?
- [ ] Decide whether to adopt rule V3 (`nil_inferred`) for reports without iX (validated 2026-10-20).
- [ ] Check visibility reporting in older decades (V3 assumes it is stable).
- [ ] Review humidity screen at humid dust sites and the `has_pw` sampling tie-break.
- [ ] Plan 2: monthly/climatology gold products, station catalog, coverage, Uth, code-06 diagnostic.
- [ ] Set up a GitHub remote; publish documentation (e.g. MkDocs) when stable.

---

## 2026-10-20

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
- **SYNOP iX rule** (user decision; `ed05e8e`, method 1.2.0). The raw SYNOP text in `REM` carries
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
- **Bug fixed** (`2e7f424`): pruning old gold versions failed on the exFAT SSD (macOS deletes the
  hidden `._x` twin with `x`). Reproduced on the SSD, test added, fixed.
- **pilot_20** (`ca1b194`): the 2-3 dustiest stations per region from pilot_100, for end-to-end
  tests. First full run (2020-2024): 103 station-years ingested, 84 silver, gold OK; ~30 s.
  Found: 4 selected stations without usable recent data; 3 with no dust reported recently.
- Default station list is pilot_100; thin_slice.csv removed (user decision; `fb62cc3`).
- Documentation started: `docs/engineering.md`, `docs/science.md`, this log (user request).

## 2026-10-18

- **Pilot station list** (user decisions; `8661f02` … `1477e75`). Notebook ranks duISD v1
  stations by mean dust frequency 1991-2019 (all dust codes incl. 06), regions = box + country
  filter, dusty = ≥ 1 %, up to 12 per region at ≥ 50 km → 88 dusty + 12 contrast = `pilot_100.csv`.
  Findings on the way: duISD v1 is SYNOP-only (most North American stations missing); ≥ 30 or
  ≥ 15 valid years left whole regions empty (→ ≥ 10 in 1991-2019); every duISD station has
  `flagDU = True`; `flagDU = False` stations may simply never report weather (Hong Kong
  automatic stations), so clean controls must also report weather (Zhang Ping, Haikou).
- **Naming** (user decision; `15194a4`): code and commands named by action (ingest, process,
  aggregate; later enrich, forecast), data by layer (bronze, silver, gold).
- **Packaging** (`f74dee6` … `57297aa`): SQL and default config inside the package; user paths
  relative to the working directory; package version in run records; a test builds, installs and
  runs the wheel outside the repo.
- **Code documentation** (`5180ec5`): module guides, docstrings and comments; verified that only
  comments changed (syntax-tree comparison).

## 2026-10-17

- **Missing weather = not observed** (user decision, "interpretation 2"; `66888ca`). Added dust
  evidence from AT/AX (daily summaries), AY/AZ (past weather) and `dusty_day_any_source`.
- **Final review fixes** (`a6a6c68`): summary-of-day reports dated to the day they summarise;
  slots counted on the slot's local day; last-year 31-December reports kept; one failing
  station-year no longer stops processing; gold refuses stale silver; missing-longitude sentinels.
- **Data root** (user decision; `39d038a`): default `./data`; the SSD is a user choice via
  `DCDR_DATA`, not a rule.

## 2026-10-15

- **Design spec** (`a31468a`) and **Plan 1** (`70054c7`): scaffolding + thin slice.
- **Plan 1 implemented** (`83dcdf1` … `973639d`): config, state, ingest, parse, QC, classify,
  haze/RH, sampling, silver build, gold daily, CLI; test-first throughout.
- **Thin slice on real data** (Kuwait, Las Vegas; `66fd677`). Found: year-boundary slot selected
  in two files (fixed, `425410d`); at METAR-heavy stations few slots carry weather codes.
- Rulings: QC ranges from the ISD document MIN/MAX; OD gust types 3 and 4; ruff excludes
  `legacy/` and `docs/`.

## 2026-10-14

- Analysis of the original scripts (`legacy/`): many bugs and undocumented choices found.
- **Revamp goals** (user): industry-grade, portfolio-quality pipeline (DS > MLE > DE) that also
  produces a citable dataset; $0 platforms; laptop compute; ISD read from AWS S3; publish and
  visualise; later environmental drivers and ML forecasting at dust source stations.
- Stack chosen: Python 3.12, uv, Polars, DuckDB, Parquet, Pydantic, Pandera, Typer, pytest, ruff,
  mypy, GitHub Actions. Cloud research-credit applications drafted (Google, AWS).
