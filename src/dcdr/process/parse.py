"""Decode ISD packed fields into typed columns and add time columns (spec §7.2.1-7.2.2).

Field layouts, missing-value codes and scale factors follow the ISD format document
(legacy/isd-format-document.pdf): e.g. WND speed 9999 = missing, scale 10; VIS 999999;
TMP/DEW +9999, scale 10; SLP 99999, scale 10; OC1/OD speed 9999, scale 10; AA depth 9999,
scale 10; AJ1 depth 9999 (cm); AY period 99.

What "packed" means
-------------------
In NOAA's CSV files, each observation group is ONE text column whose parts are separated by
commas, for example:

    WND = "350,1,N,0046,1"  -> direction 350°, its QC code 1, type N (normal),
                               speed 0046 = 4.6 m/s (stored x10), speed QC code 1
    TMP = "+0251,1"         -> 25.1 °C (stored x10), QC code 1;  "+9999" means missing
    MW1 = "06,1"            -> present-weather code 06 (dust in suspension), QC code 1

`parse_reports` splits these columns into one typed column per part (`wind_dir`, `wsd`, `t`,
`mw1_code`, ...), turns missing-value codes into nulls, and divides by the scale factor.
Columns that a file doesn't have (ISD files differ) simply come out as all-null.

`add_time_columns` then adds local mean solar time, so days are counted in local time.
"""

from __future__ import annotations

import polars as pl

# How many numbered copies of each repeating field ISD defines (MW1..MW7, AU1..AU9, ...).
MW_N, AU_N, AW_N, MV_N, AY_N, OD_N, AA_N = 7, 9, 4, 7, 2, 3, 4
AT_N, AX_N, AZ_N = 8, 6, 2
# `parse_status` is a bit mask recording which core fields held text that isn't a number (and
# isn't a missing code either). Each field has its own bit, so e.g. 6 = VIS (2) + TMP (4) bad.
PARSE_BITS = {"WND": 1, "VIS": 2, "TMP": 4, "DEW": 8, "SLP": 16}
# Parts of an AU (automated present weather, METAR-style) group, in order.
_AU_PARTS = ("int", "desc", "prec", "obsc", "other", "comb", "qc")
# A METAR present-weather group (WMO code table 4678): optional intensity or VC, then two-letter
# descriptors and phenomena. Deliberately broad (any order, several descriptors) so that
# malformed groups such as "-TSBLSA" still count as weather: missing a group would wrongly turn
# the report into "no weather group". Recent weather ("RESS") does not match ("RE" is not a code).
METAR_WX_TOKEN = (
    r"^(?:[+-]|VC)?(?:MI|BC|PR|DR|BL|SH|TS|FZ|DZ|RA|SN|SG|PL|GR|GS|UP|IC|BR|FG|FU|VA|DU|SA|HZ"
    r"|PY|PO|SQ|FC|SS|DS)+$"
)


def metar_tokens(rem: pl.Expr) -> pl.Expr:
    """The observed part of a METAR/SPECI message as a list of groups (null if REM is not one).

    ISD keeps the message in REM as "MET" + 3-digit length + text, e.g.
    "MET072METAR YBMA 082130Z AUTO 00000KT 9999 // SCT085 15/09 Q1017 RF00/0/000/0=";
    US messages have a local date and time first: "MET10012/31/19 17:51:03 METAR KELP ...";
    in the 2000s the word METAR is often missing and the text ends with ";":
    "MET046LROP 282200Z VRB01MPS CAVOK 11/11 Q1018 NOSIG;". A full message is recognised by its
    station identifier and time ("LROP 282200Z"). Before 2000 REM holds only the fragments ISD did
    not decode ("MET012CAVOK NOSIG?"): those are not a METAR text and give null.
    Dropped: the station identifier (first group), the remarks (from "RMK"), and the trend
    forecast (from "NOSIG", "BECMG" or "TEMPO"), which are not observations of present weather.
    """
    body = (
        rem.str.extract(
            r"^MET\d{3}(?:\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} )?(?:(?:METAR|SPECI) )?(?:COR )?"
            r"([A-Z][A-Z0-9]{3} \d{6}Z.*)$",
            1,
        )
        .str.replace(r"[=;]\s*$", "")
        .str.replace(r" RMK( .*)?$", "")
        .str.replace(r" (?:NOSIG|BECMG|TEMPO)( .*)?$", "")
        .str.strip_chars()
    )
    return body.str.split(" ").list.slice(1)


def _raw(df: pl.DataFrame, col: str) -> pl.Expr:
    """The column if this file has it, otherwise an all-null text column (so code never fails)."""
    return pl.col(col) if col in df.columns else pl.lit(None, dtype=pl.String)


def _part(df: pl.DataFrame, col: str, i: int) -> pl.Expr:
    """The i-th comma-separated part of a packed column, trimmed ("350,1,N" , 0 -> "350").

    `null_on_oob`: if the text has fewer parts than expected, return null instead of failing.
    """
    return _raw(df, col).str.split(",").list.get(i, null_on_oob=True).str.strip_chars()


def _num(text: pl.Expr, missing: tuple[str, ...], scale: float = 1.0) -> pl.Expr:
    """Text -> number: missing codes become null, a leading "+" is dropped, then divide by scale.

    `strict=False`: text that isn't a number becomes null rather than raising an error (it is
    reported separately through `parse_status`).
    """
    cleaned = pl.when(text.is_in(list(missing))).then(None).otherwise(text)
    return cleaned.str.strip_prefix("+").cast(pl.Float64, strict=False) / scale


def _malformed(text: pl.Expr, missing: tuple[str, ...]) -> pl.Expr:
    """True where the text is present, is not a missing code, and still is not a number."""
    value = text.str.strip_prefix("+").cast(pl.Float64, strict=False)
    return (text.is_not_null() & ~text.is_in(list(missing)) & value.is_null()).fill_null(False)


def parse_reports(raw: pl.DataFrame) -> pl.DataFrame:
    """Decode one station-year of bronze (all text) into typed columns, one row per report.

    All column expressions are collected in a list first and evaluated in a single `select`,
    which lets Polars compute them in parallel.
    """
    d = raw
    tokens = metar_tokens(_raw(d, "REM"))  # observed METAR groups (null for non-METAR remarks)
    # Text parts of the core fields, kept as variables because both _num and _malformed use them.
    wnd_spd, vis, tmp, dew, slp = (
        _part(d, "WND", 3),
        _part(d, "VIS", 0),
        _part(d, "TMP", 0),
        _part(d, "DEW", 0),
        _part(d, "SLP", 0),
    )
    exprs: list[pl.Expr] = [
        # --- identification, time and location ---
        pl.col("STATION").alias("station_id"),
        pl.col("DATE")
        .str.to_datetime("%Y-%m-%dT%H:%M:%S", time_unit="us", strict=False)  # bad dates -> null
        .alias("time_utc"),
        _raw(d, "SOURCE").str.strip_chars().alias("source"),  # data source flag (ISD merge)
        # report type, e.g. FM-12 (SYNOP), FM-15 (METAR), FM-16 (SPECI), SOD (daily summary)
        _raw(d, "REPORT_TYPE").str.strip_chars().alias("report_type"),
        _raw(d, "LATITUDE").cast(pl.Float64, strict=False).alias("lat"),
        _raw(d, "LONGITUDE").cast(pl.Float64, strict=False).alias("lon"),
        _raw(d, "ELEVATION").cast(pl.Float64, strict=False).alias("elev"),
        _raw(d, "source_key").alias("source_key"),
        # SYNOP iX indicator (WMO code table 1860) from the raw SYNOP message that ISD keeps in REM:
        # "SYN" + 3-digit length + 5-digit WMO station number + space + the group iR iX h VV.
        # E.g. "SYN08040829 12960 ..." -> iR=1, iX=2. Null for METAR remarks, for messages NOAA
        # received only as BUFR ("SYN004BUFR"), and when the file has no REM column.
        _raw(d, "REM").str.extract(r"^SYN\d{3}\d{5} [0-4/]([1-7])", 1).alias("syn_ix"),
        # METAR/SPECI text (null when REM holds no METAR message): AUTO = fully automated, no
        # human involvement (WMO FM 15.4); metar_wx = what the present-weather part says:
        # "missing" ("//", not observable, FM 15.8.19), "group" (weather groups), "cavok"
        # (CAVOK, FM 15.10), or "none" (no weather group: nothing reportable, FM 15.8.2).
        tokens.list.contains("AUTO").alias("metar_auto"),
        pl.when(tokens.list.contains("//"))
        .then(pl.lit("missing"))
        .when(tokens.list.eval(pl.element().str.contains(METAR_WX_TOKEN)).list.any())
        .then(pl.lit("group"))
        .when(tokens.list.contains("CAVOK"))
        .then(pl.lit("cavok"))
        .when(tokens.is_not_null())
        .then(pl.lit("none"))
        .alias("metar_wx"),
        # --- WND: direction, dir QC, type, speed (x10), speed QC ---
        _num(_part(d, "WND", 0), ("999",)).alias("wind_dir"),
        _part(d, "WND", 1).alias("wind_dir_qc"),
        _part(d, "WND", 2).alias("wind_type"),
        _num(wnd_spd, ("9999",), 10).alias("wsd"),
        _part(d, "WND", 4).alias("wsd_qc"),
        # --- VIS: distance (m), QC ---
        _num(vis, ("999999",)).alias("vis_raw"),
        _part(d, "VIS", 1).alias("vis_qc"),
        # --- TMP / DEW: air temperature and dew point (°C x10), QC ---
        _num(tmp, ("+9999", "9999"), 10).alias("t"),
        _part(d, "TMP", 1).alias("t_qc"),
        _num(dew, ("+9999", "9999"), 10).alias("td"),
        _part(d, "DEW", 1).alias("td_qc"),
        # --- SLP: sea-level pressure (hPa x10), QC ---
        _num(slp, ("99999",), 10).alias("slp"),
        _part(d, "SLP", 1).alias("slp_qc"),
        # --- OC1: wind gust (m/s x10), QC ---
        _num(_part(d, "OC1", 0), ("9999",), 10).alias("oc1_spd"),
        _part(d, "OC1", 1).alias("oc1_qc"),
    ]
    # OD1-3 supplementary wind: type, period, speed (x10), QC. Some types are gusts (qc.py).
    for n in range(1, OD_N + 1):
        c = f"OD{n}"
        exprs += [
            _part(d, c, 0).alias(f"od{n}_type"),
            _num(_part(d, c, 2), ("9999",), 10).alias(f"od{n}_spd"),
            _part(d, c, 3).alias(f"od{n}_qc"),
        ]
    # MW = manual present weather, AW = automated present weather, MV = weather in the vicinity.
    # Each is "code,QC"; the codes are decoded into dust classes in classify.py.
    for fam, count in (("MW", MW_N), ("AW", AW_N), ("MV", MV_N)):
        for n in range(1, count + 1):
            c = f"{fam}{n}"
            exprs += [
                _part(d, c, 0).alias(f"{fam.lower()}{n}_code"),
                _part(d, c, 1).alias(f"{fam.lower()}{n}_qc"),
            ]
    # AU = METAR-style present weather, split into its components (intensity, descriptor, ...).
    for n in range(1, AU_N + 1):
        exprs += [_part(d, f"AU{n}", i).alias(f"au{n}_{name}") for i, name in enumerate(_AU_PARTS)]
    # AY = manual past weather: code, QC, period (hours).
    for n in range(1, AY_N + 1):
        c = f"AY{n}"
        exprs += [
            _part(d, c, 0).alias(f"ay{n}_code"),
            _part(d, c, 1).alias(f"ay{n}_qc"),
            _num(_part(d, c, 2), ("99",)).alias(f"ay{n}_period_h"),
        ]
    # AA = liquid precipitation: period (h), depth (mm x10), condition, QC. Kept for later use.
    for n in range(1, AA_N + 1):
        c = f"AA{n}"
        exprs += [
            _num(_part(d, c, 0), ("99",)).alias(f"aa{n}_period_h"),
            _num(_part(d, c, 1), ("9999",), 10).alias(f"aa{n}_mm"),
            _part(d, c, 3).alias(f"aa{n}_qc"),
        ]
    # AJ1 = snow depth (cm), QC. Kept for later use (snow cover suppresses dust emission).
    exprs += [
        _num(_part(d, "AJ1", 0), ("9999",)).alias("aj1_cm"),
        _part(d, "AJ1", 2).alias("aj1_qc"),
    ]
    for n in range(1, AT_N + 1):  # DAILY-PRESENT-WEATHER: source, weather type, abbreviation, QC
        c = f"AT{n}"
        exprs += [
            _part(d, c, 0).alias(f"at{n}_src"),
            _part(d, c, 1).alias(f"at{n}_wea"),
            _part(d, c, 3).alias(f"at{n}_qc"),
        ]
    for fam, count in (("AX", AX_N), ("AZ", AZ_N)):  # past weather: code, QC, period (h), period QC
        for n in range(1, count + 1):
            c, low = f"{fam}{n}", f"{fam.lower()}{n}"
            exprs += [
                _part(d, c, 0).alias(f"{low}_code"),
                _part(d, c, 1).alias(f"{low}_qc"),
                _num(_part(d, c, 2), ("99",)).alias(f"{low}_period_h"),
            ]
    # Build the parse_status bit mask: each malformed core field adds its bit (0 = all fine).
    bits = [
        _malformed(wnd_spd, ("9999",)).cast(pl.Int32) * PARSE_BITS["WND"],
        _malformed(vis, ("999999",)).cast(pl.Int32) * PARSE_BITS["VIS"],
        _malformed(tmp, ("+9999", "9999")).cast(pl.Int32) * PARSE_BITS["TMP"],
        _malformed(dew, ("+9999", "9999")).cast(pl.Int32) * PARSE_BITS["DEW"],
        _malformed(slp, ("99999",)).cast(pl.Int32) * PARSE_BITS["SLP"],
    ]
    exprs.append(pl.sum_horizontal(bits).cast(pl.Int32).alias("parse_status"))
    # Carry over the fingerprint and duplicate count added by build.dedupe, when present.
    exprs += [pl.col(c) for c in ("row_key", "dup_count") if c in d.columns]
    return d.select(exprs)


class MissingLongitudeError(ValueError):
    """No usable longitude in a station-year, so local time cannot be computed."""


def lmst_shift(lon: pl.Expr) -> pl.Expr:
    """Offset from UTC to local mean solar time: lon/15 hours."""
    # The sun moves 15° of longitude per hour, i.e. 4 minutes = 240,000 ms per degree.
    # East longitudes (positive) are ahead of UTC, west (negative) behind.
    return pl.duration(milliseconds=(lon * 240_000).round().cast(pl.Int64))


def add_time_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Local mean solar time = UTC + lon/15 h, using each report's own longitude.

    Out-of-range coordinates (ISD missing code +999999 reads as 999.999) become null; a missing
    longitude falls back to the station-year median. Daily-summary (SOD) rows are stamped 23:59
    local STANDARD time, which can land just after LMST midnight, so their summarised day is
    date(time_lmst - 12 h).

    Why local solar time: dust has a strong daily cycle (afternoon winds), so a "day" should be
    the local day. Solar time depends only on longitude, so it is the same rule everywhere,
    unlike time zones and daylight saving.
    """
    df = df.with_columns(
        lat=pl.when(pl.col("lat").is_between(-90, 90)).then(pl.col("lat")),  # else null
        lon=pl.when(pl.col("lon").is_between(-180, 180)).then(pl.col("lon")),
    )
    df = df.with_columns(lon_used=pl.col("lon").fill_null(pl.col("lon").median()))
    if df.height and df["lon_used"].null_count():
        raise MissingLongitudeError(
            "no valid LONGITUDE in this station-year (ISD missing 999.999?)"
        )
    df = df.with_columns(time_lmst=pl.col("time_utc") + lmst_shift(pl.col("lon_used")))
    lmst = pl.col("time_lmst")
    return df.with_columns(
        lmst_date=lmst.dt.date(),
        # Fractional hour of the local day, e.g. 14:30 -> 14.5.
        lmst_hour=lmst.dt.hour().cast(pl.Float64) + lmst.dt.minute() / 60 + lmst.dt.second() / 3600,
        summary_lmst_date=pl.when(pl.col("report_type") == "SOD").then(
            (lmst - pl.duration(hours=12)).dt.date()
        ),
    )
