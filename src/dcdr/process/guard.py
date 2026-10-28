"""Station guard for inferring "nothing significant" when the SYNOP iX is missing (rule V3).

Why a guard
-----------
Rule V3 (classify.py) infers "nothing significant" for a SYNOP report with no weather code and no
iX when its visibility is reported: in SYNOP, visibility and iX share one group, so a reported
visibility shows an observation was made. A few stations break this: they report visibility
while marking the weather "not observed" (iX 3/6), e.g. Tidjikja, Good Hope, Letlhakane. The guard
detects that habit from the station's own reports that DO carry iX (in ISD: about 2015 onwards):

    evidence  = SYNOP reports with no weather code, visibility reported, and iX 2/3/5/6
    not_obs   = those with iX 3/6
    guard     = pass  if evidence >= nil_guard_min_reports and not_obs / evidence < nil_guard_max_share
                fail  otherwise
                none  if there is too little evidence (no inference at that station)

It uses all bronze years of the station that are on disk, so older years without iX are judged by
the station's recent practice (an assumption, see docs/science.md section 5.1).
Validated in notebooks/02_validate_inferred_nil.ipynb (precision 0.9998, recall 0.84).

The METAR guard (user decision 2026-10-28; notebooks/06) does the same for METARs without full
text (before 2000 ISD keeps only fragments): "no code + visibility > 5 km" is inferred "nothing
significant" only where the station's METARs WITH full text show that such reports are rarely
"not observed" (mostly AUTO, which cannot be recognised without the text):

    evidence  = full-text METARs with no weather code and visibility > metar_nil_min_vis_m
    not_obs   = those the text rules call "not observed" (AUTO, "//", an undecoded group)
    guard     = pass / fail / none as above, with metar_nil_guard_min_reports and _max_share
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from dcdr.config import ClassifyCfg, Settings
from dcdr.process.classify import classify_reports
from dcdr.process.parse import parse_reports
from dcdr.process.qc import apply_qc


def guard_counts(raw: pl.DataFrame, settings: Settings) -> tuple[int, int]:
    """(evidence, not_obs) from bronze rows of one station, using the same decoding and QC as silver.

    Only rows whose raw message is SYNOP text are decoded, so years without iX cost almost nothing.
    """
    if "REM" not in raw.columns:
        return 0, 0
    syn = raw.filter(pl.col("REM").str.starts_with("SYN"))
    if syn.height == 0:
        return 0, 0
    df = apply_qc(parse_reports(syn), settings.qc, settings.parse)
    df = classify_reports(df, settings.classify, settings.qc)  # no guard: explicit statuses only
    ev = df.filter(
        pl.col("report_type").is_in(settings.classify.infer_nil_report_types),
        pl.col("pw_status") != "reported",
        pl.col("use_vis").fill_null(False),
        pl.col("syn_ix").is_in(["2", "3", "5", "6"]),
    )
    return ev.height, ev.filter(pl.col("syn_ix").is_in(["3", "6"])).height


def nil_guard(evidence: int, not_obs: int, cfg: ClassifyCfg) -> bool | None:
    """Pass (True), fail (False), or abstain (None) when there is too little iX evidence."""
    if evidence < cfg.nil_guard_min_reports:
        return None
    return not_obs / evidence < cfg.nil_guard_max_share


def metar_guard_counts(raw: pl.DataFrame, settings: Settings) -> tuple[int, int]:
    """(evidence, not_obs) for the METAR guard, from bronze rows of one station."""
    if "REM" not in raw.columns:
        return 0, 0
    met = raw.filter(pl.col("REM").str.starts_with("MET"))
    if met.height == 0:
        return 0, 0
    cfg = settings.classify
    df = apply_qc(parse_reports(met), settings.qc, settings.parse)
    df = classify_reports(df, cfg, settings.qc)  # no guards: the text rules alone
    ev = df.filter(
        pl.col("report_type").is_in(cfg.metar_report_types),
        pl.col("metar_text") == "full",
        pl.col("pw_status") != "reported",
        pl.col("use_vis").fill_null(False),
        pl.col("vis_raw") > cfg.metar_nil_min_vis_m,
    )
    return ev.height, ev.filter(~pl.col("pw_status").is_in(["nil_metar", "nil_significant"])).height


def metar_guard(evidence: int, not_obs: int, cfg: ClassifyCfg) -> bool | None:
    """Pass (True), fail (False), or abstain (None) when there is too little full-text evidence."""
    if evidence < cfg.metar_nil_guard_min_reports:
        return None
    return not_obs / evidence < cfg.metar_nil_guard_max_share


def station_guards(bronze_paths: list[Path], settings: Settings) -> tuple[bool | None, bool | None]:
    """(V3 guard, METAR guard) for one station from all its bronze files, reading each once."""
    syn_e = syn_n = met_e = met_n = 0
    for path in bronze_paths:
        raw = pl.read_parquet(path)
        e, n = guard_counts(raw, settings)
        syn_e, syn_n = syn_e + e, syn_n + n
        e, n = metar_guard_counts(raw, settings)
        met_e, met_n = met_e + e, met_n + n
    return nil_guard(syn_e, syn_n, settings.classify), metar_guard(met_e, met_n, settings.classify)


def station_guard(bronze_paths: list[Path], settings: Settings) -> bool | None:
    """The V3 guard for one station from all its bronze files (any years; files may lack REM)."""
    evidence = not_obs = 0
    for path in bronze_paths:
        e, n = guard_counts(pl.read_parquet(path), settings)
        evidence += e
        not_obs += n
    return nil_guard(evidence, not_obs, settings.classify)
