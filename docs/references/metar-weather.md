# METAR/SPECI present weather, and what an automatic station can report

Sources:
- WMO (2019). *Manual on Codes*, WMO-No. 306, Volume I.1, Part A: FM 15 METAR / FM 16 SPECI
  regulations and code table 4678. Local copy `references/wmo/306_i1_2019_en.pdf`. Page numbers:
  printed page (PDF page). WMO text is summarised; short quotes are marked.
- NOAA, FAA, US Navy, US Air Force (1998). *Automated Surface Observing System (ASOS) User's
  Guide*. Local copy `references/nws/aum-toc.pdf`. US government work (public domain): quoted
  verbatim. Page numbers: printed page (PDF page).
- *Pending:* OFCM/ICAMS (2019). *Federal Meteorological Handbook No. 1* (FCM-H1-2019), the US
  METAR practice. The official host refused scripted downloads (HTTP 403, 2026-10-20).

Index and checksums: [../references.md](../references.md).

Used by: the METAR rules (notebook 03, in progress).

## Code table 4678: w'w' letter codes

A–359 to A–360 (PDF 383–384). A group is built from up to three parts: intensity or proximity
(`-`, none, `+`, `VC`), a descriptor (e.g. `BL`, `DR`, `TS`), and phenomena.

Dust-related entries:

| Code | Meaning (WMO 4678) | Notes in the table |
|---|---|---|
| DU | widespread dust | obscuration |
| SA | sand | obscuration |
| HZ | haze | obscuration; not dust-specific |
| PO | dust/sand whirls (dust devils) | `+` means well developed |
| DS | duststorm | intensity allowed (`-`, `+`) |
| SS | sandstorm | intensity allowed (`-`, `+`) |
| DR | descriptor: low drifting | dust, sand or snow raised to less than 2 m (note 8) |
| BL | descriptor: blowing | raised to 2 m or more (note 8); DR/BL only with DU, SA, SN |
| VC | in the vicinity | only with TS, DS, SS, FG, FC, SH, PO, BLDU, BLSA, BLSN, VA (note 13) |

So the dust groups a METAR can carry are DU, SA, BLDU, BLSA, DRDU, DRSA, PO, DS, SS, with
intensity on DS/SS and VC on DS, SS, PO, BLDU, BLSA.

## FM 15 regulations

| Regulation | Page | Content |
|---|---|---|
| 15.4 AUTO | A–28 (PDF 52) | AUTO marks a fully automated report without human intervention; an element that cannot be observed is replaced by solidi (two, `//`, for present weather). |
| 15.8.1 | A–31 (PDF 55) | Up to three w'w' groups report present weather of significance to aeronautical operations, per table 4678. |
| 15.8.2 | A–31 (PDF 55) | If the observed weather cannot be reported with table 4678, the w'w' group is omitted. |
| 15.8.10 | A–32 (PDF 56) | VC: phenomena observed in the vicinity, about 8–16 km from the aerodrome (set locally). |
| 15.8.12 | A–32 (PDF 56) | FU, HZ, DU and SA (except DRSA) are used only when the obstruction to vision is predominantly lithometeors and visibility is reduced "to 5 000 metres or less". |
| 15.8.19 | A–33 (PDF 57) | An automatic system that cannot observe present weather reports `//`. |
| 15.10 CAVOK | A–34 (PDF 58) | CAVOK replaces visibility, weather and cloud when, among other conditions, visibility is 10 km or more and there is no significant weather (table 4678). |

What this means for DCDR:
- In a METAR with **no w'w' group** and no AUTO, the code form says there was no weather to
  report (15.8.2); unlike SYNOP there is no indicator such as iX that states it explicitly.
- `//` or CAVOK are explicit: not observed, and nothing significant, respectively.
- DU, SA and HZ require visibility ≤ 5 000 m under WMO/ICAO rules, so METAR misses dust
  events that a SYNOP observer reports at higher visibility (ww 06/07 have no visibility limit).
  This fits the finding that SYNOP reports dust far more often than METAR (worklog 2026-10-20).
  US practice (FMH-1) uses a different limit; to be checked when FMH-1 is available.

## ASOS: what an automatic US station reports

Section 3.11 "Obscuration Algorithm", p. 38 (PDF 45):

> "ASOS reports only four obstructions: fog (FG), mist (BR), freezing fog (FZFG) and haze (HZ).
> Other obscurations, such as dust, smoke, and blowing sand are not automatically reported by
> ASOS, but may be augmented by the observer in accordance with agency policy."

> "Obstructions are not directly measured by ASOS, but rather inferred from the measurement of
> visibility, temperature, dew point, and present weather (precipitation)."

How ASOS decides on HZ (same section and p. 39, PDF 46): when visibility drops below 7 statute
miles, it uses the dew-point depression: above 4 °F (about 2 °C) with no precipitation, HZ is
reported (at or below 4 °F it is fog or mist). When temperature or dew point is missing, HZ is
reported for visibility of at least 4 but under 7 miles. Section 3.12 (p. 39) adds that real fog
or mist is then reported, incorrectly, as HZ.

What this means for DCDR:
- In an **AUTO** METAR (no observer), dust is never reported: for dust it is "not observed",
  never "nothing significant" (user principle: if it cannot be verified, it is "not observed").
- **HZ in an AUTO METAR** is inferred from visibility and humidity, not seen: it says nothing
  about dust and must not feed the haze rule.
- An augmented report (observer present; no AUTO) can carry dust codes.
