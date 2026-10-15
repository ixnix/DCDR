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


def _daily(
    settings: Settings, paths: list[Path], run_id: str = "20260101T000000Z-aaaaaa"
) -> pl.DataFrame:
    out = build_gold(settings, paths, run_id)
    return pl.read_parquet(out / "daily" / "part-000.parquet")


def _slots(day: str, hours: range = range(0, 24, 3), **fields: str) -> list[dict]:
    return [isd_row(f"{day}T{h:02d}:00:00", **LON0, **fields) for h in hours]


def test_daily_counts_dup6_and_visibility(settings: Settings) -> None:
    rows = [
        isd_row(
            f"2024-01-01T{h:02d}:00:00",
            **LON0,
            WND="090,1,N,0100,1",
            MW1="06,1" if h in (3, 6) else "02,1",
            VIS="000000,1,N,1" if h == 0 else "016000,1,N,1",
        )
        for h in range(0, 24, 3)
    ]
    d = _daily(settings, [_silver(settings, rows)]).row(0, named=True)
    assert (d["lmst_date"], d["n_reports"], d["n_slots"], d["n_slots_pw"]) == (
        date(2024, 1, 1),
        8,
        8,
        8,
    )
    assert d["valid_day"] and d["dusty_day_synoptic"] and d["dusty_day_any"]
    assert d["n_dust_slots_suspended"] == 2
    assert d["wsd_mean"] == pytest.approx(10.0)
    assert d["dup6_mean"] == pytest.approx(1024.0)  # 10^3 * (1 + 0.6) * (1 - 0.36)
    assert (d["vis_min"], d["n_vis_zero"]) == (0.0, 1)
    assert d["vis_hmean"] == pytest.approx(10000.0)


def test_d_t_mean_needs_consecutive_valid_days(settings: Settings) -> None:
    rows = (
        _slots("2024-01-01", TMP="+0300,1", MW1="02,1")
        + _slots("2024-01-02", range(0, 6, 3), TMP="+0310,1", MW1="02,1")
        + _slots("2024-01-03", TMP="+0320,1", MW1="02,1")
        + _slots("2024-01-04", TMP="+0330,1", MW1="02,1")
    )
    d = _daily(settings, [_silver(settings, rows)]).sort("lmst_date")
    assert d["valid_day"].to_list() == [True, False, True, True]
    assert d["d_t_mean"].to_list()[:3] == [None, None, None]
    assert d["d_t_mean"].to_list()[3] == pytest.approx(1.0)


def test_day_with_only_excluded_types_has_no_row(settings: Settings) -> None:
    rows = _slots("2024-01-01", MW1="02,1") + [
        isd_row("2024-01-02T23:59:00", **LON0, REPORT_TYPE="SOD")
    ]
    d = _daily(settings, [_silver(settings, rows)])
    assert d["lmst_date"].to_list() == [date(2024, 1, 1)]


def test_mixed_silver_versions_are_refused(settings: Settings) -> None:
    a = _silver(settings, _slots("2024-01-01"), name="A", cfg_hash="h1")
    b = _silver(settings, _slots("2024-01-01"), name="B", cfg_hash="h2")
    with pytest.raises(MixedSilverVersionsError):
        build_gold(settings, [a, b], "20260101T000000Z-aaaaaa")


def test_current_pointer_and_pruning(tmp_path: Path) -> None:
    s = load_settings(
        overrides={
            "paths": {"data_root": str(tmp_path / "data")},
            "products": {"gold_keep_versions": 2},
        }
    )
    path = _silver(s, _slots("2024-01-01"))
    run_ids = [f"20260101T00000{i}Z-aaaaaa" for i in range(4)]
    for rid in run_ids:
        build_gold(s, [path], rid)
    assert current_gold_dir(s) == s.data_root / "gold" / "versions" / run_ids[-1]
    remaining = sorted(p.name for p in (s.data_root / "gold" / "versions").iterdir())
    assert remaining == run_ids[-2:]
