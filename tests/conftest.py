from pathlib import Path

import pytest

from dcdr.config import Settings, load_settings


@pytest.fixture(autouse=True)
def _no_env_data_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DCDR_DATA", raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(overrides={"paths": {"data_root": str(tmp_path / "data")}})
