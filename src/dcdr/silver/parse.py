"""Decode ISD packed fields into typed columns and add time columns (spec §7.2.1-7.2.2).

Field layouts, missing-value codes and scale factors follow the ISD format document
(legacy/isd-format-document.pdf): e.g. WND speed 9999 = missing, scale 10; VIS 999999;
TMP/DEW +9999, scale 10; SLP 99999, scale 10; OC1/OD speed 9999, scale 10; AA depth 9999,
scale 10; AJ1 depth 9999 (cm); AY period 99.
"""

from __future__ import annotations

import polars as pl

MW_N, AU_N, AW_N, MV_N, AY_N, OD_N, AA_N = 7, 9, 4, 7, 2, 3, 4
PARSE_BITS = {"WND": 1, "VIS": 2, "TMP": 4, "DEW": 8, "SLP": 16}
_AU_PARTS = ("int", "desc", "prec", "obsc", "other", "comb", "qc")


def _raw(df: pl.DataFrame, col: str) -> pl.Expr:
    return pl.col(col) if col in df.columns else pl.lit(None, dtype=pl.String)


def _part(df: pl.DataFrame, col: str, i: int) -> pl.Expr:
    return _raw(df, col).str.split(",").list.get(i, null_on_oob=True).str.strip_chars()


def _num(text: pl.Expr, missing: tuple[str, ...], scale: float = 1.0) -> pl.Expr:
    cleaned = pl.when(text.is_in(list(missing))).then(None).otherwise(text)
    return cleaned.str.strip_prefix("+").cast(pl.Float64, strict=False) / scale


def _malformed(text: pl.Expr, missing: tuple[str, ...]) -> pl.Expr:
    value = text.str.strip_prefix("+").cast(pl.Float64, strict=False)
    return (text.is_not_null() & ~text.is_in(list(missing)) & value.is_null()).fill_null(False)


def parse_reports(raw: pl.DataFrame) -> pl.DataFrame:
    d = raw
    wnd_spd, vis, tmp, dew, slp = (
        _part(d, "WND", 3),
        _part(d, "VIS", 0),
        _part(d, "TMP", 0),
        _part(d, "DEW", 0),
        _part(d, "SLP", 0),
    )
    exprs: list[pl.Expr] = [
        pl.col("STATION").alias("station_id"),
        pl.col("DATE")
        .str.to_datetime("%Y-%m-%dT%H:%M:%S", time_unit="us", strict=False)
        .alias("time_utc"),
        _raw(d, "SOURCE").str.strip_chars().alias("source"),
        _raw(d, "REPORT_TYPE").str.strip_chars().alias("report_type"),
        _raw(d, "LATITUDE").cast(pl.Float64, strict=False).alias("lat"),
        _raw(d, "LONGITUDE").cast(pl.Float64, strict=False).alias("lon"),
        _raw(d, "ELEVATION").cast(pl.Float64, strict=False).alias("elev"),
        _raw(d, "source_key").alias("source_key"),
        _num(_part(d, "WND", 0), ("999",)).alias("wind_dir"),
        _part(d, "WND", 1).alias("wind_dir_qc"),
        _part(d, "WND", 2).alias("wind_type"),
        _num(wnd_spd, ("9999",), 10).alias("wsd"),
        _part(d, "WND", 4).alias("wsd_qc"),
        _num(vis, ("999999",)).alias("vis_raw"),
        _part(d, "VIS", 1).alias("vis_qc"),
        _num(tmp, ("+9999", "9999"), 10).alias("t"),
        _part(d, "TMP", 1).alias("t_qc"),
        _num(dew, ("+9999", "9999"), 10).alias("td"),
        _part(d, "DEW", 1).alias("td_qc"),
        _num(slp, ("99999",), 10).alias("slp"),
        _part(d, "SLP", 1).alias("slp_qc"),
        _num(_part(d, "OC1", 0), ("9999",), 10).alias("oc1_spd"),
        _part(d, "OC1", 1).alias("oc1_qc"),
    ]
    for n in range(1, OD_N + 1):
        c = f"OD{n}"
        exprs += [
            _part(d, c, 0).alias(f"od{n}_type"),
            _num(_part(d, c, 2), ("9999",), 10).alias(f"od{n}_spd"),
            _part(d, c, 3).alias(f"od{n}_qc"),
        ]
    for fam, count in (("MW", MW_N), ("AW", AW_N), ("MV", MV_N)):
        for n in range(1, count + 1):
            c = f"{fam}{n}"
            exprs += [
                _part(d, c, 0).alias(f"{fam.lower()}{n}_code"),
                _part(d, c, 1).alias(f"{fam.lower()}{n}_qc"),
            ]
    for n in range(1, AU_N + 1):
        exprs += [_part(d, f"AU{n}", i).alias(f"au{n}_{name}") for i, name in enumerate(_AU_PARTS)]
    for n in range(1, AY_N + 1):
        c = f"AY{n}"
        exprs += [
            _part(d, c, 0).alias(f"ay{n}_code"),
            _part(d, c, 1).alias(f"ay{n}_qc"),
            _num(_part(d, c, 2), ("99",)).alias(f"ay{n}_period_h"),
        ]
    for n in range(1, AA_N + 1):
        c = f"AA{n}"
        exprs += [
            _num(_part(d, c, 0), ("99",)).alias(f"aa{n}_period_h"),
            _num(_part(d, c, 1), ("9999",), 10).alias(f"aa{n}_mm"),
            _part(d, c, 3).alias(f"aa{n}_qc"),
        ]
    exprs += [
        _num(_part(d, "AJ1", 0), ("9999",)).alias("aj1_cm"),
        _part(d, "AJ1", 2).alias("aj1_qc"),
    ]
    bits = [
        _malformed(wnd_spd, ("9999",)).cast(pl.Int32) * PARSE_BITS["WND"],
        _malformed(vis, ("999999",)).cast(pl.Int32) * PARSE_BITS["VIS"],
        _malformed(tmp, ("+9999", "9999")).cast(pl.Int32) * PARSE_BITS["TMP"],
        _malformed(dew, ("+9999", "9999")).cast(pl.Int32) * PARSE_BITS["DEW"],
        _malformed(slp, ("99999",)).cast(pl.Int32) * PARSE_BITS["SLP"],
    ]
    exprs.append(pl.sum_horizontal(bits).cast(pl.Int32).alias("parse_status"))
    exprs += [pl.col(c) for c in ("row_key", "dup_count") if c in d.columns]
    return d.select(exprs)


def add_time_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Local mean solar time = UTC + lon/15 h (lon of each report; median lon if missing)."""
    lon = pl.col("lon").fill_null(pl.col("lon").median())
    shift_ms = (lon * 240_000).round().cast(pl.Int64)
    df = df.with_columns(time_lmst=pl.col("time_utc") + pl.duration(milliseconds=shift_ms))
    lmst = pl.col("time_lmst")
    return df.with_columns(
        lmst_date=lmst.dt.date(),
        lmst_hour=lmst.dt.hour().cast(pl.Float64) + lmst.dt.minute() / 60 + lmst.dt.second() / 3600,
    )
