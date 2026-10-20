# METAR/SPECI present weather, and what an automatic station can report

Sources:
- WMO (2019). *Manual on Codes*, WMO-No. 306, Volume I.1, Part A: FM 15 METAR / FM 16 SPECI
  regulations and code table 4678. Local copy `references/wmo/306_i1_2019_en.pdf`. Page numbers:
  printed page (PDF page). WMO text is summarised; short quotes are marked.
- NOAA, FAA, US Navy, US Air Force (1998). *Automated Surface Observing System (ASOS) User's
  Guide*. Local copy `references/nws/aum-toc.pdf`. US government work (public domain): quoted
  verbatim. Page numbers: printed page (PDF page).
- OFCM (2005). *Federal Meteorological Handbook No. 1: Surface Weather Observations and Reports*
  (FCM-H1-2005, September 2005), the US METAR/SPECI practice; local copy
  `references/icams/FCM-H1-2005.pdf`. And OFCM (1995), FCM-H1-1995 (December 1995), the edition
  that introduced METAR in the US; local copy `references/icams/fmh1.pdf`. US government works:
  quoted verbatim. Page numbers: chapter-page (PDF page). Only published editions are used
  (user decision 2026-10-20).

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
  US practice uses a higher limit, 7 statute miles (next section).

## US practice: FMH-1 (2005; 1995 the same)

Present weather in METAR/SPECI, FCM-H1-2005 paragraph 12.6.8, pp. 12-5 to 12-6 (PDF 63–64):

> "Weather occurring at the point of observation (at the station) or in the vicinity of the
> station shall be coded in the body of the report; weather observed but not occurring at the
> point of observation (at the station) or in the vicinity of the station shall be coded in
> Remarks."

> "With the exceptions of volcanic ash, low drifting dust, low drifting sand, low drifting snow,
> shallow fog, partial fog, and patches (of) fog, an obscuration shall be coded in the body of the
> report if the surface visibility is less than 7 miles or considered operationally significant."

- Intensity: none for BLDU, BLSA, BLSN; "Only moderate or heavy intensity shall be ascribed to
  sandstorm (SS) and duststorm (DS)" (12.6.8.a(1)).
- VC may be coded with PO, BLDU, BLSA, SS, DS (among others), without intensity (12.6.8.a(2)).
- DR = raised to less than six feet; BL = six feet or more; both only with DU, SA, SN (12.6.8.b).
- Codes: DU widespread dust, SA sand, HZ haze; PO dust/sand whirls, SS sandstorm, DS duststorm.

Definitions, paragraphs 8.3.2–8.3.3 (PDF 39–40): widespread dust is raised or suspended by the
wind and may have occurred at or far away from the station; haze is extremely small dry particles
giving the air an opalescent appearance (not dust-specific); a duststorm is strong winds and
dust-filled air over an extensive area. The glossary (Appendix A, PDF 80) defines blowing dust and
blowing sand as reducing visibility to less than 7 statute miles.

Automation (FCM-H1-2005):
- 12.6.4, p. 12-2 (PDF 60): "The report modifier, AUTO, identifies the METAR/SPECI as a fully
  automated report with no human intervention or oversight." A corrected report carries COR in
  place of AUTO.
- Glossary (PDF 80): an *augmented report* is prepared by the automated system with certified
  observers signed on to add information; it has no AUTO.
- 12.7.1.c, p. 12-9 (PDF 67): AO1 or AO2 "shall be coded in all METAR/SPECI from automated
  stations" (AO1 without, AO2 with a precipitation discriminator), augmented or not. So `AO2`
  does not mean "no observer"; `AUTO` does.

FCM-H1-1995 (PDF 56) has the same rules on the body versus Remarks, the visibility condition for
obscurations, and SS/DS intensity. Its text layer is damaged on these pages (a shifted font
encoding), so the "7" and the AUTO definition cannot be read from it by machine; quotes above are
from 2005. The 2019 edition (FCM-H1-2019, published) would cover recent years; not yet obtained.

What this means for DCDR:
- US METAR reports dust in the body only at visibility < 7 statute miles (~11 km); distant dust
  goes to Remarks, which ISD does not decode into weather fields (to check in the raw REM text).
- No w'w' group in a non-AUTO US METAR means no weather at or near the station with visibility
  < 7 mi; DU or HZ at higher visibility is not reported, so it is not "nothing significant" for
  light dust. SYNOP ww 06/07 have no visibility limit.
- A corrected (COR) report from an unattended station may lack AUTO: use the station's other
  reports that hour, or AO1/AO2 plus the absence of augmentation, to decide (to validate).

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
