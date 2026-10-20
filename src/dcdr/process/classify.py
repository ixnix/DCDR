"""Per-field dust classification, haze rule and RH screen (spec §7.2.4-7.2.6).

Each ISD present-weather field has its OWN code table (ISD format document):
MW = WMO 4677 (manual), AW = WMO 4680 (automated; 30-35 are FOG), AU = METAR components.

The idea in one paragraph
-------------------------
A report can carry several weather fields at once (MW1..MW7, AU1..AU9, AW1..AW4). Each field's code
is translated, with that field's own code table, into a dust class with a severity RANK (none 0 <
haze 1 < ... < storm with thunder 8). The report's class is the most severe class found in any of
its fields (the maximum rank).

When a report carries NO usable weather field, the SYNOP iX indicator (WMO code table 1860, read
from the raw message in REM) decides (user decision 2026-10-19, refining 2026-10-17):
    iX 2 or 5   group omitted, no significant phenomenon -> observed: class "none"
    iX 3 or 6   group omitted, no observation            -> not observed: class "unknown"
    no SYNOP text (BUFR-only) or no iX: a SYNOP with visibility reported at a station that
                passes the guard -> inferred nil: class "none" (pw_status "nil_inferred")
    otherwise                                              -> not observed: class "unknown"
A missing weather field is never ASSUMED to mean "no significant weather"; only an explicit
iX 2/5 says so. iX 7 also selects the code table: automatic station, WMO tables 4680/4531.

METAR/SPECI (FM-15/16) follow their own code rules, read from the raw message (user decisions
2026-10-20, validated in notebooks/03; full table in docs/science.md 5.2):
    AUTO (fully automated) or "//"    -> not observed; their codes are not used (machines do not
                                         detect dust; ASOS infers HZ from visibility and humidity)
    CAVOK                             -> observed, nothing significant ("nil_significant")
    no weather group, visibility > 5 km -> nothing significant, implied ("nil_metar")
    no weather group, visibility <= 5 km or missing -> not observed (something went unnamed)
    a weather group ISD did not decode -> not observed
A METAR only fills a synoptic slot when no SYNOP does (sampling priority).

Columns added (process pipeline steps 4-5 of 6)
----------------------------------------------
classify_reports:  has_pw, pw_status ("reported" / "nil_significant" / "nil_inferred" / "nil_metar" /
                   "not_observed"), dust_rank, dust_class_rule, dust_rule_id, pw_evidence,
                   flag_du / flag_susp / flag_bd / flag_ds (convenience flags from the rank),
                   flag_vicinity, flag_past, flag_ambiguous, flag_daily_dust (extra evidence that
                   does NOT change the class)
apply_haze_and_rh: dust_haze, flag_duhz, rh_screened, dust_class_final (the class used downstream)

Polars note: most code below builds expressions (pl.Expr) per field and combines them with
`max_horizontal` / `any_horizontal` (row-wise max / OR across columns). Nothing is computed until
the expressions are passed to `with_columns`, where Polars evaluates them for all rows at once.
"""

from __future__ import annotations

import polars as pl

from dcdr.config import ClassifyCfg, QcCfg
from dcdr.process.parse import AT_N, AU_N, AW_N, AX_N, AY_N, AZ_N, MV_N, MW_N
from dcdr.process.qc import code_flag

# Severity ranks: when a report has several dust codes, the highest rank wins.
RANK: dict[str, int] = {
    "none": 0,
    "haze": 1,
    "haze_or_suspended": 2,  # automated AW 04 can't tell haze from suspended dust
    "suspended": 3,  # dust in the air, not raised at the station (often transported)
    "blowing": 4,  # dust or sand raised by wind at the station
    "whirls": 5,  # dust or sand whirls (dust devils)
    "storm": 6,  # duststorm or sandstorm
    "storm_severe": 7,
    "storm_thunder": 8,  # thunderstorm combined with duststorm/sandstorm
}
CLASS_BY_RANK: dict[int, str] = {v: k for k, v in RANK.items()}  # reverse lookup: 3 -> "suspended"
# Every value `dust_class_final` may take: the ranked classes plus three special ones (see
# apply_haze_and_rh for "dust_haze" and "screened"; "unknown" = no weather observed).
FINAL_CLASSES = [*RANK, "dust_haze", "screened", "unknown"]
# Classes that make a day "dusty" in gold (haze alone does not, unless linked to recent dust).
DUSTY_CLASSES = [
    "suspended",
    "blowing",
    "whirls",
    "storm",
    "storm_severe",
    "storm_thunder",
    "dust_haze",
]

# MW (manual present weather, WMO table 4677): code -> class.
MW_TABLE = {
    "05": "haze",
    "06": "suspended",  # widespread dust in suspension, not raised at or near the station
    "07": "blowing",  # dust or sand raised by wind at or near the station
    "08": "whirls",
    "09": "storm",  # duststorm or sandstorm within sight, or during the preceding hour
    "30": "storm",
    "31": "storm",
    "32": "storm",
    "33": "storm_severe",
    "34": "storm_severe",
    "35": "storm_severe",
    "98": "storm_thunder",
}
# AW = ISD's automated present-weather field. Two sources, two code tables:
#  * AW from ISD's own automated observations follows the ISD format document's AW list, where
#    05 = smoke and 07 = dust or sand raised by wind;
#  * AW from a SYNOP report with iX = 7 follows WMO code table 4680, which has no 06-09 and where
#    05 = haze, smoke or dust in suspension, visibility < 1 km.
# In both, 30-35 are fog (rank 0).
AW_TABLE = {"04": "haze_or_suspended", "07": "blowing"}  # ISD document AW list
WMO_4680_TABLE = {"04": "haze_or_suspended", "05": "haze_or_suspended"}  # SYNOP with iX = 7
# AW codes that mean blowing or drifting snow OR sand: which one is decided by temperature.
AW_SNOW_OR_SAND = ["27", "28", "29"]
MV_VICINITY = ["03", "04", "05", "07", "08"]  # MV codes for dust-related weather near the station
MW_SNOW = [str(c) for c in range(70, 80)]  # MW 70-79: snow falling (rules out the "sand" reading)
AT_DUST = ["07"]  # daily present weather: dust, volcanic ash, blowing dust/sand or obstruction
AX_DUST = ["07"]  # daily past weather: blowing dust or sand, visibility <= 1/2 mile
AY_DUST_OR_SNOW = ["3"]  # WMO 4561 past weather: sandstorm, duststorm or blowing snow
AZ_BLOWING = ["2"]  # WMO 4531 automated past weather: blowing phenomena, visibility reduced


def _accepted(qc_col: str, qc: QcCfg) -> pl.Expr:
    """True where the field's QC code maps to an accepted flag (default: "ok" only)."""
    return code_flag(pl.col(qc_col), qc).is_in(qc.accept)


def _table_rank(code_col: str, table: dict[str, str]) -> pl.Expr:
    """Rank of a code column via a code table; codes not in the table rank 0 (no dust)."""
    mapping = {k: RANK[v] for k, v in table.items()}  # e.g. {"06": 3, "07": 4, ...}
    return pl.col(code_col).replace_strict(mapping, default=0, return_dtype=pl.Int8)


def _au_rank(n: int) -> pl.Expr:
    """Dust rank of the n-th AU (METAR-style) group, built from its components.

    METAR weather is written as combinations such as "+DS" (heavy duststorm), "BLDU" (blowing
    dust) or "VCSS" (sandstorm in the vicinity). ISD stores each part as a digit code:
        int   intensity      3 = heavy ("+"), 4 = in the vicinity ("VC")
        desc  descriptor     4/5 = low drifting/blowing ("DR"/"BL"), 7 = thunderstorm ("TS")
        obsc  obscuration    5/6 = dust/sand ("DU"/"SA"), 7 = haze ("HZ")
        other other weather  1 = dust/sand whirls ("PO"), 4/5 = sandstorm/duststorm ("SS"/"DS")
    The checks run from most to least severe; the first one that matches decides.
    """
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
    """The AU group's components joined as text (e.g. "3/9/9/9/5/9"), for the evidence list."""
    return pl.concat_str(
        [pl.col(f"au{n}_{p}") for p in ("int", "desc", "prec", "obsc", "other")], separator="/"
    )


def classify_reports(
    df: pl.DataFrame, cfg: ClassifyCfg, qc: QcCfg, *, nil_guard: bool | None = None
) -> pl.DataFrame:
    """Give every report a dust rank and class from its weather fields, plus evidence flags."""
    # Step 1: which numbered fields hold a usable code (present AND accepted by QC)?
    # One boolean expression per field number, e.g. mw_ok[1] is "MW1 is usable".
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
    az_ok = {
        n: pl.col(f"az{n}_code").is_not_null() & _accepted(f"az{n}_qc", qc)
        for n in range(1, AZ_N + 1)
    }
    ax_ok = {
        n: pl.col(f"ax{n}_code").is_not_null() & _accepted(f"ax{n}_qc", qc)
        for n in range(1, AX_N + 1)
    }
    at_ok = {
        n: pl.col(f"at{n}_wea").is_not_null() & _accepted(f"at{n}_qc", qc)
        for n in range(1, AT_N + 1)
    }

    # Step 2: snow or sand? Some codes mean "blowing snow OR sand". Decide with the weather:
    # _snow   the same report also reports falling snow (MW 70-79 or AU precipitation SN)
    # _sand   temperature is reliably >= sand_min_t_c (default 3 °C) and no snow reported
    # _snowt  temperature is reliably <= snow_max_t_c (default 0 °C)
    # Between the two thresholds neither applies, and the code is flagged ambiguous (step 5).
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

    # Step 3: dust rank of every usable field (null where the field is not usable).
    # A SYNOP with iX = 7 comes from an automatic station using WMO table 4680, so its weather codes
    # are read with that table, whichever field ISD stored them in (in practice: AW).
    ix = pl.col("syn_ix")
    ix7 = (ix == "7").fill_null(False) if cfg.synop_ix else pl.lit(False)

    def rank_4680(code_col: str) -> pl.Expr:
        """Rank under WMO 4680; 27-29 ("blowing snow or sand") count as dust only when warm."""
        return (
            pl.when(pl.col(code_col).is_in(AW_SNOW_OR_SAND) & pl.col("_sand"))
            .then(pl.lit(RANK["blowing"], dtype=pl.Int8))
            .otherwise(_table_rank(code_col, WMO_4680_TABLE))
        )

    mw_rank = {
        n: pl.when(ok & ix7)
        .then(rank_4680(f"mw{n}_code"))
        .when(ok)
        .then(_table_rank(f"mw{n}_code", MW_TABLE))
        .cast(pl.Int8)
        for n, ok in mw_ok.items()
    }
    aw_rank = {
        # AW 27-29 count as blowing dust/sand only when it is warm (_sand); otherwise the table.
        n: pl.when(ok & ix7)
        .then(rank_4680(f"aw{n}_code"))
        .when(ok & pl.col(f"aw{n}_code").is_in(AW_SNOW_OR_SAND) & pl.col("_sand"))
        .then(pl.lit(RANK["blowing"]))
        .when(ok)
        .then(_table_rank(f"aw{n}_code", AW_TABLE))
        .cast(pl.Int8)
        for n, ok in aw_ok.items()
    }
    au_raw = {n: _au_rank(n) for n in range(1, AU_N + 1)}
    # Dust "in the vicinity" (VC, intensity 4) is not dust AT the station: it doesn't count for
    # the rank, but is kept as evidence and in flag_vicinity.
    au_vic = {n: au_ok[n] & (pl.col(f"au{n}_int") == "4") & (au_raw[n] > 0) for n in au_raw}
    au_rank = {n: pl.when(au_ok[n] & ~au_vic[n]).then(au_raw[n]).cast(pl.Int8) for n in au_raw}

    # Step 4: combine. Per field family, the highest rank of its numbered fields ...
    fam_rank = {
        "MW": pl.max_horizontal(list(mw_rank.values())),
        "AU": pl.max_horizontal(list(au_rank.values())),
        "AW": pl.max_horizontal(list(aw_rank.values())),
    }
    # ... and whether the family has any usable field at all.
    fam_has = {
        "MW": pl.any_horizontal(list(mw_ok.values())),
        "AU": pl.any_horizontal(list(au_ok.values())),
        "AW": pl.any_horizontal(list(aw_ok.values())),
    }
    fields = list(cfg.fields)  # which families count (config classify.fields)
    # METAR/SPECI rules (see module docstring). `metar_unusable`: an AUTO report or one whose
    # weather is "//" -- its codes are not used at all, so it can be neither dust nor haze.
    if cfg.metar_rules:
        is_metar = (
            pl.col("report_type").is_in(cfg.metar_report_types).fill_null(False)
            & pl.col("metar_wx").is_not_null()
        )
        wx = pl.col("metar_wx")
        auto = is_metar & pl.col("metar_auto").fill_null(False)
        metar_unusable = auto | (is_metar & (wx == "missing"))
        metar_cavok = is_metar & ~auto & (wx == "cavok")
        metar_nil = (
            is_metar
            & ~auto
            & (wx == "none")
            & pl.col("use_vis").fill_null(False)
            & (pl.col("vis_raw") > cfg.metar_nil_min_vis_m).fill_null(False)
        )
    else:
        metar_unusable = metar_cavok = metar_nil = pl.lit(False)
    # reported: the report carries at least one usable present-weather code.
    reported = pl.any_horizontal([fam_has[f] for f in fields]).fill_null(False) & ~metar_unusable
    # nil_significant: a SYNOP whose iX says the weather group was omitted because NOTHING
    # SIGNIFICANT happened (iX 2 = manned, 5 = automatic; WMO code table 1860). That is an
    # observation of "no significant weather", so no dust. iX 3/6 (omitted: no observation) and
    # reports without SYNOP text (METAR, BUFR-only) stay "not observed" (user decision 2026-10-19).
    nil = ix.is_in(["2", "5"]).fill_null(False) if cfg.synop_ix else pl.lit(False)
    # nil_inferred (rule V3, user decision 2026-10-20; validated in notebooks/02): a SYNOP report
    # with no code AND no iX whose visibility was reported (good QC) is inferred "nothing
    # significant", if its station passes the guard (nil_guard, computed from the station's iX
    # years by process/guard.py). In SYNOP, visibility and iX share one group, so a reported
    # visibility shows an observation was made. Kept apart from the explicit nil_significant.
    inferred = (
        ~reported
        & ix.is_null()
        & pl.col("use_vis").fill_null(False)
        & pl.col("report_type").is_in(cfg.infer_nil_report_types).fill_null(False)
        if cfg.infer_nil and nil_guard
        else pl.lit(False)
    )
    # METAR: CAVOK is an explicit "nothing significant" (WMO FM 15.10), like iX 2/5. A missing
    # group is only implied (FM 15.8.2) and kept apart as "nil_metar".
    nil = nil | (metar_cavok & ~reported)
    metar_nil = metar_nil & ~reported
    # has_pw: weather was observed at this report (a code, an explicit or an inferred "nothing").
    has_pw = reported | nil | inferred | metar_nil
    pw_status = (
        pl.when(reported)
        .then(pl.lit("reported"))
        .when(nil)
        .then(pl.lit("nil_significant"))
        .when(inferred)
        .then(pl.lit("nil_inferred"))
        .when(metar_nil)
        .then(pl.lit("nil_metar"))
        .otherwise(pl.lit("not_observed"))
    )
    rank = (
        pl.when(metar_unusable)
        .then(pl.lit(0))
        .otherwise(pl.max_horizontal([fam_rank[f] for f in fields]))
        .fill_null(0)
        .cast(pl.Int8)
    )
    cls = rank.replace_strict(CLASS_BY_RANK, return_dtype=pl.String)

    # dust_rule_id: which family produced the winning class, e.g. "MW:blowing" (null if none).
    rule = pl.lit(None, dtype=pl.String)
    for f in reversed(fields):  # first field in `fields` order wins ties
        rule = (
            pl.when((fam_rank[f] == rank) & (rank > 0)).then(pl.lit(f"{f}:") + cls).otherwise(rule)
        )

    # Step 5: evidence. A list of every dust-related code found in the report, as readable text
    # ("MW1=06", "AU2=3/9/9/9/5/9", "AX1=07", ...). Each element is null when its condition is
    # false; the nulls are dropped below, leaving only the codes actually present.
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
        evidence.append(
            pl.when(ok & pl.col(f"ay{n}_code").is_in(AY_DUST_OR_SNOW)).then(pl.lit(f"AY{n}=3"))
        )
    for n, ok in az_ok.items():
        evidence.append(
            pl.when(ok & pl.col(f"az{n}_code").is_in(AZ_BLOWING)).then(pl.lit(f"AZ{n}=2"))
        )
    for n, ok in ax_ok.items():
        evidence.append(
            pl.when(ok & pl.col(f"ax{n}_code").is_in(AX_DUST)).then(pl.lit(f"AX{n}=07"))
        )
    for n, ok in at_ok.items():
        evidence.append(pl.when(ok & pl.col(f"at{n}_wea").is_in(AT_DUST)).then(pl.lit(f"AT{n}=07")))

    # Extra evidence flags. These do NOT change the report's class; gold uses them separately.
    # Dust near (not at) the station: AU with "VC", or a dust-related MV code.
    vicinity = (
        pl.any_horizontal(
            list(au_vic.values())
            + [ok & pl.col(f"mv{n}_code").is_in(MV_VICINITY) for n, ok in mv_ok.items()]
        ).fill_null(False)
        & ~metar_unusable
    )
    # Past weather (AY 3, AZ 2) can be snow or sand: resolved by temperature like AW 27-29.
    past_codes = [ok & pl.col(f"ay{n}_code").is_in(AY_DUST_OR_SNOW) for n, ok in ay_ok.items()] + [
        ok & pl.col(f"az{n}_code").is_in(AZ_BLOWING) for n, ok in az_ok.items()
    ]
    past = pl.any_horizontal([p & pl.col("_sand") for p in past_codes]).fill_null(False)
    # Ambiguous: a snow-or-sand code where the temperature allows neither reading.
    neither = ~pl.col("_sand") & ~pl.col("_snowt")
    ambiguous = pl.any_horizontal(
        [ok & pl.col(f"aw{n}_code").is_in(AW_SNOW_OR_SAND) & neither for n, ok in aw_ok.items()]
        + [p & neither for p in past_codes]
    ).fill_null(False)
    # Daily summaries (AT/AX, in SOD rows) are day-level evidence, not present-weather observations.
    daily_dust = pl.any_horizontal(
        [ok & pl.col(f"at{n}_wea").is_in(AT_DUST) for n, ok in at_ok.items()]
        + [ok & pl.col(f"ax{n}_code").is_in(AX_DUST) for n, ok in ax_ok.items()]
    ).fill_null(False)

    df = df.with_columns(
        has_pw=has_pw,
        pw_status=pw_status,
        dust_rank=rank,
        dust_rule_id=rule,
        pw_evidence=pl.concat_list(evidence).list.drop_nulls(),
        flag_vicinity=vicinity,
        flag_past=past,
        flag_ambiguous=ambiguous,
        flag_daily_dust=daily_dust,
        _cls=cls,
    )
    r = pl.col("dust_rank")
    return df.with_columns(
        # Not observed -> "unknown". "none" needs a reported code or an explicit iX "nothing".
        dust_class_rule=pl.when(pl.col("has_pw")).then(pl.col("_cls")).otherwise(pl.lit("unknown")),
        flag_du=(r >= RANK["suspended"]),  # any dust (suspended or stronger)
        flag_susp=(r == RANK["suspended"]),
        flag_bd=r.is_in([RANK["blowing"], RANK["whirls"]]),  # raised at the station
        flag_ds=(r >= RANK["storm"]),  # duststorm of any kind
    ).drop("_snow", "_sand", "_snowt", "_cls")  # temporary helper columns


def apply_haze_and_rh(df: pl.DataFrame, cfg: ClassifyCfg, qc: QcCfg) -> pl.DataFrame:
    """Haze rule (window (t-24h, t], same station, unscreened dust) then RH screen.

    `df` must hold one station and be sorted by `time_utc`.

    1. Haze rule: haze on its own is not counted as dust (it can be smoke or pollution). But a
       haze report within 24 hours AFTER a dust report at the same station is most likely the
       dust lingering, so it becomes "dust_haze", which counts as dusty.
    2. RH screen: in very humid air (RH above qc.rh_screen_pct, default 90 %) haze-like classes
       are probably humidity haze or mist, not dust. Those reports become "screened".
       Only the classes in qc.rh_screen_classes are screened; blowing dust and storms are not.
    """
    haze_like = pl.col("dust_class_rule").is_in(["haze", "haze_or_suspended"])
    if cfg.haze_window_h > 0:
        # Was there any dust report in the window (t - 24 h, t]? A rolling max of the 0/1 dust
        # flag over the last 24 hours of `time_utc`; "right" closed = includes t, excludes t-24h.
        recent = (
            pl.col("flag_du")
            .cast(pl.Int8)
            .rolling_max_by("time_utc", window_size=f"{cfg.haze_window_h}h", closed="right")
            == 1
        )
        dust_haze = (haze_like & recent).fill_null(False)
    else:
        dust_haze = pl.lit(False)  # window 0 switches the haze rule off
    df = df.with_columns(dust_haze=dust_haze)
    df = df.with_columns(flag_duhz=pl.col("flag_du") | pl.col("dust_haze"))
    # Class before the humidity screen: "dust_haze" replaces the haze class where the rule fired.
    pre = (
        pl.when(pl.col("dust_haze")).then(pl.lit("dust_haze")).otherwise(pl.col("dust_class_rule"))
    )
    screened = (
        pl.col("rh").is_not_null()
        & (pl.col("rh") > qc.rh_screen_pct)
        & pre.is_in(qc.rh_screen_classes)
    ).fill_null(False)
    df = df.with_columns(rh_screened=screened)
    return df.with_columns(
        dust_class_final=pl.when(pl.col("rh_screened")).then(pl.lit("screened")).otherwise(pre)
    )
