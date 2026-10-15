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
