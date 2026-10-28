"""Derived SYNOPs: is a station-year's SYNOP a copy of its METAR? (user decision 2026-10-28)

Where this fits
---------------
Called by build.py once per station-year, after QC and before classification. If the answer is
yes, classify.py does not use the SYNOPs' present weather (codes and iX): dust and validity then
come from the station's METARs, which hold the actual observation.

Why
---
In the US, SYNOPs are generated from the hourly METAR (US ASOS stations, since about 2005): the
SYNOP's temperature, wind and pressure equal those of the METAR 4-9 minutes earlier, but the dust
does not survive the conversion (after a dusty METAR the SYNOP has dust in 17 % of cases, against
91 % for independent SYNOPs; it often says "nothing significant"). Independent reports agree only
by chance (temperature identical in ~10-23 %). Evidence: notebooks/04 and notebooks/06 (whole
archive: 94.7 % of US station-years with both report types, 0.7 % elsewhere).

Test (settings `classify.derived_*`): pair every SYNOP with the METAR at most `derived_max_minutes`
before it; with at least `derived_min_pairs` pairs, the station-year is derived if temperature is
identical in >= `derived_t_share` of the pairs and wind speed in >= `derived_wind_share`.
"""

from __future__ import annotations

import polars as pl

from dcdr.config import ClassifyCfg

METAR_TYPE = "FM-15"  # the reference: routine METAR


def synop_is_derived(df: pl.DataFrame, cfg: ClassifyCfg) -> bool:
    """True if this station-year's SYNOPs copy its METARs (`df`: parsed and QC'd reports)."""
    if not cfg.derived_synop:
        return False
    cols = ["station_id", "time_utc", "t", "use_t", "wsd", "use_wsd"]
    syn = df.filter(pl.col("report_type").is_in(cfg.derived_report_types)).select(cols)
    met = df.filter(pl.col("report_type") == METAR_TYPE).select(cols)
    if syn.height == 0 or met.height == 0:
        return False
    # For each SYNOP, the latest METAR at or before it, within the time limit. A station-year
    # frame holds one station, so no per-station grouping is needed.
    pairs = syn.sort("time_utc").join_asof(
        met.sort("time_utc").drop("station_id").rename({c: f"{c}_m" for c in cols[2:]}),
        on="time_utc",
        strategy="backward",
        tolerance=f"{cfg.derived_max_minutes}m",
    )
    pairs = pairs.filter(pl.col("use_t_m").is_not_null())  # a METAR was found
    if pairs.height < cfg.derived_min_pairs:
        return False
    t_ok = pairs.filter(pl.col("use_t") & pl.col("use_t_m"))
    w_ok = pairs.filter(pl.col("use_wsd") & pl.col("use_wsd_m"))
    if t_ok.height == 0 or w_ok.height == 0:
        return False
    t_same = t_ok.select((pl.col("t") == pl.col("t_m")).mean()).item()
    w_same = w_ok.select((pl.col("wsd") == pl.col("wsd_m")).mean()).item()
    return bool(t_same >= cfg.derived_t_share and w_same >= cfg.derived_wind_share)
