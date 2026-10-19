"""Settings: load the YAML configuration, validate it, and fingerprint it with stable hashes.

Where this fits
---------------
Every command (`dcdr ingest`, `dcdr process`, ...) starts by calling `load_settings()`. The result,
a `Settings` object, is passed to every stage, so all method choices (QC codes, dust classes, the
synoptic window, ...) come from ONE place instead of being scattered through the code as constants.

How the final settings are built (later layers override earlier ones)
--------------------------------------------------------------------
1. `src/dcdr/default.yaml`  -- built-in defaults, shipped inside the package
2. `--profile my.yaml`      -- optional file that overrides only the keys it names
3. `overrides={...}`        -- used by tests to tweak single values
4. `DCDR_DATA` env variable -- where the data lives (e.g. the portable SSD)

Design choices
--------------
* Pydantic models validate the YAML: a typo such as `vis_cap: 1000` instead of `vis_cap_m`, or a
  string where a number is expected, fails immediately with a clear message instead of silently
  falling back to a default deep inside the pipeline (`extra="forbid"` rejects unknown keys).
* `config_hash()` is a short SHA-256 fingerprint of the settings. It is written into run records
  and into every silver file, so we can always tell which settings produced a file, and rebuild
  silver only when settings that affect silver actually changed (`silver_config_hash`).
"""

from __future__ import annotations

import copy
import hashlib
import os
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

# The defaults file lives inside the package (src/dcdr/default.yaml). `files("dcdr")` finds the
# installed package folder, so this works both in the repo and from an installed wheel.
DEFAULT_CONFIG = files("dcdr") / "default.yaml"

# Config sections that change silver output. Only these feed the silver hash, so editing e.g. the
# download concurrency (`ingest`) or the gold settings (`products`) never triggers a silver rebuild.
SILVER_SECTIONS = ("parse", "qc", "classify", "sampling")


class _Section(BaseModel):
    """Base for every config section: reject keys the code does not know (catches typos)."""

    model_config = ConfigDict(extra="forbid")


# --- One class per top-level YAML section. Field names and types mirror default.yaml. ----------


class PathsCfg(_Section):
    data_root: str  # where bronze/silver/gold live; relative paths are from the working directory


class IngestCfg(_Section):
    bucket: str  # NOAA's public S3 bucket with one CSV per station-year
    region: str  # AWS region of that bucket
    max_concurrency: int  # parallel downloads
    retries: int  # retries per file before marking it failed


class ParseCfg(_Section):
    gust_od_types: list[int]  # which OD (supplementary wind) types count as gusts


class QcCfg(_Section):
    codes_ok: list[str]  # ISD quality codes treated as good
    codes_suspect: list[str]  # ... as suspect
    codes_erroneous: list[str]  # ... as erroneous
    accept: list[Literal["ok", "suspect"]]  # which QC flags a value may have and still be used
    vis_cap_m: float  # visibility above this is capped (reporting practices differ)
    rh_screen_pct: float  # relative humidity above which haze-like dust classes are screened out
    rh_screen_classes: list[str]  # the dust classes the RH screen applies to


class ClassifyCfg(_Section):
    fields: list[Literal["MW", "AU", "AW"]]  # present-weather field families used for dust
    haze_window_h: int  # look-back window (hours) for the haze-after-dust rule
    sand_min_t_c: float  # ambiguous "dust or snow" codes count as sand at or above this temp
    snow_max_t_c: float  # ... and as snow at or below this temp


class SamplingCfg(_Section):
    window_min: int  # +- minutes around each 3-hourly synoptic hour
    exclude_types: list[str]  # report types never used for slots (daily/monthly summaries)
    priority: list[str]  # report-type preference when several reports compete for one slot
    synoptic_dust_source: Literal["selected"]


class ProductsCfg(_Section):
    min_slots_per_day: int  # a day is "valid" with at least this many slots carrying weather
    gold_keep_versions: int  # how many old gold versions to keep on disk


class StationsCfg(_Section):
    station_list: str  # default station list CSV (relative to the working directory)


class Settings(_Section):
    """All settings for one run. Built by `load_settings()`; read-only afterwards."""

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
        """The data folder as an absolute path (see `_from_working_dir`)."""
        return _from_working_dir(self.paths.data_root)

    @property
    def station_list_path(self) -> Path:
        """The default station-list CSV as an absolute path."""
        return _from_working_dir(self.stations.station_list)


def _from_working_dir(text: str) -> Path:
    """User-supplied paths: absolute as given, relative to where dcdr is run (like git or dbt)."""
    path = Path(text).expanduser()  # also expands "~/..." to the home folder
    return path if path.is_absolute() else Path.cwd() / path


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge two nested dicts; values in `override` win, nested dicts are merged key by key.

    Example: base {"qc": {"a": 1, "b": 2}} + override {"qc": {"b": 3}} -> {"qc": {"a": 1, "b": 3}}.
    A plain `dict.update` would replace the whole "qc" section and lose "a".
    Copies are made so the caller's dicts are never modified.
    """
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)  # both sides are sections: merge recursively
        else:
            out[key] = copy.deepcopy(value)  # a plain value (or a new key): the override wins
    return out


def load_settings(
    config_path: Path | Traversable = DEFAULT_CONFIG,
    profile: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Read the defaults, apply profile, overrides and DCDR_DATA (in that order), then validate.

    `config_path` is a `Traversable` by default (a file inside the installed package) or a `Path`
    when the user passes `--config`; both support `.read_text()`, which is all we need.
    Raises `pydantic.ValidationError` if the merged settings are incomplete or contain unknown keys.
    """
    data: dict[str, Any] = yaml.safe_load(config_path.read_text())
    if profile is not None:
        data = deep_merge(data, yaml.safe_load(profile.read_text()) or {})  # empty file -> {}
    if overrides:
        data = deep_merge(data, overrides)
    env_root = os.environ.get("DCDR_DATA")  # e.g. export DCDR_DATA=/Volumes/CrucialX8/dcdr
    if env_root:
        data = deep_merge(data, {"paths": {"data_root": env_root}})
    return Settings.model_validate(data)


def canonical_yaml(settings: Settings, sections: tuple[str, ...] | None = None) -> str:
    """Settings as YAML text with sorted keys, so the same settings always give the same text.

    Used for hashing and for the `config.resolved.yaml` saved with every run. Pass `sections` to
    include only some top-level sections (e.g. only those that affect silver).
    """
    dumped = settings.model_dump(mode="json")
    if sections is not None:
        dumped = {k: dumped[k] for k in sections}
    return yaml.safe_dump(dumped, sort_keys=True)


def config_hash(settings: Settings, sections: tuple[str, ...] | None = None) -> str:
    """Short fingerprint (first 16 hex characters of SHA-256) of the canonical settings text."""
    return hashlib.sha256(canonical_yaml(settings, sections).encode()).hexdigest()[:16]


def silver_config_hash(settings: Settings) -> str:
    """Fingerprint of only the settings that change silver output (see SILVER_SECTIONS).

    Stored in every silver file; if it differs from the current settings, that file is rebuilt,
    and gold refuses to build from it (see aggregate.StaleSilverError).
    """
    return config_hash(settings, SILVER_SECTIONS)
