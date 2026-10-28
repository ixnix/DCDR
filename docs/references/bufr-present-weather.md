# BUFR present weather: code table 0-20-003

Sources:
- WMO (2025). *Manual on Codes*, WMO-No. 306, Volume I.2, FM 94 BUFR code tables; table 0 20 003
  on PDF pages 904–911. Local copy `references/wmo/WMO-306-v-I-2-2025_en.pdf`.
- WMO BUFR4 tables, release v46 (MIT licence), `BUFRCREX_CodeFlag_en_20.csv`, and the WMO Codes
  Registry export of 0-20-003 (same entries, machine-readable).

Index and checksums: [../references.md](../references.md). WMO text is summarised.

Used by: the BUFR parity check (worklog 2026-10-20) and as the official cross-check of the
alphanumeric tables in [synop-weather-group.md](synop-weather-group.md).

## Layout of 0-20-003

| BUFR value | Meaning | Alphanumeric equivalent |
|---|---|---|
| 0–99 | present weather from a manned station | table 4677 ww, same number |
| 100–199 | present weather from an automatic station | table 4680 wawa, value − 100 |
| 508 | no significant phenomenon to report; present and past weather omitted | iX 2 or 5 |
| 509 | no observation, data not available; present and past weather omitted | iX 3 or 6 |
| 510 | present and past weather missing, but expected | (no iX equivalent) |

508/509 are on PDF p. 911. They are how a BUFR SYNOP says what iX says in the text code. ISD's
`SYN004BUFR` reports carry no text, so neither survives into ISD (rules.md §5.1).

## Entries DCDR treats as dust

| BUFR | Summary of the WMO entry | Page (PDF) | DCDR class |
|---|---|---|---|
| 04 | visibility reduced by smoke (fires, industry, volcanic ash) | 904 | not dust |
| 05 | haze | 904 | dust_haze only through the haze rule (rules.md §6) |
| 06 | widespread dust in suspension, not raised by wind at or near the station | 904 | suspended |
| 07 | dust or sand raised by wind at or near the station, no whirls or storm | 904 | blowing |
| 08 | well-developed dust or sand whirls | 904 | whirls |
| 09 | duststorm or sandstorm within sight, or at the station in the past hour | 904 | storm |
| 30–32 | slight or moderate duststorm or sandstorm (decreased / unchanged / increased in the past hour) | 905 | storm |
| 33–35 | severe duststorm or sandstorm (same three tendencies) | 905 | storm_severe |
| 98 | thunderstorm with duststorm or sandstorm | 906 | storm_thunder |
| 104 | haze or smoke, or dust in suspension, visibility ≥ 1 km (wawa 04) | 907 | haze_or_suspended |
| 105 | the same, visibility < 1 km (wawa 05) | 907 | haze_or_suspended |
| 127–129 | blowing or drifting snow *or sand* (wawa 27–29) | 907 | blowing, only when warm and no snow |

wawa 04/05 lump haze, smoke and dust together, so an automatic station cannot report dust
specifically; wawa 27–29 lump snow and sand, which is why `classify.py` uses temperature and
snow reports to decide (`rank_4680`).
