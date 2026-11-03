"""Process step (bronze → silver): parse, QC and classify ISD reports (spec §7.2).

Bump METHOD_VERSION whenever parse/QC/classify/source-group logic changes; silver files carry it,
and gold refuses to mix versions.

Modules, in the order they run (build.py calls them):
    parse.py     decode ISD's packed text fields; local mean solar time
    qc.py        quality flags, relative humidity, capped visibility, gusts
    classify.py  dust class of each report from its weather codes; haze and humidity rules
    sources.py   source group of each report (SYNOP / METAR / other)
    build.py     run all of the above for one station-year and write the silver file

Version numbers follow MAJOR.MINOR.PATCH: a change that alters results bumps at least MINOR.
Bumping it makes the next run rebuild every silver file, because each file records the version
that built it (see pipeline._silver_is_current).
"""

METHOD_VERSION = "2.0.0"  # 2.0.0: all-reports 3-hour bins, source groups (no slot sampling);
# 1.10.1: slot grid per local month; 1.10.0: off-grid schedules;
# 1.9.0: RH screen on haze classes, remarks dust;
# 1.8.0: METAR fragments (CAVOK), METAR inference, derived SYNOPs;
# 1.7.0: METAR text without the word METAR (2000s) is read;
# 1.6.0: SYNOP is the primary report whenever present;
# 1.5.0: SYNOP and METAR per slot; 1.4.0: METAR rules, vis5;
# 1.3.0: inferred nil (V3); 1.2.0: iX rule
