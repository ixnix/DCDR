# DCDR — Dust Climate Data Record

Quality-controlled dust-event records from NOAA ISD surface observations.
Design: `docs/superpowers/specs/2026-10-15-dcdr-pilot-design.md`.

## Quick start

```bash
uv sync
uv run pytest
uv run dcdr --help
```

Data are written to `data/` in the repo by default (git-ignored); set `DCDR_DATA` to use another location, e.g. an external drive.
`legacy/` holds the original scripts and the NOAA ISD format document for reference only — it is never imported.
