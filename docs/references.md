# Reference documents

> Last updated: 2026-10-20.

The official documents behind DCDR's decoding rules. Full copies live in `references/` (git-ignored,
a few MB each); the parts we rely on are extracted, with page citations, in
[`references/`](references/) next to this file:

| Extract | Topic | Used by |
|---|---|---|
| [synop-weather-group.md](references/synop-weather-group.md) | FM 12 Regulation 12.2.6 (when group 7 is omitted); iX, code table 1860; tables 4677/4561/4680/4531 | `parse.py`, `classify.py`; science.md §5 |
| [bufr-present-weather.md](references/bufr-present-weather.md) | BUFR 0-20-003: dust codes, tables 4677/4680, 508/509 | `classify.py`; science.md §5, §6 |
| [metar-weather.md](references/metar-weather.md) | METAR present weather; what ASOS (AUTO) can and cannot report | METAR rules (in progress) |

Why both: an extract keeps the rule we use next to the method and under version control; the full
copy answers the next question (context, footnotes, nearby exceptions). Each extract names the
document, edition, section and page. Re-create the downloadable copies with
`uv run python scripts/fetch_references.py`; a different checksum means the publisher replaced
the file, so the extracts must be re-checked.

Copyright: US federal handbooks are public domain (verbatim excerpts are fine). WMO-No. 306 allows
"short extracts" with the complete source indicated, so WMO material is summarised in our own words
with short quotes. The WMO BUFR4 tables on GitHub are MIT-licensed.

## Documents

| Local copy (`references/`) | Document | Publisher, edition | Source | Obtained | Size (bytes) | SHA-256 |
|---|---|---|---|---|---|---|
| `wmo/306_i1_2019_en.pdf` | *Manual on Codes*, WMO-No. 306, Vol. I.1, Part A: alphanumeric codes (FM 12 SYNOP, FM 15 METAR, code tables) | WMO, 2019 edition | https://library.wmo.int/idurl/4/35713 (manual download) | manual | 11,573,494 | `089df6cf851af52785f40fbc874ec6e82565d02f5e202cded63ad50c498e5353` |
| `wmo/WMO-306-v-I-2-2025_en.pdf` | *Manual on Codes*, WMO-No. 306, Vol. I.2: Part B binary codes (BUFR, CREX), Part C common features | WMO, 2025 edition | https://community.wmo.int/volume-i2 → library.wmo.int (manual download: the library has a human check) | manual | 22,778,169 | `460a0153f301130b45947c6660e645760a9cb589655277d274dde9553e4b7929` |
| `wmo/BUFRCREX_CodeFlag_en_20.csv` | BUFR/CREX code and flag tables, class 20 (weather) | WMO, BUFR4 release v46 (2026-06-08) | https://github.com/wmo-im/BUFR4 (tag `v46`) | script | 96,803 | `91dbb072387f166cb6f629340985846d8e6ed1de2ac2a625bdf222328f32b390` |
| `wmo/codes-registry_bufr4_0-20-003.jsonld` | Code table 0-20-003, present weather | WMO Codes Registry | https://codes.wmo.int/bufr4/codeflag/0-20-003 | script | 119,656 | `947f08aff984704900d7ef9edf8b1c8f031c9bf0e3e3c637beb408c28b8d8a74` |
| `nws/aum-toc.pdf` | *Automated Surface Observing System (ASOS) User's Guide* | NOAA, FAA, US Navy, US Air Force, March 1998 | https://www.weather.gov/media/asos/aum-toc.pdf | script | 918,728 | `1b7c0edea39bd6c282ebe905da7b74cbc3dce97f28239bfccfce6bce09b8d8d8` |
| `icams/fmh2-chg4.pdf` | FMH-2 *Surface Synoptic Codes*, Change No. 4 (one pen-and-ink change, p. 4-36) | OFCM, 17 March 2005 | https://www.icams-portal.gov/resources/ofcm/fmh/FMH2/fmh2-chg4.pdf | script | 48,515 | `f5946fee1301d35901415290771e1dd94edc49094249248940f7279cfc9f6401` |
| `legacy/isd-format-document.pdf` (in git) | *Federal Climate Complex Data Documentation for Integrated Surface Data* | NOAA NCEI | kept with the v1 material | — | — | — |

## Still to obtain

Save these into `references/icams/` under the file names in `scripts/fetch_references.py`;
the script then skips them and prints their checksums.

| Document | Why we need it | Source |
|---|---|---|
| FMH-1 *Surface Weather Observations and Reports* (FCM-H1-2019) | US METAR/SPECI practice: when DU, SA, HZ, BLDU, DS, SS are reported; visibility limits | https://www.icams-portal.gov/resources/ofcm/fmh/FMH1/fmh1_2019.pdf (the host refuses scripted downloads: HTTP 403 on 2026-10-20, twice; save from a browser) |
| FMH-2 *Surface Synoptic Codes* (1988) and changes 1-3 | US SYNOP practice | https://www.icams-portal.gov/resources/ofcm/fmh/allfmh2.htm (HTTP 403 to scripts; save from a browser) |
| FMH-12 *United States Meteorological Codes and Coding Practices* | US code practice overview | https://www.icams-portal.gov/resources/ofcm/fmh/FMH12/fmh12.pdf (HTTP 403 to scripts; save from a browser) |
| ICAO Annex 3 *Meteorological Service for International Air Navigation* | International METAR practice | ICAO (not free); WMO-No. 306 FM 15 holds the code form; FMH-1 the US practice |
