# SYNOP: the weather group 7wwW1W2 and the indicator iX

Source: WMO (2019). *Manual on Codes*, WMO-No. 306, Volume I.1, Part A – Alphanumeric Codes.
Local copy `references/wmo/306_i1_2019_en.pdf` (see [../references.md](../references.md)).
Page numbers: printed page (PDF page). WMO text is summarised; short quotes are marked.

Used by: `parse.py` (`syn_ix`), `classify.py` (`pw_status`), rules.md §5 and §5.1.

## When the weather group is included or omitted (FM 12, Regulation 12.2.6)

A–15 (PDF 39).

- **12.2.6.1** A manned station includes the group after a closure or at start-up (as `7ww//`,
  iX = 1). Otherwise the group is included "only … if present or past weather phenomena of
  significance, or both, were observed". The same applies to automatic stations able to report
  present and past weather.
- **12.2.6.2** ww 00–03 (changes in cloud, or sky unchanged) and W1W2 0–2 count as phenomena
  *without* significance.
- **12.2.6.3** The group is omitted if present and past weather were both (a) not available (no
  observation made) or (b) observed but not of significance. "The indicator ix shall indicate
  which one of these conditions applies."

Consequence for DCDR: a SYNOP without the group is ambiguous by itself; only iX (or rule V3)
separates "nothing significant" from "not observed".

## Code table 1860: iX

A–297 (PDF 321). Indicator for type of station operation and for present and past weather data.

| iX | Station | Group 7 | DCDR `pw_status` |
|---|---|---|---|
| 1 | manned | included | reported |
| 2 | manned | omitted, no significant phenomenon to report | nil_significant |
| 3 | manned | omitted, no observation, data not available | not_observed |
| 4 | automatic | included, tables 4677 and 4561 | reported |
| 5 | automatic | omitted, no significant phenomenon to report | nil_significant |
| 6 | automatic | omitted, no observation, data not available | not_observed |
| 7 | automatic | included, tables 4680 and 4531 (group 7wawaWa1Wa2) | reported, read with table 4680 |

The table's note: manned stations use iX 1–3; automatic stations normally use 5–7, and iX 4 only
if they can cope with tables 4677 and 4561.

## Code tables used for the weather codes

| Table | Content | Page |
|---|---|---|
| 4677 | ww, present weather from a manned station (dust: 06–09, 30–35, 98) | A–356 (PDF 380) |
| 4561 | W1W2, past weather, manned station (3 = sandstorm, duststorm or blowing snow) | A–355 (PDF 379) |
| 4680 | wawa, present weather from an automatic station (04/05 = haze or smoke, or dust in suspension; 27–29 = blowing or drifting snow or sand) | A–360 (PDF 384) |
| 4531 | Wa1Wa2, past weather, automatic station (2 = blowing phenomena, visibility reduced) | A–353 (PDF 377) |

The same entries in BUFR form: [bufr-present-weather.md](bufr-present-weather.md).
