"""DCDR: Dust Climate Data Record built from NOAA ISD.

Package layout (data flows top to bottom):
    cli.py        the `dcdr` command; thin layer over pipeline.py
    pipeline.py   runs the stages, decides what needs rebuilding, records each run
    ingest.py     NOAA S3 -> bronze  (raw text, one Parquet file per station-year)
    process/      bronze  -> silver  (decoded, quality-flagged, dust-classified, sampled)
    aggregate.py  silver  -> gold    (daily table via sql/daily.sql, versioned)
    config.py     settings from default.yaml (+ profile, DCDR_DATA), with fingerprints
    state.py      atomic file writes, download manifest, run log
    schemas.py    the silver data contract
"""

from importlib.metadata import version

__version__ = version("dcdr")  # single source of truth: pyproject.toml
