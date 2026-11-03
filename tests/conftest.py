"""Shared pytest fixtures, available to every test without importing them.

A fixture is a function pytest runs to prepare something a test needs; a test asks for it by
naming it as a parameter (e.g. `def test_x(settings): ...`). `tmp_path` is pytest's own fixture:
a fresh temporary folder per test, deleted automatically later.
"""

from pathlib import Path

import pytest

from dcdr.config import Settings, load_settings
from tests.fixtures.isd import write_isd_history


@pytest.fixture(autouse=True)
def _no_env_data_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Applied to every test (autouse): ignore the user's DCDR_DATA, so tests never touch real data."""
    monkeypatch.delenv("DCDR_DATA", raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Default settings with the data root inside this test's temporary folder, and an empty
    isd-history there: every test station is then "unverified" and kept in gold."""
    s = load_settings(overrides={"paths": {"data_root": str(tmp_path / "data")}})
    write_isd_history(s)
    return s
