"""Quality-control flags, RH, harmonized visibility and gusts (spec §7.2.3). Never deletes rows.

Valid ranges are the MIN/MAX of each field in the ISD format document
(legacy/isd-format-document.pdf).
"""

from __future__ import annotations

import polars as pl

from dcdr.config import ParseCfg, QcCfg

RANGES: dict[str, tuple[float, float]] = {
    "wind_dir": (1, 360),  # WIND-OBSERVATION direction angle 001-360
    "wsd": (0, 90),  # WIND-OBSERVATION speed rate 0000-0900 (x10)
    "vis_raw": (0, 160_000),  # VISIBILITY-OBSERVATION distance 000000-160000 m
    "t": (-93.2, 61.8),  # AIR-TEMPERATURE air temperature -0932..+0618 (x10)
    "td": (-98.2, 36.8),  # AIR-TEMPERATURE dew point -0982..+0368 (x10)
    "slp": (860, 1090),  # ATMOSPHERIC-PRESSURE sea level pressure 08600-10900 (x10)
}
_QC_COL = {
    "wind_dir": "wind_dir_qc",
    "wsd": "wsd_qc",
    "vis_raw": "vis_qc",
    "t": "t_qc",
    "td": "td_qc",
    "slp": "slp_qc",
}
_FLAG = {"wind_dir": "wind_dir", "wsd": "wsd", "vis_raw": "vis", "t": "t", "td": "td", "slp": "slp"}
OC1_RANGE = (5.0, 110.0)  # WIND-GUST-OBSERVATION speed rate 0050-1100 (x10)
OD_RANGE = (0.0, 200.0)  # SUPPLEMENTARY-WIND-OBSERVATION speed rate 0000-2000 (x10)


def code_flag(code: pl.Expr, cfg: QcCfg) -> pl.Expr:
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
    df = df.with_columns(
        [
            _value_flag(pl.col(v), pl.col(_QC_COL[v]), lo, hi, qc).alias(f"{_FLAG[v]}_flag")
            for v, (lo, hi) in RANGES.items()
        ]
    )
    t_ok = pl.col("t_flag").is_in(qc.accept)
    df = df.with_columns(
        td_flag=pl.when(t_ok & (pl.col("td") > pl.col("t") + 0.5))
        .then(pl.lit("erroneous"))
        .otherwise(pl.col("td_flag"))
    )
    df = df.with_columns(
        [pl.col(f"{n}_flag").is_in(qc.accept).alias(f"use_{n}") for n in _FLAG.values()]
    )
    rh = 100 * _magnus_es(pl.col("td")) / _magnus_es(pl.col("t"))
    df = df.with_columns(
        rh=pl.when(pl.col("use_t") & pl.col("use_td")).then(rh).otherwise(None),
        vis_capped=pl.when(pl.col("use_vis"))
        .then(pl.min_horizontal(pl.col("vis_raw"), pl.lit(qc.vis_cap_m)))
        .otherwise(None),
    )
    return _add_gust(df, qc, parse)


def _add_gust(df: pl.DataFrame, qc: QcCfg, parse: ParseCfg) -> pl.DataFrame:
    gust_types = [str(t) for t in parse.gust_od_types]

    def valid(spd: str, code: str, rng: tuple[float, float]) -> pl.Expr:
        return (
            pl.col(spd).is_not_null()
            & pl.col(spd).is_between(*rng)
            & code_flag(pl.col(code), qc).is_in(qc.accept)
        )

    cands = [pl.when(valid("oc1_spd", "oc1_qc", OC1_RANGE)).then(pl.col("oc1_spd"))]
    for n in range(1, 4):
        ok = valid(f"od{n}_spd", f"od{n}_qc", OD_RANGE) & pl.col(f"od{n}_type").is_in(gust_types)
        cands.append(pl.when(ok).then(pl.col(f"od{n}_spd")))
    df = df.with_columns(gust=pl.max_horizontal(cands))
    return df.with_columns(
        use_gust=pl.col("gust").is_not_null(),
        gust_src=pl.when(pl.col("gust").is_null())
        .then(None)
        .when(cands[0] == pl.col("gust"))
        .then(pl.lit("OC1"))
        .otherwise(pl.lit("OD")),
    )
