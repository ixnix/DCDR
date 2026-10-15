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
