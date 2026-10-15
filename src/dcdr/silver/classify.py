"""Per-field dust classification, haze rule and RH screen (spec §7.2.4-7.2.6).

Each ISD present-weather field has its OWN code table (ISD format document):
MW = WMO 4677 (manual), AW = WMO 4680 (automated; 30-35 are FOG), AU = METAR components.
"""

from __future__ import annotations

import polars as pl

from dcdr.config import ClassifyCfg, QcCfg
from dcdr.silver.parse import AU_N, AW_N, AY_N, MV_N, MW_N
from dcdr.silver.qc import code_flag

RANK: dict[str, int] = {
    "none": 0,
    "haze": 1,
    "haze_or_suspended": 2,
    "suspended": 3,
    "blowing": 4,
    "whirls": 5,
    "storm": 6,
    "storm_severe": 7,
    "storm_thunder": 8,
}
CLASS_BY_RANK: dict[int, str] = {v: k for k, v in RANK.items()}
FINAL_CLASSES = [*RANK, "dust_haze", "screened", "unknown"]
DUSTY_CLASSES = [
    "suspended",
    "blowing",
    "whirls",
    "storm",
    "storm_severe",
    "storm_thunder",
    "dust_haze",
]

MW_TABLE = {
    "05": "haze",
    "06": "suspended",
    "07": "blowing",
    "08": "whirls",
    "09": "storm",
    "30": "storm",
    "31": "storm",
    "32": "storm",
    "33": "storm_severe",
    "34": "storm_severe",
    "35": "storm_severe",
    "98": "storm_thunder",
}
AW_TABLE = {"04": "haze_or_suspended", "07": "blowing"}  # 30-35 are fog -> rank 0
AW_SNOW_OR_SAND = ["27", "28", "29"]
MV_VICINITY = ["03", "04", "05", "07", "08"]
MW_SNOW = [str(c) for c in range(70, 80)]


def _accepted(qc_col: str, qc: QcCfg) -> pl.Expr:
    return code_flag(pl.col(qc_col), qc).is_in(qc.accept)


def _table_rank(code_col: str, table: dict[str, str]) -> pl.Expr:
    mapping = {k: RANK[v] for k, v in table.items()}
    return pl.col(code_col).replace_strict(mapping, default=0, return_dtype=pl.Int8)


def _au_rank(n: int) -> pl.Expr:
    i, de, ob, ot = (pl.col(f"au{n}_{p}") for p in ("int", "desc", "obsc", "other"))
    storm = ot.is_in(["4", "5"])
    dust_obsc = ob.is_in(["5", "6"])
    return (
        pl.when(storm & (de == "7"))
        .then(pl.lit(RANK["storm_thunder"]))
        .when(storm & (i == "3"))
        .then(pl.lit(RANK["storm_severe"]))
        .when(storm)
        .then(pl.lit(RANK["storm"]))
        .when(ot == "1")
        .then(pl.lit(RANK["whirls"]))
        .when(dust_obsc & de.is_in(["4", "5"]))
        .then(pl.lit(RANK["blowing"]))
        .when(dust_obsc)
        .then(pl.lit(RANK["suspended"]))
        .when(ob == "7")
        .then(pl.lit(RANK["haze"]))
        .otherwise(pl.lit(0))
        .cast(pl.Int8)
    )


def _au_code(n: int) -> pl.Expr:
    return pl.concat_str(
        [pl.col(f"au{n}_{p}") for p in ("int", "desc", "prec", "obsc", "other")], separator="/"
    )


def classify_reports(df: pl.DataFrame, cfg: ClassifyCfg, qc: QcCfg) -> pl.DataFrame:
    mw_ok = {
        n: pl.col(f"mw{n}_code").is_not_null() & _accepted(f"mw{n}_qc", qc)
        for n in range(1, MW_N + 1)
    }
    au_ok = {
        n: pl.col(f"au{n}_obsc").is_not_null() & _accepted(f"au{n}_qc", qc)
        for n in range(1, AU_N + 1)
    }
    aw_ok = {
        n: pl.col(f"aw{n}_code").is_not_null() & _accepted(f"aw{n}_qc", qc)
        for n in range(1, AW_N + 1)
    }
    mv_ok = {
        n: pl.col(f"mv{n}_code").is_not_null() & _accepted(f"mv{n}_qc", qc)
        for n in range(1, MV_N + 1)
    }
    ay_ok = {
        n: pl.col(f"ay{n}_code").is_not_null() & _accepted(f"ay{n}_qc", qc)
        for n in range(1, AY_N + 1)
    }

    snow = pl.any_horizontal(
        [ok & pl.col(f"mw{n}_code").is_in(MW_SNOW) for n, ok in mw_ok.items()]
        + [ok & (pl.col(f"au{n}_prec") == "03") for n, ok in au_ok.items()]
    ).fill_null(False)
    df = df.with_columns(_snow=snow)
    df = df.with_columns(
        _sand=(pl.col("use_t") & (pl.col("t") >= cfg.sand_min_t_c) & ~pl.col("_snow")).fill_null(
            False
        ),
        _snowt=(pl.col("use_t") & (pl.col("t") <= cfg.snow_max_t_c)).fill_null(False),
    )

    mw_rank = {
        n: pl.when(ok).then(_table_rank(f"mw{n}_code", MW_TABLE)).cast(pl.Int8)
        for n, ok in mw_ok.items()
    }
    aw_rank = {
        n: pl.when(ok & pl.col(f"aw{n}_code").is_in(AW_SNOW_OR_SAND) & pl.col("_sand"))
        .then(pl.lit(RANK["blowing"]))
        .when(ok)
        .then(_table_rank(f"aw{n}_code", AW_TABLE))
        .cast(pl.Int8)
        for n, ok in aw_ok.items()
    }
    au_raw = {n: _au_rank(n) for n in range(1, AU_N + 1)}
    au_vic = {n: au_ok[n] & (pl.col(f"au{n}_int") == "4") & (au_raw[n] > 0) for n in au_raw}
    au_rank = {n: pl.when(au_ok[n] & ~au_vic[n]).then(au_raw[n]).cast(pl.Int8) for n in au_raw}

    fam_rank = {
        "MW": pl.max_horizontal(list(mw_rank.values())),
        "AU": pl.max_horizontal(list(au_rank.values())),
        "AW": pl.max_horizontal(list(aw_rank.values())),
    }
    fam_has = {
        "MW": pl.any_horizontal(list(mw_ok.values())),
        "AU": pl.any_horizontal(list(au_ok.values())),
        "AW": pl.any_horizontal(list(aw_ok.values())),
    }
    fields = list(cfg.fields)
    has_pw = pl.any_horizontal([fam_has[f] for f in fields]).fill_null(False)
    rank = pl.max_horizontal([fam_rank[f] for f in fields]).fill_null(0).cast(pl.Int8)
    cls = rank.replace_strict(CLASS_BY_RANK, return_dtype=pl.String)

    rule = pl.lit(None, dtype=pl.String)
    for f in reversed(fields):  # first field in `fields` order wins ties
        rule = (
            pl.when((fam_rank[f] == rank) & (rank > 0)).then(pl.lit(f"{f}:") + cls).otherwise(rule)
        )

    evidence: list[pl.Expr] = []
    for n, r in mw_rank.items():
        evidence.append(pl.when(r > 0).then(pl.lit(f"MW{n}=") + pl.col(f"mw{n}_code")))
    for n, r in au_rank.items():
        evidence.append(pl.when((r > 0) | au_vic[n]).then(pl.lit(f"AU{n}=") + _au_code(n)))
    for n, r in aw_rank.items():
        amb = aw_ok[n] & pl.col(f"aw{n}_code").is_in(AW_SNOW_OR_SAND)
        evidence.append(pl.when((r > 0) | amb).then(pl.lit(f"AW{n}=") + pl.col(f"aw{n}_code")))
    for n, ok in mv_ok.items():
        evidence.append(
            pl.when(ok & pl.col(f"mv{n}_code").is_in(MV_VICINITY)).then(
                pl.lit(f"MV{n}=") + pl.col(f"mv{n}_code")
            )
        )
    for n, ok in ay_ok.items():
        evidence.append(pl.when(ok & (pl.col(f"ay{n}_code") == "3")).then(pl.lit(f"AY{n}=3")))

    vicinity = pl.any_horizontal(
        list(au_vic.values())
        + [ok & pl.col(f"mv{n}_code").is_in(MV_VICINITY) for n, ok in mv_ok.items()]
    ).fill_null(False)
    past = pl.any_horizontal(
        [ok & (pl.col(f"ay{n}_code") == "3") & pl.col("_sand") for n, ok in ay_ok.items()]
    ).fill_null(False)
    neither = ~pl.col("_sand") & ~pl.col("_snowt")
    ambiguous = pl.any_horizontal(
        [ok & pl.col(f"aw{n}_code").is_in(AW_SNOW_OR_SAND) & neither for n, ok in aw_ok.items()]
        + [ok & (pl.col(f"ay{n}_code") == "3") & neither for n, ok in ay_ok.items()]
    ).fill_null(False)

    df = df.with_columns(
        has_pw=has_pw,
        dust_rank=rank,
        dust_rule_id=rule,
        pw_evidence=pl.concat_list(evidence).list.drop_nulls(),
        flag_vicinity=vicinity,
        flag_past=past,
        flag_ambiguous=ambiguous,
        _cls=cls,
    )
    r = pl.col("dust_rank")
    return df.with_columns(
        dust_class_rule=pl.when(pl.col("has_pw")).then(pl.col("_cls")).otherwise(pl.lit("unknown")),
        flag_du=(r >= RANK["suspended"]),
        flag_susp=(r == RANK["suspended"]),
        flag_bd=r.is_in([RANK["blowing"], RANK["whirls"]]),
        flag_ds=(r >= RANK["storm"]),
    ).drop("_snow", "_sand", "_snowt", "_cls")
