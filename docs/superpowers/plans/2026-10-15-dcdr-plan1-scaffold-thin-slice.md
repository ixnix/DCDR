# DCDR Plan 1: Scaffolding and Thin Slice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `dcdr` Python package and run a real thin slice: two stations' ISD station-years go from S3 to bronze, then silver (parsed, QC'd, dust-classified, synoptic-sampled), then a versioned **daily** gold table, and finally one diagnostic chart.

**Architecture:** A flat `src/dcdr` package with one module per responsibility; the silver science lives in `src/dcdr/silver/`. Bronze and silver are one Parquet file per station-year, each written atomically (temp file, then `os.replace`). Gold is rebuilt in full into `gold/versions/<run_id>/` and activated by an atomic `CURRENT` pointer file. A thin Typer CLI calls `pipeline.py`.

**Tech Stack:** Python 3.12, uv, Polars 2.x, PyArrow, DuckDB 1.5+, obstore 0.12+, Pandera (Polars backend), Pydantic 2, PyYAML, Typer, pytest, ruff, mypy, matplotlib (dev only).

**Spec:** `docs/superpowers/specs/2026-10-15-dcdr-pilot-design.md`. Read §5–§7.3.2 and §11–§13 before starting.

**Plan series:** This is Plan 1 of 5.
- Plan 1 (this one): spec milestones M0 + M1.
- Plan 2: M2 + M3 (pilot selection, 100 stations, the remaining gold products).
- Plan 3: M4 (releases).
- Plan 4: M5 (website).
- Plan 5: M6 (monthly updates, backups, documentation).

**Deferred to Plan 2 (out of scope here; do not build):**
- Spooling objects larger than 100 MB to a temporary file. No ISD station-year file is near that size.
- The `duisd_v1` profile.
- `sampling.synoptic_dust_source: any_in_window`.
- Daily precipitation and snow aggregates. They are parsed in this plan but not aggregated.
- The `monthly`, `annual`, `station_year_coverage`, `stations` catalog, `climatology`, `diurnal`, `trends`, `uth` and code-6 products.
- Pilot station selection.

## Global Constraints

- Python `>=3.12`; Polars `>=2.0,<3`; DuckDB `>=1.5`; obstore `>=0.12`; pandera `>=0.34`.
- Default data root: `/Volumes/CrucialX8/dcdr`. The `DCDR_DATA` environment variable overrides it. **Never create the root's parent directory.** If the parent is missing (SSD not mounted), fail with a clear error.
- Never discover data files with broad globs. Use the manifest, the station list, or `state.safe_listdir`, which skips names starting with `.` (macOS `._*` files, `.DS_Store`) and names ending in `.tmp`.
- Every file write is atomic: write `<name>.tmp`, then `os.replace` onto the final name. A failure leaves the previous file intact and no `.tmp` behind.
- Silver never deletes rows. The **only** exception is rows whose `DATE` can't be parsed: they can't be placed in time, so they are dropped and counted in a warning log line.
- Present-weather code tables are exactly those in spec §7.2.4. **AW codes 30–35 are fog, never dust.**
- Local time is **local mean solar time**: `time_lmst = time_utc + lon/15 h`.
- Tests run offline by default. Tests that need NOAA S3 carry `@pytest.mark.network` and are excluded by `addopts`.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **The SSD isn't mounted** when a command runs. Expected: a clear "is the SSD mounted?" error, never a new `/Volumes/CrucialX8/dcdr` folder on the internal disk. Test owned by Task 13.
2. **A bronze station-year with a header and no data rows**, which NOAA sometimes publishes. Expected: silver builds an empty file and gold runs without error. Test owned by Task 11.
3. **Rows with a malformed `DATE`.** Expected: dropped with a warning count, while the rest of the file builds normally. Test owned by Task 11.
4. **A station-day containing only excluded report types** (`SOD`/`SOM`). Expected: no daily row and no crash. Test owned by Task 12.
5. **A station that moves mid-year** (its `LONGITUDE` changes). Expected: each report's local time uses its own longitude. Test owned by Task 6.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml`, `uv.lock`, `.python-version` | Package metadata, pinned dependencies, tool config |
| `README.md` | Quick start |
| `.pre-commit-config.yaml`, `.github/workflows/ci.yml` | Lint, type-check and test automation |
| `config/default.yaml` | Every method setting, with the spec defaults |
| `config/stations/thin_slice.csv` | Two stations for the thin slice |
| `src/dcdr/__init__.py` | Version |
| `src/dcdr/cli.py` | Typer CLI (argument parsing only) |
| `src/dcdr/config.py` | Pydantic settings, YAML merge, config hashes |
| `src/dcdr/state.py` | Atomic writes, safe listing, source manifest, run records, git revision |
| `src/dcdr/ingest.py` | S3 listing, change detection, MD5, bronze writer |
| `src/dcdr/silver/__init__.py` | `METHOD_VERSION` |
| `src/dcdr/silver/parse.py` | Packed-field decoding, time columns |
| `src/dcdr/silver/qc.py` | QC flags, RH, visibility cap, gusts |
| `src/dcdr/silver/classify.py` | Per-field code tables, dust classes, haze rule, RH screen |
| `src/dcdr/silver/sampling.py` | Synoptic-slot candidates and deterministic selection |
| `src/dcdr/silver/build.py` | Station-year silver assembly (dedupe, look-back, order, write) |
| `src/dcdr/schemas.py` | Pandera silver contract plus the slot-uniqueness check |
| `src/dcdr/gold.py` | Product registry, gold versions, `CURRENT`, checks |
| `sql/daily.sql` | The daily gold product |
| `src/dcdr/pipeline.py` | Run context, incremental silver decisions, stage functions |
| `scripts/thin_slice_report.py` | Diagnostic summary and chart |
| `tests/conftest.py`, `tests/fixtures/isd.py` | Shared fixtures; synthetic ISD CSV builder |
| `tests/unit/*.py`, `tests/integration/*.py` | Tests |

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`, `.python-version`, `README.md`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `src/dcdr/__init__.py`, `src/dcdr/cli.py`, `tests/__init__.py`, `tests/unit/__init__.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Produces: `dcdr.__version__: str`; `dcdr.cli.app: typer.Typer`; console script `dcdr`.

- [ ] **Step 1: Pin Python and write `pyproject.toml`**

```bash
cd /Users/xinxi/Projects/DCDR && uv python pin 3.12
```

`pyproject.toml`:
```toml
[project]
name = "dcdr"
version = "0.1.0"
description = "Dust Climate Data Record: quality-controlled dust events from NOAA ISD"
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [{ name = "Xin Xi" }]
dependencies = [
  "polars>=2.0,<3",
  "pyarrow>=20",
  "duckdb>=1.5",
  "obstore>=0.12",
  "pandera[polars]>=0.34",
  "pydantic>=2.10",
  "pyyaml>=6.0",
  "typer>=0.15",
]

[project.scripts]
dcdr = "dcdr.cli:app"

[dependency-groups]
dev = [
  "pytest>=8.3",
  "ruff>=0.9",
  "mypy>=1.15",
  "types-pyyaml>=6.0",
  "pre-commit>=4.0",
  "matplotlib>=3.9",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/dcdr"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
ignore = ["E501"]  # line length is handled by `ruff format`

[tool.mypy]
python_version = "3.12"
ignore_missing_imports = true
warn_unused_ignores = true
check_untyped_defs = true

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-m 'not network'"
markers = ["network: test needs internet access to NOAA S3"]
```

- [ ] **Step 2: Write the package and CLI skeleton**

`src/dcdr/__init__.py`:
```python
"""DCDR: Dust Climate Data Record built from NOAA ISD."""

__version__ = "0.1.0"
```

`src/dcdr/cli.py`:
```python
"""Command-line interface. Parses arguments and calls pipeline functions; no logic here."""

from __future__ import annotations

import logging

import typer

from dcdr import __version__

app = typer.Typer(help="DCDR pipeline command-line interface.", no_args_is_help=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"dcdr {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """DCDR pipeline."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
```

`tests/__init__.py` and `tests/unit/__init__.py`: empty files.

- [ ] **Step 3: Write the failing test**

`tests/unit/test_cli.py`:
```python
from typer.testing import CliRunner

from dcdr import __version__
from dcdr.cli import app


def test_version_flag_prints_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"dcdr {__version__}" in result.output
```

- [ ] **Step 4: Install and run the test**

```bash
uv sync
```
```bash
uv run pytest tests/unit/test_cli.py -v
```
Expected: PASS (1 passed). `uv sync` creates `.venv/` and `uv.lock`.

- [ ] **Step 5: Add README, pre-commit and CI**

`README.md`:
````markdown
# DCDR — Dust Climate Data Record

Quality-controlled dust-event records from NOAA ISD surface observations.
Design: `docs/superpowers/specs/2026-10-15-dcdr-pilot-design.md`.

## Quick start

```bash
uv sync
uv run pytest
uv run dcdr --help
```

Data are written to `/Volumes/CrucialX8/dcdr` by default; set `DCDR_DATA` to override.
`legacy/` holds the original scripts for reference only — it is never imported.
````

`.pre-commit-config.yaml`:
```yaml
repos:
  - repo: local
    hooks:
      - id: ruff-check
        name: ruff check
        entry: uv run ruff check --fix
        language: system
        types: [python]
      - id: ruff-format
        name: ruff format
        entry: uv run ruff format
        language: system
        types: [python]
```

`.github/workflows/ci.yml`:
```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv python install 3.12
      - run: uv sync --locked
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run mypy src
      - run: uv run pytest
```

- [ ] **Step 6: Run the quality gates**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
```
Expected: all pass (run `uv run ruff format .` first if formatting differs).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock .python-version README.md .pre-commit-config.yaml .github src tests
git commit -m "feat: scaffold dcdr package, CLI and CI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Configuration

**Files:**
- Create: `config/default.yaml`, `src/dcdr/config.py`, `tests/conftest.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces:
  - `dcdr.config.REPO_ROOT: Path`, `DEFAULT_CONFIG: Path`, `SILVER_SECTIONS: tuple[str, ...]`.
  - Pydantic models `Settings`, `PathsCfg`, `IngestCfg`, `ParseCfg`, `QcCfg`, `ClassifyCfg`, `SamplingCfg`, `ProductsCfg`, `StationsCfg`.
  - `Settings.data_root -> Path`.
  - `load_settings(config_path: Path = DEFAULT_CONFIG, profile: Path | None = None, overrides: dict | None = None) -> Settings`.
  - `canonical_yaml(settings, sections=None) -> str`, `config_hash(settings, sections=None) -> str` (16 hex chars), `silver_config_hash(settings) -> str`.
  - Pytest fixture `settings` (data root under `tmp_path`).

- [ ] **Step 1: Write the default config**

`config/default.yaml`:
```yaml
# DCDR default configuration. Every method choice in the spec is a key here.
paths:
  data_root: /Volumes/CrucialX8/dcdr
ingest:
  bucket: noaa-global-hourly-pds
  region: us-east-1
  max_concurrency: 16
  retries: 5
parse:
  gust_od_types: [3, 4]
qc:
  codes_ok: ["0", "1", "4", "5", "9", "A", "C", "I", "M", "P", "R", "U"]
  codes_suspect: ["2", "6"]
  codes_erroneous: ["3", "7"]
  accept: [ok]
  vis_cap_m: 10000
  rh_screen_pct: 90
  rh_screen_classes: [suspended, haze, haze_or_suspended, dust_haze]
classify:
  fields: [MW, AU, AW]
  haze_window_h: 24
  sand_min_t_c: 3.0
  snow_max_t_c: 0.0
sampling:
  window_min: 30
  exclude_types: [SOD, SOM]
  priority: [FM-12, SY-MT, SY-SA, SY-AE, SY-AU, FM-15, SAO, AUTO, FM-16]
  synoptic_dust_source: selected
products:
  min_slots_per_day: 4
  gold_keep_versions: 3
stations:
  station_list: config/stations/thin_slice.csv
```

- [ ] **Step 2: Write the failing tests**

`tests/conftest.py`:
```python
from pathlib import Path

import pytest

from dcdr.config import Settings, load_settings


@pytest.fixture(autouse=True)
def _no_env_data_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DCDR_DATA", raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(overrides={"paths": {"data_root": str(tmp_path / "data")}})
```

`tests/unit/test_config.py`:
```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from dcdr.config import config_hash, load_settings, silver_config_hash


def test_defaults_load() -> None:
    s = load_settings()
    assert s.qc.vis_cap_m == 10000
    assert s.classify.fields == ["MW", "AU", "AW"]
    assert s.parse.gust_od_types == [3, 4]
    assert str(s.data_root) == "/Volumes/CrucialX8/dcdr"


def test_profile_overrides_only_named_keys(tmp_path: Path) -> None:
    profile = tmp_path / "p.yaml"
    profile.write_text("classify:\n  fields: [MW]\n")
    s = load_settings(profile=profile)
    assert s.classify.fields == ["MW"]
    assert s.classify.haze_window_h == 24


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        load_settings(overrides={"qc": {"bogus": 1}})


def test_env_var_overrides_data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DCDR_DATA", str(tmp_path))
    assert load_settings().data_root == tmp_path


def test_hashes_are_stable_and_scoped() -> None:
    a, b = load_settings(), load_settings()
    assert config_hash(a) == config_hash(b)
    moved = load_settings(overrides={"paths": {"data_root": "/elsewhere"}})
    assert silver_config_hash(moved) == silver_config_hash(a)
    assert config_hash(moved) != config_hash(a)
    stricter = load_settings(overrides={"qc": {"rh_screen_pct": 95}})
    assert silver_config_hash(stricter) != silver_config_hash(a)
```

- [ ] **Step 3: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dcdr.config'`.

- [ ] **Step 4: Implement `src/dcdr/config.py`**

```python
"""Validated settings: YAML defaults + optional profile + overrides, with stable hashes."""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"
SILVER_SECTIONS = ("parse", "qc", "classify", "sampling")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsCfg(_Section):
    data_root: str


class IngestCfg(_Section):
    bucket: str
    region: str
    max_concurrency: int
    retries: int


class ParseCfg(_Section):
    gust_od_types: list[int]


class QcCfg(_Section):
    codes_ok: list[str]
    codes_suspect: list[str]
    codes_erroneous: list[str]
    accept: list[Literal["ok", "suspect"]]
    vis_cap_m: float
    rh_screen_pct: float
    rh_screen_classes: list[str]


class ClassifyCfg(_Section):
    fields: list[Literal["MW", "AU", "AW"]]
    haze_window_h: int
    sand_min_t_c: float
    snow_max_t_c: float


class SamplingCfg(_Section):
    window_min: int
    exclude_types: list[str]
    priority: list[str]
    synoptic_dust_source: Literal["selected"]


class ProductsCfg(_Section):
    min_slots_per_day: int
    gold_keep_versions: int


class StationsCfg(_Section):
    station_list: str


class Settings(_Section):
    paths: PathsCfg
    ingest: IngestCfg
    parse: ParseCfg
    qc: QcCfg
    classify: ClassifyCfg
    sampling: SamplingCfg
    products: ProductsCfg
    stations: StationsCfg

    @property
    def data_root(self) -> Path:
        return Path(self.paths.data_root)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_settings(
    config_path: Path = DEFAULT_CONFIG,
    profile: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    data: dict[str, Any] = yaml.safe_load(config_path.read_text())
    if profile is not None:
        data = deep_merge(data, yaml.safe_load(profile.read_text()) or {})
    if overrides:
        data = deep_merge(data, overrides)
    env_root = os.environ.get("DCDR_DATA")
    if env_root:
        data = deep_merge(data, {"paths": {"data_root": env_root}})
    return Settings.model_validate(data)


def canonical_yaml(settings: Settings, sections: tuple[str, ...] | None = None) -> str:
    dumped = settings.model_dump(mode="json")
    if sections is not None:
        dumped = {k: dumped[k] for k in sections}
    return yaml.safe_dump(dumped, sort_keys=True)


def config_hash(settings: Settings, sections: tuple[str, ...] | None = None) -> str:
    return hashlib.sha256(canonical_yaml(settings, sections).encode()).hexdigest()[:16]


def silver_config_hash(settings: Settings) -> str:
    return config_hash(settings, SILVER_SECTIONS)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add config/default.yaml src/dcdr/config.py tests/conftest.py tests/unit/test_config.py
git commit -m "feat: validated YAML settings with profile merge and config hashes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Run state (atomic writes, safe listing, manifest, run records)

**Files:**
- Create: `src/dcdr/state.py`, `tests/unit/test_state.py`

**Interfaces:**
- Produces:
  - `utcnow() -> datetime` (UTC-aware); `new_run_id() -> str` (format `YYYYMMDDTHHMMSSZ-xxxxxx`).
  - `atomic_write_bytes(path: Path, data: bytes) -> None`.
  - `atomic_write_parquet(table: pa.Table, path: Path, metadata: dict[str, str] | None = None) -> None`.
  - `read_parquet_metadata(path: Path) -> dict[str, str]`.
  - `safe_listdir(path: Path) -> list[Path]`.
  - `MANIFEST_SCHEMA: dict[str, Any]`; class `Manifest(path)` with attribute `df: pl.DataFrame` and methods `get(key) -> dict | None`, `upsert(records: list[dict]) -> None`, `save() -> None`.
  - `RUNS_SCHEMA`; `append_run_record(runs_path: Path, record: dict) -> None`.
  - `git_revision(repo: Path) -> tuple[str, bool]`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_state.py`:
```python
import re
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from dcdr import state


def test_atomic_parquet_roundtrip_with_metadata(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b.parquet"
    state.atomic_write_parquet(pl.DataFrame({"x": [1, 2]}).to_arrow(), path, {"dcdr_layer": "bronze"})
    assert pl.read_parquet(path)["x"].to_list() == [1, 2]
    assert state.read_parquet_metadata(path) == {"dcdr_layer": "bronze"}
    assert not path.with_name("b.parquet.tmp").exists()


def test_failed_replace_keeps_previous_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "f.parquet"
    state.atomic_write_parquet(pl.DataFrame({"x": [1]}).to_arrow(), path)

    def boom(*_: object) -> None:
        raise OSError("disk unplugged")

    monkeypatch.setattr(state.os, "replace", boom)
    with pytest.raises(OSError):
        state.atomic_write_parquet(pl.DataFrame({"x": [99]}).to_arrow(), path)
    monkeypatch.undo()
    assert pl.read_parquet(path)["x"].to_list() == [1]
    assert not path.with_name("f.parquet.tmp").exists()


def test_safe_listdir_skips_hidden_and_tmp(tmp_path: Path) -> None:
    for name in ("part-000.parquet", "._part-000.parquet", ".DS_Store", "x.parquet.tmp"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in state.safe_listdir(tmp_path)] == ["part-000.parquet"]
    assert state.safe_listdir(tmp_path / "missing") == []


def test_manifest_upsert_latest_wins_and_reloads(tmp_path: Path) -> None:
    m = state.Manifest(tmp_path / "sources.parquet")
    when = datetime(2025, 3, 1, tzinfo=UTC)
    m.upsert([{"key": "2024/A.csv", "station_id": "A", "year": 2024, "status": "failed", "last_modified": when}])
    m.upsert([{"key": "2024/A.csv", "station_id": "A", "year": 2024, "status": "ok", "last_modified": when}])
    m.save()
    again = state.Manifest(tmp_path / "sources.parquet")
    assert again.df.height == 1
    row = again.get("2024/A.csv")
    assert row is not None and row["status"] == "ok"
    assert again.get("nope") is None


def test_run_id_format() -> None:
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", state.new_run_id())


def test_append_run_record_accumulates(tmp_path: Path) -> None:
    runs = tmp_path / "runs.parquet"
    for rid in ("r1", "r2"):
        state.append_run_record(runs, {"run_id": rid, "command": "test", "status": "ok"})
    assert pl.read_parquet(runs)["run_id"].to_list() == ["r1", "r2"]
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_state.py -v`
Expected: FAIL with `ImportError` (no `dcdr.state`).

- [ ] **Step 3: Implement `src/dcdr/state.py`**

```python
"""Run state: atomic writes, safe directory listing, source manifest and run records."""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

MANIFEST_SCHEMA: dict[str, Any] = {
    "key": pl.String,
    "station_id": pl.String,
    "year": pl.Int32,
    "size": pl.Int64,
    "etag": pl.String,
    "last_modified": pl.Datetime("us", "UTC"),
    "md5": pl.String,
    "retrieved_at": pl.Datetime("us", "UTC"),
    "status": pl.String,
    "rows": pl.Int64,
    "bronze_path": pl.String,
    "run_id": pl.String,
    "error": pl.String,
}

RUNS_SCHEMA: dict[str, Any] = {
    "run_id": pl.String,
    "command": pl.String,
    "started_at": pl.Datetime("us", "UTC"),
    "finished_at": pl.Datetime("us", "UTC"),
    "status": pl.String,
    "git_commit": pl.String,
    "git_dirty": pl.Boolean,
    "config_hash": pl.String,
    "summary": pl.String,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_run_id() -> str:
    return utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def _tmp_path(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_write_parquet(table: pa.Table, path: Path, metadata: dict[str, str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if metadata:
        merged = dict(table.schema.metadata or {})
        merged.update({k.encode(): v.encode() for k, v in metadata.items()})
        table = table.replace_schema_metadata(merged)
    tmp = _tmp_path(path)
    try:
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_parquet_metadata(path: Path) -> dict[str, str]:
    meta = pq.read_schema(path).metadata or {}
    return {k.decode(): v.decode() for k, v in meta.items() if not k.startswith(b"ARROW")}


def safe_listdir(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(
        p for p in path.iterdir() if not p.name.startswith(".") and not p.name.endswith(".tmp")
    )


class Manifest:
    """Source inventory: one row per S3 key (spec §7.1.3)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.df = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=MANIFEST_SCHEMA)

    def get(self, key: str) -> dict[str, Any] | None:
        rows = self.df.filter(pl.col("key") == key)
        return rows.row(0, named=True) if rows.height else None

    def upsert(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        rows = [{k: r.get(k) for k in MANIFEST_SCHEMA} for r in records]
        new = pl.DataFrame(rows, schema=MANIFEST_SCHEMA)
        self.df = pl.concat([self.df, new]).unique(subset="key", keep="last", maintain_order=True)

    def save(self) -> None:
        atomic_write_parquet(self.df.to_arrow(), self.path)


def append_run_record(runs_path: Path, record: dict[str, Any]) -> None:
    row = pl.DataFrame([{k: record.get(k) for k in RUNS_SCHEMA}], schema=RUNS_SCHEMA)
    df = pl.concat([pl.read_parquet(runs_path), row]) if runs_path.exists() else row
    atomic_write_parquet(df.to_arrow(), runs_path)


def git_revision(repo: Path) -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        porcelain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return commit, bool(porcelain)
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_state.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/dcdr/state.py tests/unit/test_state.py
git commit -m "feat: atomic writes, safe listing, source manifest and run records

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Ingest (S3 listing, change detection, MD5, bronze)

**Files:**
- Create: `src/dcdr/ingest.py`, `tests/fixtures/__init__.py`, `tests/fixtures/isd.py`, `tests/unit/test_ingest.py`

**Interfaces:**
- Consumes: `IngestCfg` (Task 2); `Manifest`, `MANIFEST_SCHEMA`, `atomic_write_parquet`, `utcnow` (Task 3).
- Produces:
  - `SourceObject(key, size, etag, last_modified)` with properties `.station_id` and `.year`; `IngestSummary`; `IntegrityError`.
  - `make_store(cfg) -> Any`; `list_year(store, year) -> list[SourceObject]`; `needs_download(obj, previous) -> bool`.
  - `fetch_bytes(store, key, retries) -> bytes`; `verify_md5(data, etag) -> str`.
  - `bronze_path(data_root: Path, year: int, station_id: str) -> Path`; `csv_to_bronze(data, key, md5, run_id) -> pl.DataFrame`.
  - `ingest(store, cfg, data_root, manifest, stations: set[str], years: Iterable[int], run_id) -> IngestSummary`.
  - Test helpers `tests.fixtures.isd.isd_row(date, **fields)`, `make_isd_csv(rows) -> bytes`, `bronze_frame(rows) -> pl.DataFrame`.

- [ ] **Step 1: Write the synthetic ISD builder**

`tests/fixtures/__init__.py`: empty file.

`tests/fixtures/isd.py`:
```python
"""Build small ISD Global Hourly CSV payloads that look exactly like NOAA's files."""

from __future__ import annotations

import io

import polars as pl

BASE_COLUMNS = [
    "STATION", "DATE", "SOURCE", "LATITUDE", "LONGITUDE", "ELEVATION", "NAME", "REPORT_TYPE",
    "CALL_SIGN", "QUALITY_CONTROL", "WND", "CIG", "VIS", "TMP", "DEW", "SLP",
]
DEFAULTS = {
    "STATION": "40582099999", "SOURCE": "4", "LATITUDE": "29.226567", "LONGITUDE": "47.968928",
    "ELEVATION": "62.78", "NAME": "KUWAIT INTL, KU", "REPORT_TYPE": "FM-12", "CALL_SIGN": "99999",
    "QUALITY_CONTROL": "V020", "WND": "090,1,N,0057,1", "CIG": "99999,9,9,N",
    "VIS": "010000,1,N,1", "TMP": "+0304,1", "DEW": "+0047,1", "SLP": "10058,1",
}


def isd_row(date: str, **fields: str) -> dict[str, str]:
    row = dict(DEFAULTS)
    row["DATE"] = date
    row.update(fields)
    return row


def make_isd_csv(rows: list[dict[str, str]]) -> bytes:
    """NOAA quotes every non-empty value and leaves empty values unquoted."""
    extra = sorted({k for r in rows for k in r} - set(BASE_COLUMNS))
    cols = BASE_COLUMNS + extra
    lines = [",".join(f'"{c}"' for c in cols)]
    for r in rows:
        lines.append(",".join(f'"{r[c]}"' if r.get(c, "") != "" else "" for c in cols))
    return ("\n".join(lines) + "\n").encode()


def bronze_frame(rows: list[dict[str, str]]) -> pl.DataFrame:
    return pl.read_csv(io.BytesIO(make_isd_csv(rows)), infer_schema_length=0)
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_ingest.py`:
```python
from datetime import UTC, datetime
from pathlib import Path

import obstore as obs
import pytest
from obstore.store import MemoryStore

from dcdr import ingest as ing
from dcdr.config import Settings
from dcdr.state import Manifest, read_parquet_metadata
from tests.fixtures.isd import isd_row, make_isd_csv

A, B = "40582099999", "72386023169"


def _store() -> MemoryStore:
    store = MemoryStore()
    obs.put(store, f"2024/{A}.csv", make_isd_csv([isd_row("2024-01-01T00:00:00")]))
    obs.put(store, f"2024/{B}.csv", make_isd_csv([isd_row("2024-01-01T00:00:00", STATION=B)]))
    obs.put(store, "2024/readme.txt", b"not a station")
    return store


def _run(settings: Settings, store: MemoryStore, manifest: Manifest) -> ing.IngestSummary:
    return ing.ingest(store, settings.ingest, settings.data_root, manifest, {A}, [2024], "run1")


def test_list_year_keeps_csv_only() -> None:
    keys = sorted(o.key for o in ing.list_year(_store(), 2024))
    assert keys == [f"2024/{A}.csv", f"2024/{B}.csv"]


def test_needs_download_rules() -> None:
    obj = ing.SourceObject("2024/A.csv", 10, "abc", datetime(2025, 1, 1, tzinfo=UTC))
    assert ing.needs_download(obj, None)
    assert not ing.needs_download(obj, {"status": "ok", "etag": "abc", "size": 10})
    assert ing.needs_download(obj, {"status": "ok", "etag": "old", "size": 10})
    assert ing.needs_download(obj, {"status": "failed", "etag": "abc", "size": 10})


def test_verify_md5_checks_only_md5_like_etags() -> None:
    data = b"hello"
    good = "5d41402abc4b2a76b9719d911017c592"
    assert ing.verify_md5(data, good) == good
    assert ing.verify_md5(data, "abc-3") == good  # multipart ETag: not comparable
    with pytest.raises(ing.IntegrityError):
        ing.verify_md5(data, "0" * 32)


def test_ingest_writes_bronze_and_skips_unchanged(settings: Settings) -> None:
    store = _store()
    manifest = Manifest(settings.data_root / "_state" / "sources.parquet")
    first = _run(settings, store, manifest)
    assert (first.selected, first.downloaded, first.failed) == (1, 1, 0)
    path = ing.bronze_path(settings.data_root, 2024, A)
    meta = read_parquet_metadata(path)
    assert meta["dcdr_layer"] == "bronze" and len(meta["source_md5"]) == 32
    row = manifest.get(f"2024/{A}.csv")
    assert row is not None and row["status"] == "ok" and row["rows"] == 1
    second = _run(settings, store, manifest)
    assert (second.downloaded, second.skipped) == (0, 1)


def test_changed_object_is_downloaded_again(settings: Settings) -> None:
    store = _store()
    manifest = Manifest(settings.data_root / "_state" / "sources.parquet")
    _run(settings, store, manifest)
    two_rows = [isd_row("2024-01-01T00:00:00"), isd_row("2024-01-01T03:00:00")]
    obs.put(store, f"2024/{A}.csv", make_isd_csv(two_rows))
    assert _run(settings, store, manifest).downloaded == 1
    row = manifest.get(f"2024/{A}.csv")
    assert row is not None and row["rows"] == 2


def test_failed_download_keeps_previous_bronze(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store()
    manifest = Manifest(settings.data_root / "_state" / "sources.parquet")
    _run(settings, store, manifest)
    path = ing.bronze_path(settings.data_root, 2024, A)
    md5_before = read_parquet_metadata(path)["source_md5"]
    obs.put(store, f"2024/{A}.csv", make_isd_csv([isd_row("2024-02-02T00:00:00")] * 3))

    def boom(*_: object, **__: object) -> bytes:
        raise RuntimeError("network down")

    monkeypatch.setattr(ing, "fetch_bytes", boom)
    summary = _run(settings, store, manifest)
    assert summary.failed == 1
    assert read_parquet_metadata(path)["source_md5"] == md5_before
    row = manifest.get(f"2024/{A}.csv")
    assert row is not None and row["status"] == "failed" and row["md5"] == md5_before


def test_object_removed_upstream_is_marked_not_deleted(settings: Settings) -> None:
    store = _store()
    manifest = Manifest(settings.data_root / "_state" / "sources.parquet")
    _run(settings, store, manifest)
    obs.delete(store, f"2024/{A}.csv")
    summary = _run(settings, store, manifest)
    assert summary.missing_upstream == 1
    row = manifest.get(f"2024/{A}.csv")
    assert row is not None and row["status"] == "missing_upstream"
    assert ing.bronze_path(settings.data_root, 2024, A).exists()


def test_bronze_path_layout(tmp_path: Path) -> None:
    assert ing.bronze_path(tmp_path, 2024, A) == tmp_path / "bronze" / "year=2024" / f"{A}.parquet"
```

- [ ] **Step 3: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_ingest.py -v`
Expected: FAIL with `ImportError` (no `dcdr.ingest`).

- [ ] **Step 4: Implement `src/dcdr/ingest.py`**

```python
"""List NOAA ISD objects on S3, download changed station-years, write bronze (spec §7.1)."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import obstore as obs
import polars as pl
from obstore.store import S3Store

from dcdr.config import IngestCfg
from dcdr.state import MANIFEST_SCHEMA, Manifest, atomic_write_parquet, utcnow

log = logging.getLogger(__name__)
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class SourceObject:
    key: str
    size: int
    etag: str
    last_modified: datetime

    @property
    def station_id(self) -> str:
        return Path(self.key).stem

    @property
    def year(self) -> int:
        return int(self.key.split("/", 1)[0])


@dataclass
class IngestSummary:
    listed: int = 0
    selected: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    missing_upstream: int = 0
    failures: list[str] = field(default_factory=list)


class IntegrityError(RuntimeError):
    """Downloaded bytes do not match the object's MD5 ETag."""


def make_store(cfg: IngestCfg) -> Any:
    return S3Store(cfg.bucket, region=cfg.region, skip_signature=True)


def list_year(store: Any, year: int) -> list[SourceObject]:
    """obstore prefixes match whole path segments, so list the year folder ("2024")."""
    out: list[SourceObject] = []
    for batch in obs.list(store, prefix=str(year), chunk_size=1000):
        for meta in batch:
            key = meta["path"]
            if not key.endswith(".csv"):
                continue
            etag = (meta.get("e_tag") or "").strip('"')
            out.append(SourceObject(key, int(meta["size"]), etag, meta["last_modified"]))
    return out


def needs_download(obj: SourceObject, previous: dict[str, Any] | None) -> bool:
    if previous is None or previous.get("status") != "ok":
        return True
    return previous.get("etag") != obj.etag or previous.get("size") != obj.size


def fetch_bytes(store: Any, key: str, retries: int) -> bytes:
    delay = 0.5
    for attempt in range(retries + 1):
        try:
            return bytes(obs.get(store, key).bytes())
        except Exception:  # obstore network errors have no stable exception type
            if attempt == retries:
                raise
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def verify_md5(data: bytes, etag: str) -> str:
    md5 = hashlib.md5(data).hexdigest()
    if _MD5_RE.match(etag) and md5 != etag:
        raise IntegrityError(f"MD5 {md5} does not match ETag {etag}")
    return md5


def bronze_path(data_root: Path, year: int, station_id: str) -> Path:
    return data_root / "bronze" / f"year={year}" / f"{station_id}.parquet"


def csv_to_bronze(data: bytes, key: str, md5: str, run_id: str) -> pl.DataFrame:
    df = pl.read_csv(io.BytesIO(data), infer_schema_length=0)
    return df.with_columns(
        source_key=pl.lit(key), source_md5=pl.lit(md5), ingest_run_id=pl.lit(run_id)
    )


def ingest(
    store: Any,
    cfg: IngestCfg,
    data_root: Path,
    manifest: Manifest,
    stations: set[str],
    years: Iterable[int],
    run_id: str,
) -> IngestSummary:
    summary = IngestSummary()
    todo: list[SourceObject] = []
    for year in years:
        try:
            listed = list_year(store, year)
        except Exception as exc:  # a failed listing never marks anything removed
            log.error("listing year %s failed: %s", year, exc)
            summary.failures.append(f"list:{year}")
            continue
        summary.listed += len(listed)
        chosen = [o for o in listed if o.station_id in stations]
        summary.selected += len(chosen)
        present = [o.key for o in chosen]
        stale = manifest.df.filter(
            (pl.col("year") == year)
            & pl.col("station_id").is_in(sorted(stations))
            & ~pl.col("key").is_in(present)
            & (pl.col("status") != "missing_upstream")
        )
        if stale.height:
            summary.missing_upstream += stale.height
            manifest.upsert([{**r, "status": "missing_upstream"} for r in stale.iter_rows(named=True)])
        for o in chosen:
            if needs_download(o, manifest.get(o.key)):
                todo.append(o)
            else:
                summary.skipped += 1

    def work(o: SourceObject) -> dict[str, Any]:
        base = {
            "key": o.key, "station_id": o.station_id, "year": o.year, "size": o.size,
            "etag": o.etag, "last_modified": o.last_modified, "retrieved_at": utcnow(),
            "run_id": run_id,
        }
        try:
            data = fetch_bytes(store, o.key, cfg.retries)
            md5 = verify_md5(data, o.etag)
            df = csv_to_bronze(data, o.key, md5, run_id)
            path = bronze_path(data_root, o.year, o.station_id)
            atomic_write_parquet(
                df.to_arrow(),
                path,
                {"dcdr_layer": "bronze", "source_md5": md5, "source_key": o.key, "run_id": run_id},
            )
            return {**base, "md5": md5, "status": "ok", "rows": df.height,
                    "bronze_path": str(path), "error": None}
        except Exception as exc:
            prev = manifest.get(o.key) or {}
            log.error("download %s failed: %s", o.key, exc)
            return {**{k: prev.get(k) for k in MANIFEST_SCHEMA}, **base,
                    "md5": prev.get("md5"), "rows": prev.get("rows"),
                    "bronze_path": prev.get("bronze_path"), "run_id": prev.get("run_id"),
                    "status": "failed", "error": repr(exc)[:500]}

    with ThreadPoolExecutor(max_workers=cfg.max_concurrency) as pool:
        results = list(pool.map(work, todo))
    manifest.upsert(results)
    for r in results:
        if r["status"] == "ok":
            summary.downloaded += 1
        else:
            summary.failed += 1
            summary.failures.append(r["key"])
    return summary
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/unit/test_ingest.py -v`
Expected: PASS (8 passed).

- [ ] **Step 6: Commit**

```bash
git add src/dcdr/ingest.py tests/fixtures tests/unit/test_ingest.py
git commit -m "feat: incremental S3 ingest to per-station-year bronze with MD5 checks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Silver package marker

**Files:**
- Create: `src/dcdr/silver/__init__.py`

**Interfaces:**
- Produces: `dcdr.silver.METHOD_VERSION: str`. Bump it whenever parse, QC, classify or sampling logic changes (spec §11).

- [ ] **Step 1: Create the module**

`src/dcdr/silver/__init__.py`:
```python
"""Silver layer: parse, QC, classify and sample ISD reports (spec §7.2).

Bump METHOD_VERSION whenever parse/QC/classify/sampling logic changes; silver files carry it,
and gold refuses to mix versions.
"""

METHOD_VERSION = "1.0.0"
```

- [ ] **Step 2: Verify the import and commit**

```bash
uv run python -c "from dcdr.silver import METHOD_VERSION; print(METHOD_VERSION)"
```
Expected output: `1.0.0`
```bash
git add src/dcdr/silver/__init__.py
git commit -m "feat: silver package with method version

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Parse packed fields and add time columns

**Files:**
- Create: `src/dcdr/silver/parse.py`, `tests/unit/test_parse.py`

**Interfaces:**
- Consumes: `tests.fixtures.isd.bronze_frame`, `isd_row` (Task 4).
- Produces:
  - Constants `MW_N=7, AU_N=9, AW_N=4, MV_N=7, AY_N=2, OD_N=3, AA_N=4`, `PARSE_BITS`.
  - `parse_reports(raw: pl.DataFrame) -> pl.DataFrame`. Columns:
    - Report and location: `station_id, time_utc, source, report_type, lat, lon, elev, source_key`.
    - Wind, visibility, temperature, pressure: `wind_dir, wind_dir_qc, wind_type, wsd, wsd_qc, vis_raw, vis_qc, t, t_qc, td, td_qc, slp, slp_qc`.
    - Gust sources: `oc1_spd, oc1_qc, od{n}_type, od{n}_spd, od{n}_qc`.
    - Present weather: `mw{n}_code, mw{n}_qc, aw{n}_code, aw{n}_qc, mv{n}_code, mv{n}_qc, au{n}_{int,desc,prec,obsc,other,comb,qc}, ay{n}_code, ay{n}_qc, ay{n}_period_h`.
    - Precipitation and snow: `aa{n}_period_h, aa{n}_mm, aa{n}_qc, aj1_cm, aj1_qc`.
    - Status: `parse_status`, plus `row_key` and `dup_count` if they are present in the input.
  - `add_time_columns(df) -> pl.DataFrame`, which adds `time_lmst, lmst_date, lmst_hour`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_parse.py`:
```python
from datetime import date

import pytest

from dcdr.silver.parse import add_time_columns, parse_reports
from tests.fixtures.isd import bronze_frame, isd_row


def _one(**fields: str) -> dict:
    return parse_reports(bronze_frame([isd_row("2024-07-22T21:00:00", **fields)])).row(0, named=True)


def test_core_values_are_decoded_and_scaled() -> None:
    r = _one(WND="270,5,N,0031,5", VIS="016093,1,9,9", TMP="+0417,1", DEW="-0022,2", SLP="10179,5")
    assert (r["wind_dir"], r["wsd"], r["wsd_qc"]) == (270.0, 3.1, "5")
    assert (r["vis_raw"], r["vis_qc"]) == (16093.0, "1")
    assert (r["t"], r["td"], r["slp"]) == (41.7, -2.2, 1017.9)
    assert r["parse_status"] == 0


def test_missing_sentinels_become_null() -> None:
    r = _one(WND="999,9,9,9999,9", VIS="999999,9,9,9", TMP="+9999,9", DEW="+9999,9", SLP="99999,9")
    assert [r[c] for c in ("wind_dir", "wsd", "vis_raw", "t", "td", "slp")] == [None] * 6
    assert r["parse_status"] == 0


def test_malformed_value_sets_parse_bit() -> None:
    r = _one(WND="090,1,N,AB12,1")
    assert r["wsd"] is None
    assert r["parse_status"] & 1 == 1


def test_report_type_is_stripped() -> None:
    assert _one(REPORT_TYPE="SOD  ")["report_type"] == "SOD"


def test_present_weather_components() -> None:
    r = _one(MW1="07,1", AU1="0,5,00,5,0,1,5", AW1="04,1", MV1="08,6", AY1="3,1,06,1")
    assert (r["mw1_code"], r["mw1_qc"]) == ("07", "1")
    assert (r["au1_int"], r["au1_desc"], r["au1_prec"], r["au1_obsc"], r["au1_other"], r["au1_qc"]) == (
        "0", "5", "00", "5", "0", "5")
    assert (r["aw1_code"], r["mv1_code"], r["ay1_code"], r["ay1_period_h"]) == ("04", "08", "3", 6.0)


def test_gust_sources_are_decoded() -> None:
    r = _one(OC1="0206,1", OD1="4,99,0211,1,350")
    assert (r["oc1_spd"], r["od1_type"], r["od1_spd"], r["od1_qc"]) == (20.6, "4", 21.1, "1")


def test_absent_columns_are_null() -> None:
    r = _one()
    assert r["mw1_code"] is None and r["au9_obsc"] is None and r["aj1_cm"] is None


def test_lmst_uses_each_reports_longitude() -> None:
    raw = bronze_frame([
        isd_row("2024-01-01T01:00:00", LONGITUDE="-115.16343"),
        isd_row("2024-06-01T01:00:00", LONGITUDE="0.0"),  # station moved mid-year
    ])
    df = add_time_columns(parse_reports(raw))
    first, second = df.row(0, named=True), df.row(1, named=True)
    assert first["lmst_date"] == date(2023, 12, 31)
    assert first["lmst_hour"] == pytest.approx(17.3222, abs=1e-3)
    assert second["lmst_date"] == date(2024, 6, 1) and second["lmst_hour"] == pytest.approx(1.0)
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_parse.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dcdr.silver.parse'`.

- [ ] **Step 3: Implement `src/dcdr/silver/parse.py`**

```python
"""Decode ISD packed fields into typed columns and add time columns (spec §7.2.1-7.2.2)."""

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
        _part(d, "WND", 3), _part(d, "VIS", 0), _part(d, "TMP", 0), _part(d, "DEW", 0), _part(d, "SLP", 0)
    )
    exprs: list[pl.Expr] = [
        pl.col("STATION").alias("station_id"),
        pl.col("DATE").str.to_datetime("%Y-%m-%dT%H:%M:%S", time_unit="us", strict=False).alias("time_utc"),
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
    exprs += [_num(_part(d, "AJ1", 0), ("9999",)).alias("aj1_cm"), _part(d, "AJ1", 2).alias("aj1_qc")]
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_parse.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add src/dcdr/silver/parse.py tests/unit/test_parse.py
git commit -m "feat: decode ISD packed fields and add local mean solar time

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: QC flags, RH, visibility cap and gusts

**Files:**
- Create: `src/dcdr/silver/qc.py`, `tests/unit/test_qc.py`

**Interfaces:**
- Consumes: `parse_reports` (Task 6); `QcCfg`, `ParseCfg` (Task 2).
- Produces:
  - `code_flag(code: pl.Expr, cfg: QcCfg) -> pl.Expr`, returning `ok|suspect|erroneous`.
  - `apply_qc(df, qc: QcCfg, parse: ParseCfg) -> pl.DataFrame`, which adds:
    - flags `wind_dir_flag, wsd_flag, vis_flag, t_flag, td_flag, slp_flag`;
    - `use_wind_dir, use_wsd, use_vis, use_t, use_td, use_slp`;
    - `rh, vis_capped, gust, use_gust, gust_src`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_qc.py`:
```python
import polars as pl
import pytest

from dcdr.config import Settings
from dcdr.silver.parse import parse_reports
from dcdr.silver.qc import apply_qc, code_flag
from tests.fixtures.isd import bronze_frame, isd_row


def _qc(settings: Settings, **fields: str) -> dict:
    df = parse_reports(bronze_frame([isd_row("2024-07-22T21:00:00", **fields)]))
    return apply_qc(df, settings.qc, settings.parse).row(0, named=True)


@pytest.mark.parametrize(
    "code,flag", [("1", "ok"), ("5", "ok"), ("A", "ok"), ("M", "ok"), ("2", "suspect"),
                  ("6", "suspect"), ("3", "erroneous"), ("7", "erroneous"), ("Z", "suspect")],
)
def test_code_flag_mapping(settings: Settings, code: str, flag: str) -> None:
    out = pl.DataFrame({"c": [code]}).select(code_flag(pl.col("c"), settings.qc)).item()
    assert out == flag


def test_out_of_range_values_are_erroneous(settings: Settings) -> None:
    r = _qc(settings, WND="090,1,N,0800,1", TMP="-0950,1")
    assert (r["wsd_flag"], r["use_wsd"]) == ("erroneous", False)
    assert r["t_flag"] == "erroneous"


def test_dewpoint_above_temperature_is_erroneous(settings: Settings) -> None:
    r = _qc(settings, TMP="+0200,1", DEW="+0210,1")
    assert r["td_flag"] == "erroneous" and r["rh"] is None


def test_rh_uses_magnus_formula(settings: Settings) -> None:
    assert _qc(settings, TMP="+0300,1", DEW="+0200,1")["rh"] == pytest.approx(55.07, abs=0.05)


def test_visibility_is_capped_and_needs_good_qc(settings: Settings) -> None:
    assert _qc(settings, VIS="016093,1,N,1")["vis_capped"] == 10000
    assert _qc(settings, VIS="005000,1,N,1")["vis_capped"] == 5000
    suspect = _qc(settings, VIS="005000,2,N,1")
    assert suspect["vis_flag"] == "suspect" and suspect["vis_capped"] is None


def test_gust_uses_od_types_3_and_4_only(settings: Settings) -> None:
    r = _qc(settings, OC1="0206,1", OD1="4,99,0211,1,350")
    assert (r["gust"], r["gust_src"], r["use_gust"]) == (21.1, "OD", True)
    r = _qc(settings, OD1="1,99,0300,1,350")
    assert r["gust"] is None and r["use_gust"] is False
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_qc.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dcdr.silver.qc'`.

- [ ] **Step 3: Implement `src/dcdr/silver/qc.py`**

```python
"""Quality-control flags, RH, harmonized visibility and gusts (spec §7.2.3). Never deletes rows."""

from __future__ import annotations

import polars as pl

from dcdr.config import ParseCfg, QcCfg

RANGES: dict[str, tuple[float, float]] = {
    "wind_dir": (0, 360), "wsd": (0, 75), "vis_raw": (0, 160_000),
    "t": (-90, 60), "td": (-100, 40), "slp": (850, 1090),
}
_QC_COL = {"wind_dir": "wind_dir_qc", "wsd": "wsd_qc", "vis_raw": "vis_qc",
           "t": "t_qc", "td": "td_qc", "slp": "slp_qc"}
_FLAG = {"wind_dir": "wind_dir", "wsd": "wsd", "vis_raw": "vis", "t": "t", "td": "td", "slp": "slp"}
GUST_RANGE = (0.0, 100.0)


def code_flag(code: pl.Expr, cfg: QcCfg) -> pl.Expr:
    return (
        pl.when(code.is_in(cfg.codes_ok)).then(pl.lit("ok"))
        .when(code.is_in(cfg.codes_suspect)).then(pl.lit("suspect"))
        .when(code.is_in(cfg.codes_erroneous)).then(pl.lit("erroneous"))
        .otherwise(pl.lit("suspect"))
    )


def _value_flag(value: pl.Expr, code: pl.Expr, lo: float, hi: float, cfg: QcCfg) -> pl.Expr:
    return (
        pl.when(value.is_null()).then(pl.lit("missing"))
        .when((value < lo) | (value > hi)).then(pl.lit("erroneous"))
        .otherwise(code_flag(code, cfg))
    )


def _magnus_es(t: pl.Expr) -> pl.Expr:
    """Saturation vapour pressure (hPa), Alduchov & Eskridge (1996)."""
    return 6.1094 * (17.625 * t / (t + 243.04)).exp()


def apply_qc(df: pl.DataFrame, qc: QcCfg, parse: ParseCfg) -> pl.DataFrame:
    df = df.with_columns(
        [_value_flag(pl.col(v), pl.col(_QC_COL[v]), lo, hi, qc).alias(f"{_FLAG[v]}_flag")
         for v, (lo, hi) in RANGES.items()]
    )
    t_ok = pl.col("t_flag").is_in(qc.accept)
    df = df.with_columns(
        td_flag=pl.when(t_ok & (pl.col("td") > pl.col("t") + 0.5))
        .then(pl.lit("erroneous"))
        .otherwise(pl.col("td_flag"))
    )
    df = df.with_columns([pl.col(f"{n}_flag").is_in(qc.accept).alias(f"use_{n}") for n in _FLAG.values()])
    rh = 100 * _magnus_es(pl.col("td")) / _magnus_es(pl.col("t"))
    df = df.with_columns(
        rh=pl.when(pl.col("use_t") & pl.col("use_td")).then(rh).otherwise(None),
        vis_capped=pl.when(pl.col("use_vis"))
        .then(pl.min_horizontal(pl.col("vis_raw"), pl.lit(qc.vis_cap_m)))
        .otherwise(None),
    )
    return _add_gust(df, qc, parse)


def _add_gust(df: pl.DataFrame, qc: QcCfg, parse: ParseCfg) -> pl.DataFrame:
    gust_types = [str(t) for t in parse.gust_od_types]

    def valid(spd: str, code: str) -> pl.Expr:
        return (
            pl.col(spd).is_not_null()
            & pl.col(spd).is_between(*GUST_RANGE)
            & code_flag(pl.col(code), qc).is_in(qc.accept)
        )

    cands = [pl.when(valid("oc1_spd", "oc1_qc")).then(pl.col("oc1_spd"))]
    for n in range(1, 4):
        ok = valid(f"od{n}_spd", f"od{n}_qc") & pl.col(f"od{n}_type").is_in(gust_types)
        cands.append(pl.when(ok).then(pl.col(f"od{n}_spd")))
    df = df.with_columns(gust=pl.max_horizontal(cands))
    return df.with_columns(
        use_gust=pl.col("gust").is_not_null(),
        gust_src=pl.when(pl.col("gust").is_null()).then(None)
        .when(cands[0] == pl.col("gust")).then(pl.lit("OC1"))
        .otherwise(pl.lit("OD")),
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_qc.py -v`
Expected: PASS (14 passed).

- [ ] **Step 5: Commit**

```bash
git add src/dcdr/silver/qc.py tests/unit/test_qc.py
git commit -m "feat: QC flags, Magnus RH, capped visibility and OD type 3/4 gusts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Per-field dust classification

**Files:**
- Create: `src/dcdr/silver/classify.py`, `tests/unit/test_classify.py`

**Interfaces:**
- Consumes: `parse_reports`, `add_time_columns` (Task 6); `apply_qc`, `code_flag` (Task 7); `ClassifyCfg`, `QcCfg` (Task 2).
- Produces:
  - `RANK: dict[str, int]`, `FINAL_CLASSES: list[str]`, `DUSTY_CLASSES: list[str]`.
  - `classify_reports(df, cfg: ClassifyCfg, qc: QcCfg) -> pl.DataFrame`, which adds:
    - `has_pw, dust_rank, dust_class_rule, dust_rule_id` (format `"<FIELD>:<class>"`);
    - `flag_du, flag_susp, flag_bd, flag_ds, flag_vicinity, flag_past, flag_ambiguous`;
    - `pw_evidence: list[str]`.

- [ ] **Step 1: Write the failing tests (one per code-table row in spec §7.2.4)**

`tests/unit/test_classify.py`:
```python
import pytest

from dcdr.config import Settings, load_settings
from dcdr.silver.classify import classify_reports
from dcdr.silver.parse import add_time_columns, parse_reports
from dcdr.silver.qc import apply_qc
from tests.fixtures.isd import bronze_frame, isd_row


def _cls(settings: Settings, **fields: str) -> dict:
    df = add_time_columns(parse_reports(bronze_frame([isd_row("2024-07-22T21:00:00", **fields)])))
    df = apply_qc(df, settings.qc, settings.parse)
    return classify_reports(df, settings.classify, settings.qc).row(0, named=True)


TABLE = [
    ("MW1", "05,1", "haze"), ("MW1", "06,1", "suspended"), ("MW1", "07,1", "blowing"),
    ("MW1", "08,1", "whirls"), ("MW1", "09,1", "storm"), ("MW1", "30,1", "storm"),
    ("MW1", "31,1", "storm"), ("MW1", "32,1", "storm"), ("MW1", "33,1", "storm_severe"),
    ("MW1", "34,1", "storm_severe"), ("MW1", "35,1", "storm_severe"),
    ("MW1", "98,1", "storm_thunder"), ("MW1", "04,1", "none"),
    ("AW1", "04,1", "haze_or_suspended"), ("AW1", "07,1", "blowing"), ("AW1", "05,1", "none"),
    ("AW1", "30,1", "none"), ("AW1", "31,1", "none"), ("AW1", "32,1", "none"),
    ("AW1", "33,1", "none"), ("AW1", "34,1", "none"), ("AW1", "35,1", "none"),
    ("AU1", "0,0,00,0,4,1,1", "storm"), ("AU1", "0,0,00,0,5,1,1", "storm"),
    ("AU1", "3,0,00,0,5,1,1", "storm_severe"), ("AU1", "0,7,00,0,5,1,1", "storm_thunder"),
    ("AU1", "0,0,00,0,1,1,1", "whirls"), ("AU1", "0,5,00,5,0,1,1", "blowing"),
    ("AU1", "0,4,00,6,0,1,1", "blowing"), ("AU1", "0,0,00,5,0,1,1", "suspended"),
    ("AU1", "0,0,00,6,0,1,1", "suspended"), ("AU1", "0,0,00,7,0,1,1", "haze"),
]


@pytest.mark.parametrize("field,value,expected", TABLE)
def test_code_table_row(settings: Settings, field: str, value: str, expected: str) -> None:
    assert _cls(settings, **{field: value})["dust_class_rule"] == expected


def test_aw_fog_codes_are_never_dust(settings: Settings) -> None:
    for code in ("30", "31", "32", "33", "34", "35"):
        r = _cls(settings, AW1=f"{code},1")
        assert r["flag_du"] is False and r["flag_ds"] is False


@pytest.mark.parametrize(
    "tmp,extra,cls,ambiguous",
    [("+0304,1", {}, "blowing", False), ("-0050,1", {}, "none", False),
     ("+0015,1", {}, "none", True), ("+0050,1", {"MW1": "71,1"}, "none", True)],
)
def test_aw_blowing_snow_or_sand_resolved_by_temperature(
    settings: Settings, tmp: str, extra: dict, cls: str, ambiguous: bool
) -> None:
    r = _cls(settings, AW1="27,1", TMP=tmp, DEW="-0100,1", **extra)
    assert (r["dust_class_rule"], r["flag_ambiguous"]) == (cls, ambiguous)


def test_vicinity_is_evidence_not_station_dust(settings: Settings) -> None:
    r = _cls(settings, AU1="4,5,00,5,0,1,1")
    assert (r["dust_class_rule"], r["flag_vicinity"], r["flag_du"]) == ("none", True, False)
    r = _cls(settings, MW1="02,1", MV1="08,1")
    assert (r["dust_class_rule"], r["flag_vicinity"]) == ("none", True)


def test_past_weather_sandstorm_is_evidence(settings: Settings) -> None:
    r = _cls(settings, MW1="02,1", AY1="3,1,06,1")
    assert (r["flag_past"], r["flag_du"]) == (True, False)


def test_precedence_and_rule_id(settings: Settings) -> None:
    r = _cls(settings, MW1="06,1", AW1="07,1")
    assert (r["dust_class_rule"], r["dust_rule_id"]) == ("blowing", "AW:blowing")
    r = _cls(settings, MW1="07,1", AW1="07,1")
    assert r["dust_rule_id"] == "MW:blowing"
    assert sorted(r["pw_evidence"]) == ["AW1=07", "MW1=07"]


def test_fields_restriction() -> None:
    s = load_settings(overrides={"classify": {"fields": ["MW"]}})
    assert _cls(s, MW1="02,1", AW1="07,1")["dust_class_rule"] == "none"


def test_rejected_qc_and_no_present_weather(settings: Settings) -> None:
    assert _cls(settings, MW1="06,3")["dust_class_rule"] == "unknown"
    r = _cls(settings)
    assert (r["dust_class_rule"], r["has_pw"]) == ("unknown", False)


def test_flags_follow_classes(settings: Settings) -> None:
    r = _cls(settings, MW1="06,1")
    assert (r["flag_du"], r["flag_susp"], r["flag_bd"], r["flag_ds"]) == (True, True, False, False)
    r = _cls(settings, MW1="08,1")
    assert (r["flag_bd"], r["flag_ds"]) == (True, False)
    r = _cls(settings, MW1="98,1")
    assert (r["flag_ds"], r["flag_du"]) == (True, True)
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_classify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dcdr.silver.classify'`.

- [ ] **Step 3: Implement `src/dcdr/silver/classify.py` (classification part)**

```python
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
    "none": 0, "haze": 1, "haze_or_suspended": 2, "suspended": 3, "blowing": 4,
    "whirls": 5, "storm": 6, "storm_severe": 7, "storm_thunder": 8,
}
CLASS_BY_RANK: dict[int, str] = {v: k for k, v in RANK.items()}
FINAL_CLASSES = [*RANK, "dust_haze", "screened", "unknown"]
DUSTY_CLASSES = ["suspended", "blowing", "whirls", "storm", "storm_severe", "storm_thunder", "dust_haze"]

MW_TABLE = {
    "05": "haze", "06": "suspended", "07": "blowing", "08": "whirls", "09": "storm",
    "30": "storm", "31": "storm", "32": "storm",
    "33": "storm_severe", "34": "storm_severe", "35": "storm_severe", "98": "storm_thunder",
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
        pl.when(storm & (de == "7")).then(pl.lit(RANK["storm_thunder"]))
        .when(storm & (i == "3")).then(pl.lit(RANK["storm_severe"]))
        .when(storm).then(pl.lit(RANK["storm"]))
        .when(ot == "1").then(pl.lit(RANK["whirls"]))
        .when(dust_obsc & de.is_in(["4", "5"])).then(pl.lit(RANK["blowing"]))
        .when(dust_obsc).then(pl.lit(RANK["suspended"]))
        .when(ob == "7").then(pl.lit(RANK["haze"]))
        .otherwise(pl.lit(0))
        .cast(pl.Int8)
    )


def _au_code(n: int) -> pl.Expr:
    return pl.concat_str(
        [pl.col(f"au{n}_{p}") for p in ("int", "desc", "prec", "obsc", "other")], separator="/"
    )


def classify_reports(df: pl.DataFrame, cfg: ClassifyCfg, qc: QcCfg) -> pl.DataFrame:
    mw_ok = {n: pl.col(f"mw{n}_code").is_not_null() & _accepted(f"mw{n}_qc", qc) for n in range(1, MW_N + 1)}
    au_ok = {n: pl.col(f"au{n}_obsc").is_not_null() & _accepted(f"au{n}_qc", qc) for n in range(1, AU_N + 1)}
    aw_ok = {n: pl.col(f"aw{n}_code").is_not_null() & _accepted(f"aw{n}_qc", qc) for n in range(1, AW_N + 1)}
    mv_ok = {n: pl.col(f"mv{n}_code").is_not_null() & _accepted(f"mv{n}_qc", qc) for n in range(1, MV_N + 1)}
    ay_ok = {n: pl.col(f"ay{n}_code").is_not_null() & _accepted(f"ay{n}_qc", qc) for n in range(1, AY_N + 1)}

    snow = pl.any_horizontal(
        [ok & pl.col(f"mw{n}_code").is_in(MW_SNOW) for n, ok in mw_ok.items()]
        + [ok & (pl.col(f"au{n}_prec") == "03") for n, ok in au_ok.items()]
    ).fill_null(False)
    df = df.with_columns(_snow=snow)
    df = df.with_columns(
        _sand=(pl.col("use_t") & (pl.col("t") >= cfg.sand_min_t_c) & ~pl.col("_snow")).fill_null(False),
        _snowt=(pl.col("use_t") & (pl.col("t") <= cfg.snow_max_t_c)).fill_null(False),
    )

    mw_rank = {n: pl.when(ok).then(_table_rank(f"mw{n}_code", MW_TABLE)).cast(pl.Int8) for n, ok in mw_ok.items()}
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
        rule = pl.when((fam_rank[f] == rank) & (rank > 0)).then(pl.lit(f"{f}:") + cls).otherwise(rule)

    evidence: list[pl.Expr] = []
    for n, r in mw_rank.items():
        evidence.append(pl.when(r > 0).then(pl.lit(f"MW{n}=") + pl.col(f"mw{n}_code")))
    for n, r in au_rank.items():
        evidence.append(pl.when((r > 0) | au_vic[n]).then(pl.lit(f"AU{n}=") + _au_code(n)))
    for n, r in aw_rank.items():
        amb = aw_ok[n] & pl.col(f"aw{n}_code").is_in(AW_SNOW_OR_SAND)
        evidence.append(pl.when((r > 0) | amb).then(pl.lit(f"AW{n}=") + pl.col(f"aw{n}_code")))
    for n, ok in mv_ok.items():
        evidence.append(pl.when(ok & pl.col(f"mv{n}_code").is_in(MV_VICINITY)).then(pl.lit(f"MV{n}=") + pl.col(f"mv{n}_code")))
    for n, ok in ay_ok.items():
        evidence.append(pl.when(ok & (pl.col(f"ay{n}_code") == "3")).then(pl.lit(f"AY{n}=3")))

    vicinity = pl.any_horizontal(
        list(au_vic.values()) + [ok & pl.col(f"mv{n}_code").is_in(MV_VICINITY) for n, ok in mv_ok.items()]
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_classify.py -v`
Expected: PASS (43 passed: 32 table rows + 11 others).

- [ ] **Step 5: Commit**

```bash
git add src/dcdr/silver/classify.py tests/unit/test_classify.py
git commit -m "feat: per-field dust code tables with precedence, evidence and snow/sand rule

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Haze rule and RH screen

**Files:**
- Modify: `src/dcdr/silver/classify.py` (append the function below)
- Create: `tests/unit/test_haze_rh.py`

**Interfaces:**
- Consumes: `classify_reports` output (Task 8). The input **must be sorted by `time_utc`** and contain a single station.
- Produces: `apply_haze_and_rh(df, cfg: ClassifyCfg, qc: QcCfg) -> pl.DataFrame`, which adds `dust_haze, flag_duhz, rh_screened, dust_class_final`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_haze_rh.py`:
```python
import polars as pl

from dcdr.config import Settings, load_settings
from dcdr.silver.classify import apply_haze_and_rh, classify_reports
from dcdr.silver.parse import add_time_columns, parse_reports
from dcdr.silver.qc import apply_qc
from tests.fixtures.isd import bronze_frame, isd_row


def _run(settings: Settings, rows: list[dict]) -> pl.DataFrame:
    df = add_time_columns(parse_reports(bronze_frame(rows)))
    df = apply_qc(df, settings.qc, settings.parse).sort("time_utc").with_columns(pl.col("time_utc").set_sorted())
    df = classify_reports(df, settings.classify, settings.qc)
    return apply_haze_and_rh(df, settings.classify, settings.qc)


def test_haze_within_24h_after_dust_becomes_dust_haze(settings: Settings) -> None:
    out = _run(settings, [isd_row("2024-03-01T00:00:00", MW1="06,1"),
                          isd_row("2024-03-01T23:59:00", MW1="05,1")])
    assert out["dust_class_final"].to_list() == ["suspended", "dust_haze"]
    assert out["flag_duhz"].to_list() == [True, True]


def test_window_excludes_exactly_24h(settings: Settings) -> None:
    out = _run(settings, [isd_row("2024-03-01T00:00:00", MW1="06,1"),
                          isd_row("2024-03-02T00:00:00", MW1="05,1")])
    assert out["dust_class_final"].to_list() == ["suspended", "haze"]


def test_haze_without_prior_dust_stays_haze(settings: Settings) -> None:
    out = _run(settings, [isd_row("2024-03-01T00:00:00", MW1="05,1")])
    assert (out["dust_class_final"].item(), out["flag_duhz"].item()) == ("haze", False)


def test_zero_window_disables_rule() -> None:
    s = load_settings(overrides={"classify": {"haze_window_h": 0}})
    out = _run(s, [isd_row("2024-03-01T00:00:00", MW1="06,1"), isd_row("2024-03-01T01:00:00", MW1="05,1")])
    assert out["dust_class_final"].to_list() == ["suspended", "haze"]


def test_rh_screen_applies_to_suspended_not_blowing(settings: Settings) -> None:
    humid = {"TMP": "+0200,1", "DEW": "+0195,1"}  # RH ~ 97 %
    out = _run(settings, [isd_row("2024-03-01T00:00:00", MW1="06,1", **humid),
                          isd_row("2024-03-01T03:00:00", MW1="07,1", **humid)])
    assert out["dust_class_rule"].to_list() == ["suspended", "blowing"]
    assert out["dust_class_final"].to_list() == ["screened", "blowing"]
    assert out["rh_screened"].to_list() == [True, False]
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_haze_rh.py -v`
Expected: FAIL with `ImportError: cannot import name 'apply_haze_and_rh'`.

- [ ] **Step 3: Append to `src/dcdr/silver/classify.py`**

```python
def apply_haze_and_rh(df: pl.DataFrame, cfg: ClassifyCfg, qc: QcCfg) -> pl.DataFrame:
    """Haze rule (window (t-24h, t], same station, unscreened dust) then RH screen.

    `df` must hold one station and be sorted by `time_utc`.
    """
    haze_like = pl.col("dust_class_rule").is_in(["haze", "haze_or_suspended"])
    if cfg.haze_window_h > 0:
        recent = (
            pl.col("flag_du").cast(pl.Int8)
            .rolling_max_by("time_utc", window_size=f"{cfg.haze_window_h}h", closed="right")
            == 1
        )
        dust_haze = (haze_like & recent).fill_null(False)
    else:
        dust_haze = pl.lit(False)
    df = df.with_columns(dust_haze=dust_haze)
    df = df.with_columns(flag_duhz=pl.col("flag_du") | pl.col("dust_haze"))
    pre = pl.when(pl.col("dust_haze")).then(pl.lit("dust_haze")).otherwise(pl.col("dust_class_rule"))
    screened = (
        pl.col("rh").is_not_null() & (pl.col("rh") > qc.rh_screen_pct) & pre.is_in(qc.rh_screen_classes)
    ).fill_null(False)
    df = df.with_columns(rh_screened=screened)
    return df.with_columns(
        dust_class_final=pl.when(pl.col("rh_screened")).then(pl.lit("screened")).otherwise(pre)
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_haze_rh.py tests/unit/test_classify.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/dcdr/silver/classify.py tests/unit/test_haze_rh.py
git commit -m "feat: 24h haze rule and class-limited RH screen

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Synoptic sampling

**Files:**
- Create: `src/dcdr/silver/sampling.py`, `tests/unit/test_sampling.py`

**Interfaces:**
- Consumes: the columns `station_id, time_utc, report_type, source, has_pw, row_key`; `SamplingCfg` (Task 2).
- Produces: `select_synoptic(df, cfg: SamplingCfg) -> pl.DataFrame`, which adds `slot_time, slot_delta_min, is_excluded_type, is_candidate, selection_rank (Int32, null for non-candidates), selection_reason, is_synoptic_selected`. Row order of the output is not guaranteed; callers re-sort.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_sampling.py`:
```python
import random
from datetime import datetime

import polars as pl

from dcdr.config import Settings
from dcdr.silver.sampling import select_synoptic


def _frame(rows: list[tuple[str, str, bool, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_id": ["S"] * len(rows),
            "time_utc": [datetime.fromisoformat(t) for t, _, _, _ in rows],
            "report_type": [rt for _, rt, _, _ in rows],
            "has_pw": [pw for _, _, pw, _ in rows],
            "source": ["4"] * len(rows),
            "row_key": [k for _, _, _, k in rows],
        }
    )


def _selected(df: pl.DataFrame) -> list[str]:
    return sorted(df.filter(pl.col("is_synoptic_selected"))["row_key"].to_list())


def test_slot_and_window(settings: Settings) -> None:
    out = select_synoptic(_frame([("2024-01-01T02:40:00", "FM-15", True, "a"),
                                  ("2024-01-01T01:20:00", "FM-15", True, "b")]), settings.sampling)
    rows = {r["row_key"]: r for r in out.iter_rows(named=True)}
    assert rows["a"]["slot_time"] == datetime(2024, 1, 1, 3) and rows["a"]["is_candidate"]
    assert rows["b"]["is_candidate"] is False and rows["b"]["selection_rank"] is None


def test_priority_beats_distance(settings: Settings) -> None:
    out = select_synoptic(_frame([("2024-01-01T03:00:00", "FM-15", True, "metar"),
                                  ("2024-01-01T03:20:00", "FM-12", True, "synop")]), settings.sampling)
    assert _selected(out) == ["synop"]


def test_present_weather_breaks_equal_priority(settings: Settings) -> None:
    out = select_synoptic(_frame([("2024-01-01T03:00:00", "FM-15", False, "nopw"),
                                  ("2024-01-01T03:10:00", "FM-15", True, "pw")]), settings.sampling)
    assert _selected(out) == ["pw"]


def test_excluded_types_never_selected(settings: Settings) -> None:
    out = select_synoptic(_frame([("2024-01-01T03:00:00", "SOD", True, "sod")]), settings.sampling)
    row = out.row(0, named=True)
    assert row["is_excluded_type"] and not row["is_synoptic_selected"]


def test_selection_is_independent_of_row_order(settings: Settings) -> None:
    rows = [(f"2024-01-01T{h:02d}:{m:02d}:00", rt, pw, f"k{h}{m}{rt}")
            for h in range(0, 24) for m in (0, 10, 50) for rt, pw in (("FM-15", True), ("FM-16", False))]
    base = _selected(select_synoptic(_frame(rows), settings.sampling))
    for seed in range(3):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        assert _selected(select_synoptic(_frame(shuffled), settings.sampling)) == base
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_sampling.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dcdr.silver.sampling'`.

- [ ] **Step 3: Implement `src/dcdr/silver/sampling.py`**

```python
"""Synoptic-slot candidates, deterministic ranking and selection (spec §7.2.7)."""

from __future__ import annotations

import polars as pl

from dcdr.config import SamplingCfg

_ORDER = ["station_id", "slot_time", "priority_rank", "has_pw", "slot_delta_min", "time_utc", "source", "row_key"]
_DESC = [False, False, False, True, False, False, False, False]


def select_synoptic(df: pl.DataFrame, cfg: SamplingCfg) -> pl.DataFrame:
    prio = {t: i for i, t in enumerate(cfg.priority)}
    df = df.with_columns(slot_time=(pl.col("time_utc") + pl.duration(minutes=90)).dt.truncate("3h"))
    df = df.with_columns(
        slot_delta_min=(pl.col("time_utc") - pl.col("slot_time")).dt.total_seconds().abs() / 60,
        is_excluded_type=pl.col("report_type").is_in(cfg.exclude_types).fill_null(False),
        priority_rank=pl.col("report_type")
        .replace_strict(prio, default=len(prio), return_dtype=pl.Int32)
        .fill_null(len(prio)),
    )
    df = df.with_columns(
        is_candidate=(pl.col("slot_delta_min") <= cfg.window_min) & ~pl.col("is_excluded_type")
    )
    ranked = (
        df.filter(pl.col("is_candidate"))
        .sort(_ORDER, descending=_DESC, nulls_last=True)
        .with_columns(
            selection_rank=(pl.int_range(pl.len()).over(["station_id", "slot_time"]) + 1).cast(pl.Int32)
        )
        .select("row_key", "selection_rank")
    )
    df = df.join(ranked, on="row_key", how="left")
    return df.with_columns(
        is_synoptic_selected=(pl.col("selection_rank") == 1).fill_null(False),
        selection_reason=pl.when(pl.col("selection_rank").is_null())
        .then(None)
        .otherwise(
            pl.format(
                "rank={};type={};pw={};dt_min={}",
                pl.col("selection_rank"), pl.col("report_type"), pl.col("has_pw"),
                pl.col("slot_delta_min").round(1),
            )
        ),
    ).drop("priority_rank")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_sampling.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/dcdr/silver/sampling.py tests/unit/test_sampling.py
git commit -m "feat: deterministic synoptic-slot selection with audit reasons

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Silver station-year build, contract and writer

**Files:**
- Create: `src/dcdr/silver/build.py`, `src/dcdr/schemas.py`, `tests/unit/test_silver_build.py`

**Interfaces:**
- Consumes: Tasks 6–10; `atomic_write_parquet` (Task 3); `METHOD_VERSION` (Task 5); `Settings` (Task 2).
- Produces:
  - `dcdr.silver.build.CORE_COLUMNS: list[str]`; `previous_year_tail(prev, year) -> pl.DataFrame`; `dedupe(raw) -> pl.DataFrame`.
  - `build_station_year(current: pl.DataFrame, previous: pl.DataFrame | None, year: int, settings: Settings) -> pl.DataFrame`.
  - `write_silver(df, path: Path, *, run_id: str, config_hash: str, source_md5: str, prev_source_md5: str) -> None`.
  - `dcdr.schemas.SILVER_SCHEMA`, `SilverContractError`, `validate_silver(df) -> pl.DataFrame`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_silver_build.py`:
```python
import random
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from dcdr.config import Settings
from dcdr.schemas import SilverContractError, validate_silver
from dcdr.silver import METHOD_VERSION
from dcdr.silver.build import build_station_year, write_silver
from dcdr.state import read_parquet_metadata
from tests.fixtures.isd import bronze_frame, isd_row


def _day(date: str, **fields: str) -> list[dict]:
    return [isd_row(f"{date}T{h:02d}:00:00", **fields) for h in range(0, 24, 3)]


def test_haze_rule_reaches_back_across_new_year(settings: Settings) -> None:
    prev = bronze_frame([isd_row("2023-12-31T22:00:00", MW1="06,1")])
    cur = bronze_frame([isd_row("2024-01-01T00:00:00", MW1="05,1")])
    out = build_station_year(cur, prev, 2024, settings)
    assert out.height == 1  # look-back rows are not kept
    assert out["dust_class_final"].item() == "dust_haze"
    assert build_station_year(cur, None, 2024, settings)["dust_class_final"].item() == "haze"


def test_exact_duplicates_collapse_with_count(settings: Settings) -> None:
    row = isd_row("2024-01-01T00:00:00", MW1="06,1")
    out = build_station_year(bronze_frame([row, row]), None, 2024, settings)
    assert out.height == 1 and out["dup_count"].item() == 2


def test_output_independent_of_input_row_order(settings: Settings) -> None:
    rows = _day("2024-01-01", MW1="02,1") + [isd_row("2024-01-01T03:10:00", REPORT_TYPE="FM-15", MW1="06,1")]
    base = build_station_year(bronze_frame(rows), None, 2024, settings)
    shuffled = rows[:]
    random.Random(1).shuffle(shuffled)
    assert_frame_equal(build_station_year(bronze_frame(shuffled), None, 2024, settings), base)


def test_bad_dates_are_dropped_others_kept(settings: Settings) -> None:
    rows = [isd_row("2024-01-01T00:00:00"), isd_row("not-a-date")]
    assert build_station_year(bronze_frame(rows), None, 2024, settings).height == 1


def test_header_only_bronze_builds_empty_silver(settings: Settings, tmp_path: Path) -> None:
    empty = bronze_frame([isd_row("2024-01-01T00:00:00")]).clear()
    out = build_station_year(empty, None, 2024, settings)
    assert out.height == 0
    write_silver(out, tmp_path / "s.parquet", run_id="r", config_hash="h", source_md5="m", prev_source_md5="")
    assert pl.read_parquet(tmp_path / "s.parquet").height == 0


def test_write_silver_records_metadata(settings: Settings, tmp_path: Path) -> None:
    out = build_station_year(bronze_frame(_day("2024-01-01")), None, 2024, settings)
    path = tmp_path / "silver.parquet"
    write_silver(out, path, run_id="r1", config_hash="abc", source_md5="m1", prev_source_md5="m0")
    meta = read_parquet_metadata(path)
    assert meta == {"dcdr_layer": "silver", "method_version": METHOD_VERSION, "config_hash": "abc",
                    "run_id": "r1", "source_md5": "m1", "prev_source_md5": "m0"}


def test_contract_rejects_two_selected_reports_in_one_slot(settings: Settings) -> None:
    out = build_station_year(bronze_frame(_day("2024-01-01")), None, 2024, settings)
    broken = out.with_columns(is_synoptic_selected=pl.lit(True))
    with pytest.raises(SilverContractError):
        validate_silver(broken)
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_silver_build.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dcdr.schemas'`.

- [ ] **Step 3: Implement `src/dcdr/schemas.py`**

```python
"""Data contracts (spec §7.2, §13). Column checks via Pandera; cross-row checks in plain Polars."""

from __future__ import annotations

import pandera.polars as pa
import polars as pl

from dcdr.silver.classify import FINAL_CLASSES

SILVER_SCHEMA = pa.DataFrameSchema(
    {
        "station_id": pa.Column(pl.String, nullable=False),
        "time_utc": pa.Column(pl.Datetime("us"), nullable=False),
        "lmst_date": pa.Column(pl.Date, nullable=False),
        "lmst_hour": pa.Column(pl.Float64, pa.Check.in_range(0, 24, include_max=False)),
        "dust_class_final": pa.Column(pl.String, pa.Check.isin(FINAL_CLASSES)),
        "is_synoptic_selected": pa.Column(pl.Boolean),
        "has_pw": pa.Column(pl.Boolean),
        "wsd": pa.Column(pl.Float64, nullable=True),
        "vis_capped": pa.Column(pl.Float64, nullable=True),
        "row_key": pa.Column(pl.String, nullable=False, unique=True),
    },
    strict=False,
)


class SilverContractError(ValueError):
    """Silver data violate a cross-row rule."""


def validate_silver(df: pl.DataFrame) -> pl.DataFrame:
    SILVER_SCHEMA.validate(df)
    dup = (
        df.filter(pl.col("is_synoptic_selected"))
        .group_by(["station_id", "slot_time"])
        .len()
        .filter(pl.col("len") > 1)
    )
    if dup.height:
        raise SilverContractError(f"{dup.height} synoptic slots have more than one selected report")
    return df
```

- [ ] **Step 4: Implement `src/dcdr/silver/build.py`**

```python
"""Assemble one silver station-year from bronze (spec §7.2)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import polars as pl

from dcdr.config import Settings
from dcdr.schemas import validate_silver
from dcdr.silver import METHOD_VERSION
from dcdr.silver.classify import apply_haze_and_rh, classify_reports
from dcdr.silver.parse import add_time_columns, parse_reports
from dcdr.silver.qc import apply_qc
from dcdr.silver.sampling import select_synoptic
from dcdr.state import atomic_write_parquet

log = logging.getLogger(__name__)
PROVENANCE = {"source_key", "source_md5", "ingest_run_id"}
_SORT = ["time_utc", "report_type", "source", "row_key"]
CORE_COLUMNS = [
    "station_id", "time_utc", "time_lmst", "lmst_date", "lmst_hour", "report_type", "source",
    "lat", "lon", "elev", "slot_time", "slot_delta_min", "is_candidate", "is_excluded_type",
    "selection_rank", "selection_reason", "is_synoptic_selected", "has_pw", "dust_rank",
    "dust_class_rule", "dust_rule_id", "dust_class_final", "dust_haze", "rh_screened",
    "flag_du", "flag_duhz", "flag_susp", "flag_bd", "flag_ds", "flag_vicinity", "flag_past",
    "flag_ambiguous", "pw_evidence", "wsd", "use_wsd", "wind_dir", "use_wind_dir", "gust",
    "use_gust", "gust_src", "vis_raw", "vis_capped", "use_vis", "t", "use_t", "td", "use_td",
    "rh", "slp", "use_slp", "parse_status", "dup_count", "row_key", "source_key",
]


def _row_keys(raw: pl.DataFrame) -> pl.Series:
    cols = sorted(c for c in raw.columns if c not in PROVENANCE)
    joined = raw.select(
        pl.concat_str([pl.col(c).fill_null("\x00") for c in cols], separator="\x1f")
    ).to_series()
    return pl.Series("row_key", [hashlib.sha1(s.encode()).hexdigest() for s in joined], dtype=pl.String)


def previous_year_tail(prev: pl.DataFrame, year: int) -> pl.DataFrame:
    return prev.filter(pl.col("DATE") >= f"{year - 1}-12-31T00:00:00")


def dedupe(raw: pl.DataFrame) -> pl.DataFrame:
    raw = raw.with_columns(_row_keys(raw))
    counts = raw.group_by("row_key").agg(pl.len().cast(pl.Int32).alias("dup_count"))
    return raw.unique(subset="row_key", keep="first", maintain_order=True).join(counts, on="row_key")


def build_station_year(
    current: pl.DataFrame, previous: pl.DataFrame | None, year: int, settings: Settings
) -> pl.DataFrame:
    frames = [current] if previous is None else [previous_year_tail(previous, year), current]
    raw = dedupe(pl.concat(frames, how="diagonal_relaxed"))
    df = add_time_columns(parse_reports(raw))
    bad = df.filter(pl.col("time_utc").is_null()).height
    if bad:
        log.warning("dropping %d rows with unparseable DATE", bad)
        df = df.filter(pl.col("time_utc").is_not_null())
    df = apply_qc(df, settings.qc, settings.parse)
    df = df.sort(_SORT, nulls_last=True).with_columns(pl.col("time_utc").set_sorted())
    df = classify_reports(df, settings.classify, settings.qc)
    df = apply_haze_and_rh(df, settings.classify, settings.qc)
    df = select_synoptic(df, settings.sampling)
    df = df.filter(pl.col("time_utc").dt.year() == year).sort(_SORT, nulls_last=True)
    rest = sorted(c for c in df.columns if c not in CORE_COLUMNS)
    return df.select(CORE_COLUMNS + rest)


def write_silver(
    df: pl.DataFrame, path: Path, *, run_id: str, config_hash: str, source_md5: str, prev_source_md5: str
) -> None:
    validate_silver(df)
    atomic_write_parquet(
        df.to_arrow(),
        path,
        {"dcdr_layer": "silver", "method_version": METHOD_VERSION, "config_hash": config_hash,
         "run_id": run_id, "source_md5": source_md5, "prev_source_md5": prev_source_md5},
    )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/unit/test_silver_build.py -v`
Expected: PASS (7 passed). If `test_header_only_bronze_builds_empty_silver` fails inside Pandera because the frame is empty, change `validate_silver` to `if df.height == 0: return df` as its first line, and re-run.

- [ ] **Step 6: Run the whole suite and commit**

```bash
uv run pytest && uv run mypy src
```
```bash
git add src/dcdr/silver/build.py src/dcdr/schemas.py tests/unit/test_silver_build.py
git commit -m "feat: silver station-year build with look-back, dedupe and contract

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Gold daily product, versions and CURRENT pointer

**Files:**
- Create: `sql/daily.sql`, `src/dcdr/gold.py`, `tests/unit/test_gold_daily.py`

**Interfaces:**
- Consumes: `build_station_year`, `write_silver` (Task 11); `atomic_write_bytes`, `read_parquet_metadata`, `safe_listdir` (Task 3).
- Produces:
  - `dcdr.gold.SQL_DIR`; `Product(name, sql_file, checks)`; `PRODUCTS`; `MixedSilverVersionsError`; `GoldCheckError`.
  - `build_gold(settings, silver_paths: list[Path], run_id: str) -> Path` (returns the version directory).
  - `current_gold_dir(settings) -> Path | None`; `prune_versions(gold_root: Path, keep: int) -> None`.
  - Output: `<data_root>/gold/versions/<run_id>/daily/part-000.parquet`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_gold_daily.py`:
```python
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from dcdr.config import Settings, load_settings
from dcdr.gold import MixedSilverVersionsError, build_gold, current_gold_dir
from dcdr.silver.build import build_station_year, write_silver
from tests.fixtures.isd import bronze_frame, isd_row

LON0 = {"LONGITUDE": "0.0"}  # LMST == UTC keeps expectations readable


def _silver(settings: Settings, rows: list[dict], name: str = "S", cfg_hash: str = "h") -> Path:
    df = build_station_year(bronze_frame(rows), None, 2024, settings)
    path = settings.data_root / "silver" / "year=2024" / f"{name}.parquet"
    write_silver(df, path, run_id="r", config_hash=cfg_hash, source_md5="m", prev_source_md5="")
    return path


def _daily(settings: Settings, paths: list[Path], run_id: str = "20260101T000000Z-aaaaaa") -> pl.DataFrame:
    out = build_gold(settings, paths, run_id)
    return pl.read_parquet(out / "daily" / "part-000.parquet")


def _slots(day: str, hours: range = range(0, 24, 3), **fields: str) -> list[dict]:
    return [isd_row(f"{day}T{h:02d}:00:00", **LON0, **fields) for h in hours]


def test_daily_counts_dup6_and_visibility(settings: Settings) -> None:
    rows = [isd_row(f"2024-01-01T{h:02d}:00:00", **LON0, WND="090,1,N,0100,1",
                    MW1="06,1" if h in (3, 6) else "02,1",
                    VIS="000000,1,N,1" if h == 0 else "016000,1,N,1") for h in range(0, 24, 3)]
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    assert (d["lmst_date"], d["n_reports"], d["n_slots"], d["n_slots_pw"]) == (date(2024, 1, 1), 8, 8, 8)
    assert d["valid_day"] and d["dusty_day_synoptic"] and d["dusty_day_any"]
    assert d["n_dust_slots_suspended"] == 2
    assert d["wsd_mean"] == pytest.approx(10.0)
    assert d["dup6_mean"] == pytest.approx(1024.0)  # 10^3 * (1 + 0.6) * (1 - 0.36)
    assert (d["vis_min"], d["n_vis_zero"]) == (0.0, 1)
    assert d["vis_hmean"] == pytest.approx(10000.0)


def test_d_t_mean_needs_consecutive_valid_days(settings: Settings) -> None:
    rows = (_slots("2024-01-01", TMP="+0300,1", MW1="02,1")
            + _slots("2024-01-02", range(0, 6, 3), TMP="+0310,1", MW1="02,1")
            + _slots("2024-01-03", TMP="+0320,1", MW1="02,1")
            + _slots("2024-01-04", TMP="+0330,1", MW1="02,1"))
    d = _daily(settings, [_silver(settings, rows)]).sort("lmst_date")
    assert d["valid_day"].to_list() == [True, False, True, True]
    assert d["d_t_mean"].to_list()[:3] == [None, None, None]
    assert d["d_t_mean"].to_list()[3] == pytest.approx(1.0)


def test_day_with_only_excluded_types_has_no_row(settings: Settings) -> None:
    rows = _slots("2024-01-01", MW1="02,1") + [isd_row("2024-01-02T23:59:00", **LON0, REPORT_TYPE="SOD")]
    d = _daily(settings, [_silver(settings, rows)])
    assert d["lmst_date"].to_list() == [date(2024, 1, 1)]


def test_mixed_silver_versions_are_refused(settings: Settings) -> None:
    a = _silver(settings, _slots("2024-01-01"), name="A", cfg_hash="h1")
    b = _silver(settings, _slots("2024-01-01"), name="B", cfg_hash="h2")
    with pytest.raises(MixedSilverVersionsError):
        build_gold(settings, [a, b], "20260101T000000Z-aaaaaa")


def test_current_pointer_and_pruning(tmp_path: Path) -> None:
    s = load_settings(overrides={"paths": {"data_root": str(tmp_path / "data")},
                                 "products": {"gold_keep_versions": 2}})
    path = _silver(s, _slots("2024-01-01"))
    run_ids = [f"20260101T00000{i}Z-aaaaaa" for i in range(4)]
    for rid in run_ids:
        build_gold(s, [path], rid)
    assert current_gold_dir(s) == s.data_root / "gold" / "versions" / run_ids[-1]
    remaining = sorted(p.name for p in (s.data_root / "gold" / "versions").iterdir())
    assert remaining == run_ids[-2:]
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_gold_daily.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dcdr.gold'`.

- [ ] **Step 3: Write `sql/daily.sql`** (no trailing semicolon: it is wrapped in `COPY (...)`)

```sql
-- Daily station product on the local mean solar time (LMST) day.
-- Definitions: docs/superpowers/specs/2026-10-15-dcdr-pilot-design.md section 7.3.2.
-- Inputs: view `silver`; variable `min_slots` (valid-day threshold).
WITH sel AS (
    SELECT
        *,
        list_contains(
            ['suspended', 'blowing', 'whirls', 'storm', 'storm_severe', 'storm_thunder', 'dust_haze'],
            dust_class_final
        ) AS is_dusty,
        CASE
            WHEN use_wsd AND wsd > 6 THEN pow(wsd, 3) * (1 + 6 / wsd) * (1 - 36 / pow(wsd, 2))
            WHEN use_wsd THEN 0.0
        END AS dup6
    FROM silver
    WHERE is_synoptic_selected
),
sel_daily AS (
    SELECT
        station_id,
        lmst_date,
        count(*) AS n_slots,
        count(*) FILTER (WHERE has_pw) AS n_slots_pw,
        bool_or(is_dusty) AS dusty_day_synoptic,
        count(*) FILTER (WHERE dust_class_final = 'suspended') AS n_dust_slots_suspended,
        count(*) FILTER (WHERE dust_class_final = 'blowing') AS n_dust_slots_blowing,
        count(*) FILTER (WHERE dust_class_final = 'whirls') AS n_dust_slots_whirls,
        count(*) FILTER (WHERE dust_class_final IN ('storm', 'storm_severe', 'storm_thunder')) AS n_dust_slots_storm,
        count(*) FILTER (WHERE dust_class_final = 'dust_haze') AS n_dust_slots_dust_haze,
        count(*) FILTER (WHERE dust_class_final = 'screened') AS n_slots_rh_screened,
        min(lmst_hour) FILTER (WHERE is_dusty) AS dust_first_lmst_hour,
        avg(wsd) FILTER (WHERE use_wsd) AS wsd_mean,
        max(wsd) FILTER (WHERE use_wsd) AS wsd_max,
        min(vis_capped) FILTER (WHERE use_vis) AS vis_min,
        count(*) FILTER (WHERE use_vis AND vis_capped > 0)
            / nullif(sum(1.0 / vis_capped) FILTER (WHERE use_vis AND vis_capped > 0), 0) AS vis_hmean,
        count(*) FILTER (WHERE use_vis AND vis_capped = 0) AS n_vis_zero,
        avg(t) FILTER (WHERE use_t) AS t_mean,
        min(t) FILTER (WHERE use_t) AS t_min,
        max(t) FILTER (WHERE use_t) AS t_max,
        avg(td) FILTER (WHERE use_td) AS td_mean,
        avg(rh) AS rh_mean,
        avg(slp) FILTER (WHERE use_slp) AS slp_mean,
        avg(dup6) AS dup6_mean
    FROM sel
    GROUP BY station_id, lmst_date
),
all_daily AS (
    SELECT
        station_id,
        lmst_date,
        count(*) AS n_reports,
        bool_or(list_contains(
            ['suspended', 'blowing', 'whirls', 'storm', 'storm_severe', 'storm_thunder', 'dust_haze'],
            dust_class_final
        )) AS dusty_day_any,
        max(gust) FILTER (WHERE use_gust) AS gust_max
    FROM silver
    WHERE NOT is_excluded_type
    GROUP BY station_id, lmst_date
),
joined AS (
    SELECT
        a.station_id,
        a.lmst_date,
        a.n_reports,
        coalesce(s.n_slots, 0) AS n_slots,
        coalesce(s.n_slots_pw, 0) AS n_slots_pw,
        coalesce(s.n_slots_pw, 0) >= getvariable('min_slots') AS valid_day,
        coalesce(s.dusty_day_synoptic, false) AS dusty_day_synoptic,
        coalesce(a.dusty_day_any, false) AS dusty_day_any,
        coalesce(s.n_dust_slots_suspended, 0) AS n_dust_slots_suspended,
        coalesce(s.n_dust_slots_blowing, 0) AS n_dust_slots_blowing,
        coalesce(s.n_dust_slots_whirls, 0) AS n_dust_slots_whirls,
        coalesce(s.n_dust_slots_storm, 0) AS n_dust_slots_storm,
        coalesce(s.n_dust_slots_dust_haze, 0) AS n_dust_slots_dust_haze,
        coalesce(s.n_slots_rh_screened, 0) AS n_slots_rh_screened,
        s.dust_first_lmst_hour,
        s.wsd_mean,
        s.wsd_max,
        a.gust_max,
        s.vis_min,
        s.vis_hmean,
        coalesce(s.n_vis_zero, 0) AS n_vis_zero,
        s.t_mean,
        s.t_min,
        s.t_max,
        s.td_mean,
        s.rh_mean,
        s.slp_mean,
        s.dup6_mean
    FROM all_daily AS a
    LEFT JOIN sel_daily AS s ON a.station_id = s.station_id AND a.lmst_date = s.lmst_date
)
SELECT
    *,
    CASE
        WHEN valid_day AND lag(valid_day) OVER w AND date_diff('day', lag(lmst_date) OVER w, lmst_date) = 1
        THEN t_mean - lag(t_mean) OVER w
    END AS d_t_mean,
    CASE
        WHEN valid_day AND lag(valid_day) OVER w AND date_diff('day', lag(lmst_date) OVER w, lmst_date) = 1
        THEN slp_mean - lag(slp_mean) OVER w
    END AS d_slp_mean
FROM joined
WINDOW w AS (PARTITION BY station_id ORDER BY lmst_date)
ORDER BY station_id, lmst_date
```

- [ ] **Step 4: Implement `src/dcdr/gold.py`**

```python
"""Gold layer: product registry, versioned full rebuilds and the CURRENT pointer (spec §6.1, §7.3)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import duckdb

from dcdr.config import REPO_ROOT, Settings
from dcdr.state import atomic_write_bytes, read_parquet_metadata, safe_listdir

SQL_DIR = REPO_ROOT / "sql"


@dataclass(frozen=True)
class Product:
    name: str
    sql_file: str
    checks: dict[str, str]  # name -> SQL returning a count that must be 0


PRODUCTS = [
    Product(
        "daily",
        "daily.sql",
        {
            "n_slots_le_8": "SELECT count(*) FROM daily WHERE n_slots > 8",
            "pw_le_slots": "SELECT count(*) FROM daily WHERE n_slots_pw > n_slots",
            "valid_day_consistent": (
                "SELECT count(*) FROM daily WHERE valid_day <> (n_slots_pw >= getvariable('min_slots'))"
            ),
            "dusty_needs_slots": "SELECT count(*) FROM daily WHERE dusty_day_synoptic AND n_slots = 0",
            "unique_station_day": (
                "SELECT count(*) FROM (SELECT station_id, lmst_date FROM daily "
                "GROUP BY ALL HAVING count(*) > 1)"
            ),
            "d_t_only_valid": "SELECT count(*) FROM daily WHERE d_t_mean IS NOT NULL AND NOT valid_day",
        },
    ),
]


class MixedSilverVersionsError(RuntimeError):
    """Silver inputs were built with different method versions or configs."""


class GoldCheckError(RuntimeError):
    """A gold product failed a validation check."""


def _silver_versions(paths: list[Path]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for p in paths:
        meta = read_parquet_metadata(p)
        out.add((meta.get("method_version", ""), meta.get("config_hash", "")))
    return out


def _sql_path(path: Path) -> str:
    text = str(path)
    if "'" in text:
        raise ValueError(f"unsupported character in path: {text}")
    return text


def build_gold(settings: Settings, silver_paths: list[Path], run_id: str) -> Path:
    if not silver_paths:
        raise ValueError("no silver files to build gold from")
    versions = _silver_versions(silver_paths)
    if len(versions) != 1:
        raise MixedSilverVersionsError(f"silver files have mixed versions: {sorted(versions)}")
    gold_root = settings.data_root / "gold"
    out_dir = gold_root / "versions" / run_id
    con = duckdb.connect()
    con.read_parquet([_sql_path(p) for p in silver_paths], union_by_name=True).create_view("silver")
    con.execute(f"SET VARIABLE min_slots = {int(settings.products.min_slots_per_day)}")
    try:
        for product in PRODUCTS:
            target = out_dir / product.name / "part-000.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            sql = (SQL_DIR / product.sql_file).read_text()
            con.execute(f"COPY ({sql}) TO '{_sql_path(target)}' (FORMAT parquet, COMPRESSION zstd)")
            con.read_parquet(_sql_path(target)).create_view(product.name)
            failures = {}
            for name, query in product.checks.items():
                row = con.execute(query).fetchone()
                count = row[0] if row else 0
                if count:
                    failures[name] = count
            if failures:
                raise GoldCheckError(f"{product.name}: {failures}")
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    finally:
        con.close()
    atomic_write_bytes(gold_root / "CURRENT", run_id.encode())
    prune_versions(gold_root, settings.products.gold_keep_versions)
    return out_dir


def current_gold_dir(settings: Settings) -> Path | None:
    pointer = settings.data_root / "gold" / "CURRENT"
    if not pointer.exists():
        return None
    return settings.data_root / "gold" / "versions" / pointer.read_text().strip()


def prune_versions(gold_root: Path, keep: int) -> None:
    active = (gold_root / "CURRENT").read_text().strip()
    versions = [p for p in safe_listdir(gold_root / "versions") if p.is_dir()]
    stale = [p for p in versions if p.name != active][: max(0, len(versions) - keep)]
    for p in stale:
        shutil.rmtree(p)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/unit/test_gold_daily.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add sql/daily.sql src/dcdr/gold.py tests/unit/test_gold_daily.py
git commit -m "feat: versioned gold build with daily SQL product and checks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Pipeline, CLI commands and end-to-end test

**Files:**
- Create: `src/dcdr/pipeline.py`, `tests/integration/__init__.py`, `tests/integration/test_pipeline_e2e.py`, `tests/unit/test_pipeline.py`
- Modify: `src/dcdr/cli.py` (replace the whole file with the version below)

**Interfaces:**
- Consumes: Tasks 2–12.
- Produces:
  - `DataRootUnavailable`; `RunContext(settings, run_id, command, started_at, git_commit, git_dirty)` with `.root` and `.state_dir`.
  - `start_run(settings, command) -> RunContext`; `finish_run(ctx, status, summary: dict) -> None`.
  - `read_station_list(path) -> list[str]`; `parse_years(text) -> list[int]`.
  - `run_ingest(ctx, stations, years, store=None) -> IngestSummary`, which also ingests `min(years) - 1` for the haze look-back.
  - `SilverSummary`; `silver_path(root, year, station) -> Path`; `run_silver(ctx, stations, years, force=False) -> SilverSummary`.
  - `all_silver_paths(root, stations, years) -> list[Path]`; `run_gold(ctx, stations, years) -> Path`.
  - CLI commands `ingest`, `build silver`, `build gold`, `run`, `status`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_pipeline.py`:
```python
from pathlib import Path

import pytest

from dcdr.config import load_settings
from dcdr.pipeline import DataRootUnavailable, parse_years, read_station_list, start_run


def test_parse_years_forms() -> None:
    assert parse_years("2024") == [2024]
    assert parse_years("2022-2024") == [2022, 2023, 2024]
    assert parse_years("2020,2022") == [2020, 2022]


def test_station_list_reads_ids(tmp_path: Path) -> None:
    p = tmp_path / "s.csv"
    p.write_text("station_id,name\n72386023169,LAS VEGAS\n 40582099999 ,KUWAIT\n")
    assert read_station_list(p) == ["72386023169", "40582099999"]


def test_unmounted_data_root_fails_clearly() -> None:
    s = load_settings(overrides={"paths": {"data_root": "/nonexistent-volume-xyz/dcdr"}})
    with pytest.raises(DataRootUnavailable, match="mounted"):
        start_run(s, "test")
    assert not Path("/nonexistent-volume-xyz").exists()
```

`tests/integration/__init__.py`: empty file.

`tests/integration/test_pipeline_e2e.py`:
```python
from datetime import date

import obstore as obs
import polars as pl
from obstore.store import MemoryStore

from dcdr.config import Settings
from dcdr.pipeline import run_gold, run_ingest, run_silver, start_run
from tests.fixtures.isd import isd_row, make_isd_csv

STATION = "40582099999"
LON0 = {"LONGITUDE": "0.0"}


def _put(store: MemoryStore, year: int, rows: list[dict]) -> None:
    obs.put(store, f"{year}/{STATION}.csv", make_isd_csv(rows))


def _jan1(store: MemoryStore) -> None:
    rows = [isd_row(f"2024-01-01T{h:02d}:00:00", **LON0, MW1="05,1" if h == 0 else "02,1")
            for h in range(0, 24, 3)]
    _put(store, 2024, rows)


def test_end_to_end_incremental(settings: Settings) -> None:
    store = MemoryStore()
    _put(store, 2023, [isd_row("2023-12-31T22:00:00", **LON0, MW1="06,1")])
    _jan1(store)
    ctx = start_run(settings, "test")

    ing = run_ingest(ctx, [STATION], [2024], store=store)
    assert ing.downloaded == 2  # 2024 plus 2023 for the haze look-back
    sil = run_silver(ctx, [STATION], [2024])
    assert sil.built == 1
    daily = pl.read_parquet(run_gold(ctx, [STATION], [2024]) / "daily" / "part-000.parquet")
    jan1 = daily.filter(pl.col("lmst_date") == date(2024, 1, 1)).row(0, named=True)
    assert jan1["dusty_day_synoptic"] and jan1["n_dust_slots_dust_haze"] == 1

    # Unchanged inputs: nothing is re-downloaded or rebuilt.
    assert run_ingest(ctx, [STATION], [2024], store=store).downloaded == 0
    assert run_silver(ctx, [STATION], [2024]).built == 0

    # A correction to the PREVIOUS year must rebuild this year's silver (look-back dependency).
    _put(store, 2023, [isd_row("2023-12-31T22:00:00", **LON0, MW1="02,1"), isd_row("2023-12-31T23:00:00", **LON0)])
    assert run_ingest(ctx, [STATION], [2024], store=store).downloaded == 1
    assert run_silver(ctx, [STATION], [2024]).built == 1
    daily = pl.read_parquet(run_gold(ctx, [STATION], [2024]) / "daily" / "part-000.parquet")
    jan1 = daily.filter(pl.col("lmst_date") == date(2024, 1, 1)).row(0, named=True)
    assert not jan1["dusty_day_synoptic"]
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/unit/test_pipeline.py tests/integration -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dcdr.pipeline'`.

- [ ] **Step 3: Implement `src/dcdr/pipeline.py`**

```python
"""Stage runner: run context, incremental silver decisions and run records (spec §11-12)."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from dcdr.config import REPO_ROOT, Settings, canonical_yaml, config_hash, silver_config_hash
from dcdr.gold import build_gold
from dcdr.ingest import IngestSummary, bronze_path, ingest, make_store
from dcdr.silver import METHOD_VERSION
from dcdr.silver.build import build_station_year, write_silver
from dcdr.state import (
    Manifest, append_run_record, atomic_write_bytes, git_revision, new_run_id,
    read_parquet_metadata, utcnow,
)

log = logging.getLogger(__name__)


class DataRootUnavailable(RuntimeError):
    """The data root's parent directory does not exist (e.g. the SSD is not mounted)."""


@dataclass
class RunContext:
    settings: Settings
    run_id: str
    command: str
    started_at: datetime
    git_commit: str
    git_dirty: bool

    @property
    def root(self) -> Path:
        return self.settings.data_root

    @property
    def state_dir(self) -> Path:
        return self.root / "_state"


def start_run(settings: Settings, command: str) -> RunContext:
    root = settings.data_root
    if not root.parent.exists():
        raise DataRootUnavailable(f"{root.parent} does not exist — is the SSD mounted?")
    root.mkdir(exist_ok=True)
    commit, dirty = git_revision(REPO_ROOT)
    ctx = RunContext(settings, new_run_id(), command, utcnow(), commit, dirty)
    atomic_write_bytes(
        ctx.state_dir / "runs" / ctx.run_id / "config.resolved.yaml", canonical_yaml(settings).encode()
    )
    log.info("run %s started (%s) commit=%s dirty=%s", ctx.run_id, command, commit[:8], dirty)
    return ctx


def finish_run(ctx: RunContext, status: str, summary: dict[str, Any]) -> None:
    append_run_record(
        ctx.state_dir / "runs.parquet",
        {
            "run_id": ctx.run_id, "command": ctx.command, "started_at": ctx.started_at,
            "finished_at": utcnow(), "status": status, "git_commit": ctx.git_commit,
            "git_dirty": ctx.git_dirty, "config_hash": config_hash(ctx.settings),
            "summary": json.dumps(summary, default=str),
        },
    )


def read_station_list(path: Path) -> list[str]:
    with path.open(newline="") as fh:
        return [row["station_id"].strip() for row in csv.DictReader(fh)]


def parse_years(text: str) -> list[int]:
    years: list[int] = []
    for part in text.split(","):
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            years.extend(range(lo, hi + 1))
        else:
            years.append(int(part))
    return sorted(set(years))


def run_ingest(ctx: RunContext, stations: list[str], years: list[int], store: Any = None) -> IngestSummary:
    padded = sorted(set(years) | {min(years) - 1})  # previous year feeds the haze look-back
    manifest = Manifest(ctx.state_dir / "sources.parquet")
    summary = ingest(
        store if store is not None else make_store(ctx.settings.ingest),
        ctx.settings.ingest, ctx.root, manifest, set(stations), padded, ctx.run_id,
    )
    manifest.save()
    return summary


@dataclass
class SilverSummary:
    built: int = 0
    skipped: int = 0
    missing_bronze: int = 0
    paths: list[Path] = field(default_factory=list)


def silver_path(root: Path, year: int, station: str) -> Path:
    return root / "silver" / f"year={year}" / f"{station}.parquet"


def _bronze_md5(path: Path) -> str:
    return read_parquet_metadata(path).get("source_md5", "") if path.exists() else ""


def _silver_is_current(path: Path, cfg_hash: str, md5: str, prev_md5: str) -> bool:
    if not path.exists():
        return False
    meta = read_parquet_metadata(path)
    return (
        meta.get("method_version") == METHOD_VERSION
        and meta.get("config_hash") == cfg_hash
        and meta.get("source_md5") == md5
        and meta.get("prev_source_md5") == prev_md5
    )


def run_silver(ctx: RunContext, stations: list[str], years: list[int], force: bool = False) -> SilverSummary:
    summary = SilverSummary()
    cfg_hash = silver_config_hash(ctx.settings)
    for station in stations:
        for year in years:
            bpath = bronze_path(ctx.root, year, station)
            if not bpath.exists():
                summary.missing_bronze += 1
                continue
            prev_path = bronze_path(ctx.root, year - 1, station)
            md5, prev_md5 = _bronze_md5(bpath), _bronze_md5(prev_path)
            spath = silver_path(ctx.root, year, station)
            if not force and _silver_is_current(spath, cfg_hash, md5, prev_md5):
                summary.skipped += 1
                summary.paths.append(spath)
                continue
            previous = pl.read_parquet(prev_path) if prev_path.exists() else None
            df = build_station_year(pl.read_parquet(bpath), previous, year, ctx.settings)
            write_silver(df, spath, run_id=ctx.run_id, config_hash=cfg_hash,
                         source_md5=md5, prev_source_md5=prev_md5)
            summary.built += 1
            summary.paths.append(spath)
    return summary


def all_silver_paths(root: Path, stations: list[str], years: list[int]) -> list[Path]:
    paths = [silver_path(root, y, s) for s in stations for y in years]
    return [p for p in paths if p.exists()]


def run_gold(ctx: RunContext, stations: list[str], years: list[int]) -> Path:
    return build_gold(ctx.settings, all_silver_paths(ctx.root, stations, years), ctx.run_id)
```

- [ ] **Step 4: Replace `src/dcdr/cli.py`**

```python
"""Command-line interface. Parses arguments and calls pipeline functions; no logic here."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import polars as pl
import typer

from dcdr import __version__, pipeline
from dcdr.config import DEFAULT_CONFIG, REPO_ROOT, Settings, load_settings

app = typer.Typer(help="DCDR pipeline command-line interface.", no_args_is_help=True)
build_app = typer.Typer(help="Build one data layer.", no_args_is_help=True)
app.add_typer(build_app, name="build")

ConfigOpt = typer.Option(DEFAULT_CONFIG, "--config", help="Config YAML file.")
ProfileOpt = typer.Option(None, "--profile", help="Profile YAML merged over the config.")
StationsOpt = typer.Option(None, "--stations", help="Station list CSV (default from config).")
YearsOpt = typer.Option(..., "--years", help="Years, e.g. 2024, 2022-2024 or 2020,2022.")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"dcdr {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """DCDR pipeline."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _station_ids(settings: Settings, stations: Path | None) -> list[str]:
    return pipeline.read_station_list(stations or REPO_ROOT / settings.stations.station_list)


def _execute(command: str, config: Path, profile: Path | None,
             body: Callable[[pipeline.RunContext], dict[str, Any]]) -> None:
    ctx = pipeline.start_run(load_settings(config, profile), command)
    try:
        summary = body(ctx)
    except Exception as exc:
        pipeline.finish_run(ctx, "failed", {"error": repr(exc)})
        raise
    pipeline.finish_run(ctx, "ok", summary)
    typer.echo(f"run {ctx.run_id}: {summary}")


@app.command()
def ingest(years: str = YearsOpt, stations: Path | None = StationsOpt,
           config: Path = ConfigOpt, profile: Path | None = ProfileOpt) -> None:
    """Download new or changed station-years from NOAA S3 into bronze."""
    def body(ctx: pipeline.RunContext) -> dict[str, Any]:
        ids = _station_ids(ctx.settings, stations)
        s = pipeline.run_ingest(ctx, ids, pipeline.parse_years(years))
        return asdict(s)
    _execute("ingest", config, profile, body)


@build_app.command("silver")
def build_silver(years: str = YearsOpt, stations: Path | None = StationsOpt,
                 force: bool = typer.Option(False, "--force", help="Rebuild even if current."),
                 config: Path = ConfigOpt, profile: Path | None = ProfileOpt) -> None:
    """Build silver station-years from bronze (incremental unless --force)."""
    def body(ctx: pipeline.RunContext) -> dict[str, Any]:
        ids = _station_ids(ctx.settings, stations)
        s = pipeline.run_silver(ctx, ids, pipeline.parse_years(years), force=force)
        return {"built": s.built, "skipped": s.skipped, "missing_bronze": s.missing_bronze}
    _execute("build silver", config, profile, body)


@build_app.command("gold")
def build_gold_cmd(years: str = YearsOpt, stations: Path | None = StationsOpt,
                   config: Path = ConfigOpt, profile: Path | None = ProfileOpt) -> None:
    """Rebuild gold products from silver into a new version and activate it."""
    def body(ctx: pipeline.RunContext) -> dict[str, Any]:
        ids = _station_ids(ctx.settings, stations)
        return {"gold": str(pipeline.run_gold(ctx, ids, pipeline.parse_years(years)))}
    _execute("build gold", config, profile, body)


@app.command()
def run(years: str = YearsOpt, stations: Path | None = StationsOpt,
        force: bool = typer.Option(False, "--force", help="Rebuild silver even if current."),
        config: Path = ConfigOpt, profile: Path | None = ProfileOpt) -> None:
    """Ingest, then build silver and gold."""
    def body(ctx: pipeline.RunContext) -> dict[str, Any]:
        ids, yrs = _station_ids(ctx.settings, stations), pipeline.parse_years(years)
        ing = pipeline.run_ingest(ctx, ids, yrs)
        sil = pipeline.run_silver(ctx, ids, yrs, force=force)
        gold = pipeline.run_gold(ctx, ids, yrs)
        return {"ingest": asdict(ing), "silver_built": sil.built,
                "silver_skipped": sil.skipped, "gold": str(gold)}
    _execute("run", config, profile, body)


@app.command()
def status(config: Path = ConfigOpt, profile: Path | None = ProfileOpt) -> None:
    """Show source-manifest counts and the most recent runs."""
    root = load_settings(config, profile).data_root / "_state"
    sources, runs = root / "sources.parquet", root / "runs.parquet"
    if sources.exists():
        typer.echo(pl.read_parquet(sources).group_by("status").len().sort("status"))
    else:
        typer.echo("no source manifest yet")
    if runs.exists():
        cols = ["run_id", "command", "status", "git_commit"]
        typer.echo(pl.read_parquet(runs).tail(5).select(cols))
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -v`
Expected: PASS (all unit + integration tests).

- [ ] **Step 6: Check the CLI help and the status command on an empty data root**

```bash
uv run dcdr --help && uv run dcdr build --help
```
```bash
DCDR_DATA=/tmp/dcdr-empty uv run dcdr status
```
Expected: the help lists `ingest`, `build`, `run`, `status`; status prints `no source manifest yet`.

- [ ] **Step 7: Run the gates and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
```
```bash
git add src/dcdr/pipeline.py src/dcdr/cli.py tests/unit/test_pipeline.py tests/integration
git commit -m "feat: pipeline stages, run records and CLI commands with e2e test

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Thin slice on real data and diagnostic chart

**Files:**
- Create: `config/stations/thin_slice.csv`, `scripts/thin_slice_report.py`, `docs/figures/thin_slice_monthly_dusty_days.png` (generated), `tests/integration/test_network_smoke.py`

**Interfaces:**
- Consumes: the CLI (Task 13); `current_gold_dir` (Task 12); `safe_listdir` (Task 3).
- Produces: a working thin slice on `/Volumes/CrucialX8/dcdr` and a chart for the user to review.

- [ ] **Step 1: Add the thin-slice station list**

`config/stations/thin_slice.csv`:
```csv
station_id,name,region,role
72386023169,LAS VEGAS HARRY REID INTL,north_america,contrast
40582099999,KUWAIT INTL,middle_east,source_candidate
```

- [ ] **Step 2: Add a network smoke test**

`tests/integration/test_network_smoke.py`:
```python
import pytest

from dcdr.config import load_settings
from dcdr.ingest import fetch_bytes, list_year, make_store, verify_md5


@pytest.mark.network
def test_noaa_bucket_lists_and_downloads() -> None:
    store = make_store(load_settings().ingest)
    objs = {o.key: o for o in list_year(store, 2024)}
    obj = objs["2024/72386023169.csv"]
    data = fetch_bytes(store, obj.key, retries=2)
    assert verify_md5(data, obj.etag) == obj.etag
```

Run: `uv run pytest -m network -v`
Expected: PASS (1 passed; needs internet).

- [ ] **Step 3: Write the report script**

`scripts/thin_slice_report.py`:
```python
"""Thin-slice diagnostics: per-station summary, field-family evidence and a monthly chart."""

from __future__ import annotations

import sys

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from dcdr.config import REPO_ROOT, load_settings  # noqa: E402
from dcdr.gold import current_gold_dir  # noqa: E402
from dcdr.state import safe_listdir  # noqa: E402

INK, MUTED, GRID, BAR = "#1f2328", "#59636e", "#d1d9e0", "#2f6fb0"
OUT = REPO_ROOT / "docs" / "figures" / "thin_slice_monthly_dusty_days.png"


def main() -> int:
    settings = load_settings()
    gold = current_gold_dir(settings)
    if gold is None:
        print("No active gold version — run `uv run dcdr run --years ...` first.")
        return 1
    daily = str(gold / "daily" / "part-000.parquet")
    silver = [str(p) for ydir in safe_listdir(settings.data_root / "silver")
              for p in safe_listdir(ydir) if p.suffix == ".parquet"]
    con = duckdb.connect()

    print("== Per-station daily summary (valid days only)")
    print(con.sql(f"""
        SELECT station_id, count(*) FILTER (WHERE valid_day) AS valid_days,
               count(*) FILTER (WHERE valid_day AND dusty_day_synoptic) AS dusty_days_synoptic,
               count(*) FILTER (WHERE valid_day AND dusty_day_any) AS dusty_days_any,
               sum(n_dust_slots_suspended) AS suspended, sum(n_dust_slots_blowing) AS blowing,
               sum(n_dust_slots_whirls) AS whirls, sum(n_dust_slots_storm) AS storm,
               sum(n_dust_slots_dust_haze) AS dust_haze, sum(n_slots_rh_screened) AS rh_screened
        FROM '{daily}' GROUP BY station_id ORDER BY station_id"""))

    print("== Which field decided dust in selected slots (MW / AU / AW), and ambiguous counts")
    print(con.sql(f"""
        SELECT station_id, split_part(dust_rule_id, ':', 1) AS field, count(*) AS slots,
               sum(flag_ambiguous::INT) AS ambiguous, sum(flag_vicinity::INT) AS vicinity
        FROM read_parquet({silver}, union_by_name = true)
        WHERE is_synoptic_selected AND dust_rule_id IS NOT NULL
        GROUP BY ALL ORDER BY ALL"""))

    monthly = con.sql(f"""
        SELECT station_id, date_trunc('month', lmst_date) AS month,
               count(*) FILTER (WHERE valid_day AND dusty_day_synoptic) AS dusty_days
        FROM '{daily}' GROUP BY ALL ORDER BY ALL""").pl()
    stations = monthly["station_id"].unique().sort().to_list()
    fig, axes = plt.subplots(len(stations), 1, figsize=(10, 2.6 * len(stations)),
                             sharex=True, sharey=True, squeeze=False)
    for ax, sid in zip(axes[:, 0], stations, strict=True):
        sub = monthly.filter(monthly["station_id"] == sid)
        ax.bar(sub["month"].to_list(), sub["dusty_days"].to_list(), width=20, color=BAR)
        ax.set_title(f"Station {sid} — dusty days per month (synoptic, valid days)",
                     loc="left", fontsize=10, color=INK)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(colors=MUTED, labelsize=8)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT, dpi=150)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the thin slice on the SSD**

```bash
ls /Volumes/CrucialX8 >/dev/null && echo "SSD mounted"
```
```bash
uv run dcdr run --years 2023-2024
```
```bash
uv run dcdr status
```
Expected: `SSD mounted`. The run summary shows `ingest.downloaded` = 6 (2 stations × 2022–2024) and `silver_built` = 4. Status shows `ok` = 6 in the manifest and a run with status `ok`.

- [ ] **Step 5: Re-run to confirm incrementality**

```bash
uv run dcdr run --years 2023-2024
```
Expected: `ingest.downloaded` = 0 and `silver_built` = 0 (`silver_skipped` = 4); a new gold version is still built and activated.

- [ ] **Step 6: Produce the diagnostics and the chart**

```bash
uv run python scripts/thin_slice_report.py
```
Expected: two printed tables, and `wrote …/docs/figures/thin_slice_monthly_dusty_days.png`. Open the PNG and check that both station panels share the y-axis.

- [ ] **Step 7: Review with the user (stop here; do not continue without their read-out)**

Show the user the two tables and the chart, and ask them to judge:
1. **Kuwait:** dusty days should cluster in spring and summer; non-zero storm counts are plausible.
2. **Las Vegas:** few dusty days. Check the **field table**: dust decided by `AU`/`AW` as well as `MW`, which shows the METAR fields are being used.
3. **Ambiguous counts** (AW 27–29 / AY 3) feed verification task V1: are the snow/sand temperature thresholds sensible?
4. Any class count that looks implausible becomes a bug ticket for Plan 2, not a silent fix.

- [ ] **Step 8: Commit**

```bash
git add config/stations/thin_slice.csv scripts/thin_slice_report.py tests/integration/test_network_smoke.py docs/figures/thin_slice_monthly_dusty_days.png
git commit -m "feat: thin-slice run on real ISD data with diagnostic report and chart

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
