"""Thin-slice diagnostics: per-station summary, field-family evidence and a monthly chart.

A one-off analysis script (not part of the pipeline): it reads the ACTIVE gold version and the
silver files from the data root, prints two summary tables, and saves a bar chart of dusty days
per month to docs/figures/. Run it after `dcdr run`:

    uv run python scripts/thin_slice_report.py

It uses DuckDB to query the Parquet files directly with SQL, without loading them into Python
first; `.pl()` converts a query result into a Polars DataFrame for plotting.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import matplotlib

# "Agg" draws to image files only (no window), so the script also runs without a display.
# It must be chosen before pyplot is imported, hence the import order (and the noqa markers).
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from dcdr.aggregate import current_gold_dir  # noqa: E402
from dcdr.config import load_settings  # noqa: E402
from dcdr.state import safe_listdir  # noqa: E402

INK, MUTED, GRID, BAR = "#1f2328", "#59636e", "#d1d9e0", "#2f6fb0"  # chart colours
REPO_ROOT = Path(__file__).resolve().parents[1]  # scripts/ only run from a checkout
OUT = REPO_ROOT / "docs" / "figures" / "thin_slice_monthly_dusty_days.png"


def main() -> int:
    """Print the summaries and write the chart; return a shell exit code (0 = success)."""
    settings = load_settings()
    gold = current_gold_dir(settings)
    if gold is None:
        print("No active gold version — run `uv run dcdr run --years ...` first.")
        return 1
    daily = str(gold / "daily" / "part-000.parquet")
    # Every silver file: silver/year=YYYY/<station>.parquet
    silver = [
        str(p)
        for ydir in safe_listdir(settings.data_root / "silver")
        for p in safe_listdir(ydir)
        if p.suffix == ".parquet"
    ]
    con = duckdb.connect()

    # 1. Per station: valid days, dusty days (two definitions) and dust slots by class.
    print("== Per-station daily summary (valid days only)")
    print(
        con.sql(f"""
        SELECT station_id, count(*) FILTER (WHERE valid_day) AS valid_days,
               count(*) FILTER (WHERE valid_day AND dusty_day_synoptic) AS dusty_days_synoptic,
               count(*) FILTER (WHERE valid_day AND dusty_day_any) AS dusty_days_any,
               sum(n_dust_slots_suspended) AS suspended, sum(n_dust_slots_blowing) AS blowing,
               sum(n_dust_slots_whirls) AS whirls, sum(n_dust_slots_storm) AS storm,
               sum(n_dust_slots_dust_haze) AS dust_haze, sum(n_slots_rh_screened) AS rh_screened
        FROM '{daily}' GROUP BY station_id ORDER BY station_id""")
    )

    # 2. Which weather-field family (the part of dust_rule_id before ":") decided each dusty slot.
    print("== Which field decided dust in selected slots (MW / AU / AW), and ambiguous counts")
    print(
        con.sql(f"""
        SELECT station_id, split_part(dust_rule_id, ':', 1) AS field, count(*) AS slots,
               sum(flag_ambiguous::INT) AS ambiguous, sum(flag_vicinity::INT) AS vicinity
        FROM read_parquet({silver}, union_by_name = true)
        WHERE is_synoptic_selected AND dust_rule_id IS NOT NULL
        GROUP BY ALL ORDER BY ALL""")
    )

    # 3. Chart: dusty days per month, one panel per station (shared axes for comparison).
    monthly = con.sql(f"""
        SELECT station_id, date_trunc('month', lmst_date) AS month,
               count(*) FILTER (WHERE valid_day AND dusty_day_synoptic) AS dusty_days
        FROM '{daily}' GROUP BY ALL ORDER BY ALL""").pl()
    stations = monthly["station_id"].unique().sort().to_list()
    fig, axes = plt.subplots(
        len(stations), 1, figsize=(10, 2.6 * len(stations)), sharex=True, sharey=True, squeeze=False
    )
    for ax, sid in zip(axes[:, 0], stations, strict=True):
        sub = monthly.filter(monthly["station_id"] == sid)
        ax.bar(sub["month"].to_list(), sub["dusty_days"].to_list(), width=20, color=BAR)
        ax.set_title(
            f"Station {sid} — dusty days per month (synoptic, valid days)",
            loc="left",
            fontsize=10,
            color=INK,
        )
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
    sys.exit(main())  # pass main()'s return value to the shell as the exit code
