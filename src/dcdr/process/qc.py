"""Quality-control flags, RH, harmonized visibility and gusts (spec §7.2.3). Never deletes rows.

Valid ranges are the MIN/MAX of each field in the ISD format document
(legacy/isd-format-document.pdf).

What this step adds (process pipeline step 3 of 6: parse -> time -> QC -> classify -> haze/RH -> sample)
-------------------------------------------------------------------------------------------
For each measured variable `v` (wind direction and speed, visibility, temperature, dew point,
sea-level pressure):

* `<v>_flag`  one of "ok", "suspect", "erroneous", "missing", from two sources of evidence:
    1. NOAA's own quality code that comes with each value (e.g. "1" = passed all checks), mapped
       through the config lists `codes_ok` / `codes_suspect` / `codes_erroneous`;
    2. our range check: a value outside the physically possible MIN..MAX is "erroneous",
       whatever its quality code says.
* `use_<v>`   True if the flag is one of the accepted flags (config `qc.accept`, default ["ok"]).
  Downstream code uses a value only where `use_<v>` is True.

Plus derived variables: relative humidity `rh`, visibility capped at 10 km `vis_capped`, and the
strongest valid gust `gust` (with `use_gust` and `gust_src`).

Why flag instead of delete: deleting rows would hide how much data was rejected and why. With
flags, every rejection is visible and countable, and a different QC policy is a config change.
"""

from __future__ import annotations

import polars as pl

from dcdr.config import ParseCfg, QcCfg

# Physically possible range of each variable, in the units produced by parse.py.
RANGES: dict[str, tuple[float, float]] = {
    "wind_dir": (1, 360),  # WIND-OBSERVATION direction angle 001-360
    "wsd": (0, 90),  # WIND-OBSERVATION speed rate 0000-0900 (x10)
    "vis_raw": (0, 160_000),  # VISIBILITY-OBSERVATION distance 000000-160000 m
    "t": (-93.2, 61.8),  # AIR-TEMPERATURE air temperature -0932..+0618 (x10)
    "td": (-98.2, 36.8),  # AIR-TEMPERATURE dew point -0982..+0368 (x10)
    "slp": (860, 1090),  # ATMOSPHERIC-PRESSURE sea level pressure 08600-10900 (x10)
}
# Value column -> the column holding NOAA's quality code for it.
_QC_COL = {
    "wind_dir": "wind_dir_qc",
    "wsd": "wsd_qc",
    "vis_raw": "vis_qc",
    "t": "t_qc",
    "td": "td_qc",
    "slp": "slp_qc",
}
# Value column -> short name used in the output columns (`vis_flag`, `use_vis`, ...).
_FLAG = {"wind_dir": "wind_dir", "wsd": "wsd", "vis_raw": "vis", "t": "t", "td": "td", "slp": "slp"}
OC1_RANGE = (5.0, 110.0)  # WIND-GUST-OBSERVATION speed rate 0050-1100 (x10)
OD_RANGE = (0.0, 200.0)  # SUPPLEMENTARY-WIND-OBSERVATION speed rate 0000-2000 (x10)


def code_flag(code: pl.Expr, cfg: QcCfg) -> pl.Expr:
    """Map an ISD quality-code column to "ok" / "suspect" / "erroneous".

    Unknown or missing codes count as "suspect": we don't trust what we can't interpret.
    `pl.when(...).then(...).when(...)...otherwise(...)` is Polars' vectorised if/elif/else: it is
    evaluated for the whole column at once (fast), not row by row in a Python loop.
    """
    return (
        pl.when(code.is_in(cfg.codes_ok))
        .then(pl.lit("ok"))
        .when(code.is_in(cfg.codes_suspect))
        .then(pl.lit("suspect"))
        .when(code.is_in(cfg.codes_erroneous))
        .then(pl.lit("erroneous"))
        .otherwise(pl.lit("suspect"))
    )


def _value_flag(value: pl.Expr, code: pl.Expr, lo: float, hi: float, cfg: QcCfg) -> pl.Expr:
    """Flag one value: "missing" if absent, "erroneous" if out of range, else from its QC code."""
    return (
        pl.when(value.is_null())
        .then(pl.lit("missing"))
        .when((value < lo) | (value > hi))
        .then(pl.lit("erroneous"))
        .otherwise(code_flag(code, cfg))
    )


def _magnus_es(t: pl.Expr) -> pl.Expr:
    """Saturation vapour pressure (hPa), Alduchov & Eskridge (1996)."""
    return 6.1094 * (17.625 * t / (t + 243.04)).exp()


def apply_qc(df: pl.DataFrame, qc: QcCfg, parse: ParseCfg) -> pl.DataFrame:
    """Add `<v>_flag` and `use_<v>` for every measured variable, plus rh, vis_capped and gust."""
    # 1. One flag column per variable, built in a single pass over the table.
    df = df.with_columns(
        [
            _value_flag(pl.col(v), pl.col(_QC_COL[v]), lo, hi, qc).alias(f"{_FLAG[v]}_flag")
            for v, (lo, hi) in RANGES.items()
        ]
    )
    # 2. Consistency check: the dew point cannot exceed the air temperature (0.5 °C tolerance
    #    for rounding). Only applied when the temperature itself is trustworthy.
    t_ok = pl.col("t_flag").is_in(qc.accept)
    df = df.with_columns(
        td_flag=pl.when(t_ok & (pl.col("td") > pl.col("t") + 0.5))
        .then(pl.lit("erroneous"))
        .otherwise(pl.col("td_flag"))
    )
    # 3. use_<v>: may this value be used downstream?
    df = df.with_columns(
        [pl.col(f"{n}_flag").is_in(qc.accept).alias(f"use_{n}") for n in _FLAG.values()]
    )
    # 4. Relative humidity (%) = vapour pressure at the dew point / saturation vapour pressure.
    rh = 100 * _magnus_es(pl.col("td")) / _magnus_es(pl.col("t"))
    df = df.with_columns(
        rh=pl.when(pl.col("use_t") & pl.col("use_td")).then(rh).otherwise(None),
        # Stations report "unlimited" visibility differently (10 km, 20 km, 50 km, ...), so cap
        # at a common value (default 10 km) to make stations comparable.
        vis_capped=pl.when(pl.col("use_vis"))
        .then(pl.min_horizontal(pl.col("vis_raw"), pl.lit(qc.vis_cap_m)))
        .otherwise(None),
    )
    return _add_gust(df, qc, parse)


def _add_gust(df: pl.DataFrame, qc: QcCfg, parse: ParseCfg) -> pl.DataFrame:
    """Strongest valid gust from OC1 (wind-gust field) and OD1-3 (supplementary wind fields).

    OD fields hold several kinds of wind; only the types listed in the config
    (`parse.gust_od_types`, default 3 and 4) are gusts. `gust_src` records which field won.
    """
    gust_types = [str(t) for t in parse.gust_od_types]

    def valid(spd: str, code: str, rng: tuple[float, float]) -> pl.Expr:
        """Speed present, inside its valid range, and with an accepted QC code."""
        return (
            pl.col(spd).is_not_null()
            & pl.col(spd).is_between(*rng)
            & code_flag(pl.col(code), qc).is_in(qc.accept)
        )

    # Candidate gust values (null where invalid); the row's gust is the largest candidate.
    cands = [pl.when(valid("oc1_spd", "oc1_qc", OC1_RANGE)).then(pl.col("oc1_spd"))]
    for n in range(1, 4):
        ok = valid(f"od{n}_spd", f"od{n}_qc", OD_RANGE) & pl.col(f"od{n}_type").is_in(gust_types)
        cands.append(pl.when(ok).then(pl.col(f"od{n}_spd")))
    df = df.with_columns(gust=pl.max_horizontal(cands))  # max_horizontal ignores nulls
    return df.with_columns(
        use_gust=pl.col("gust").is_not_null(),
        gust_src=pl.when(pl.col("gust").is_null())
        .then(None)
        .when(cands[0] == pl.col("gust"))
        .then(pl.lit("OC1"))
        .otherwise(pl.lit("OD")),
    )
