# DCDR — Dust Climate Data Record

Quality-controlled dust-event records from NOAA ISD surface observations.

Documentation:
- [Part 1, Code design](docs/code_design.md): structure, commands, inputs, outputs, design.
- [Part 2, Rules](docs/rules.md): the data ingestion and conversion rules as implemented now.
- [Rules history](docs/rules_history.md): how each rule came about, with the evidence and decisions.
- [Work log](docs/worklog.md): dated decisions, findings and open items.
- [References](docs/references.md): the official code manuals behind the rules, with extracts.

## Quick start

```bash
uv sync
uv run pytest
uv run dcdr --help
```

Pipeline: `dcdr ingest` (NOAA S3 → bronze), `dcdr process` (→ silver), `dcdr aggregate` (→ gold), or `dcdr run` for all three.

Run `dcdr` from the repo root (or anywhere: it is an installable package — `uv build` makes a wheel).
Data are written to `./data` (relative to where you run `dcdr`; git-ignored in the repo) by default; set `DCDR_DATA` to use another location, e.g. an external drive.
`legacy/` (original v1 scripts, the NOAA ISD format document) and `notebooks/` (analyses) are kept
locally and are not part of the public repository; the package never imports them. Their findings
are in [docs/rules.md](docs/rules.md) and [docs/worklog.md](docs/worklog.md).

## Licence

The code is released under the [MIT licence](LICENSE). NOAA ISD data are public; reference
documents in `docs/references/` are short extracts with their sources (see
[docs/references.md](docs/references.md)). Licences for published data products will be set when
they are released.
