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


def station_guard(bronze_paths: list[Path], settings: Settings) -> bool | None:
    """The guard for one station from all its bronze files (any years; files may lack REM)."""
    evidence = not_obs = 0
    for path in bronze_paths:
        e, n = guard_counts(pl.read_parquet(path), settings)
        evidence += e
        not_obs += n
    return nil_guard(evidence, not_obs, settings.classify)
