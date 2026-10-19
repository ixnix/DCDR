"""Smoke test against the real NOAA bucket (needs internet).

Marked `network`, so it is skipped by default (see pyproject.toml addopts). Run it with:
    uv run pytest -m network
"""

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
