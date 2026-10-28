# Project rules for DCDR

## Documentation is part of every change
- `docs/code_design.md` (Part 1: software) and `docs/rules.md` (Part 2: the data rules as
  implemented now) describe the project **as it is now**. Update the affected sections in the same
  commit as the code change.
- `docs/rules_history.md` records how each rule came about: for every method version, what
  changed, why (evidence), the superseded rule and the decision. Add an entry when a rule changes.
- `docs/worklog.md` is the dated record: add an entry for every change, finding or decision
  (mark "user decision" vs "ruling"), keep the "Open items" list current, cite commit hashes.
- Update the "Last updated" line and the method version when they change.
- The design spec and plans (`docs/superpowers/`, local only, not in the public repository) are
  historical; the docs above are the source of truth.

## Data and method
- Every ISD field, code or QC meaning is checked against `legacy/isd-format-document.pdf` **and**
  against real CSV strings (CSV sub-field order can differ from the document).
- WMO code meanings come from official WMO sources (WMO-No. 306, codes.wmo.int).
- A missing weather field is never assumed to mean "no significant weather": only an explicit
  SYNOP iX 2/5 says so, or the validated inference V3 (docs/rules.md, section 5).
- **If it cannot be verified, it is "not observed"** (user decision 2026-10-20): any inference
  must be validated against ground truth; where verification is impossible, stay conservative.
- Bump `METHOD_VERSION` (`src/dcdr/process/__init__.py`) when processing results change.
- Data live under `DCDR_DATA` (the author uses `/Volumes/CrucialX8/dcdr`); never commit data.

## Working style
- Test-first for behaviour changes; run `ruff check`, `ruff format --check`, `mypy src`, `pytest`.
- `legacy/` stays verbatim; `docs/funding/` stays private (git-ignored).
- The author is learning DE/DS: show commands, explain decisions, and leave pipeline runs to them
  unless asked.
