"""Write a station list of the WHOLE NOAA ISD archive, for downloading every station.

The list comes from NOAA's S3 bucket (noaa-global-hourly-pds), the same place `dcdr ingest`
downloads from: every file there is one station-year, named after its station
("2024/40582099999.csv"; 11 digits = 6-digit USAF + 5-digit WBAN). The script lists every year
folder and keeps the unique station IDs. Only `station_id` is written: that is all `dcdr ingest`
needs. Names, coordinates and countries are inside the data files (and in NOAA's isd-history.csv)
and belong in a station catalog built after the download.

Usage (from the repo root):
    uv run python scripts/list_isd_stations.py                      # 1901-2025
    uv run python scripts/list_isd_stations.py --first 1973 --last 2025 --out my_list.csv
Takes about 20 seconds (anonymous S3 listing, no AWS account needed).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dcdr.config import load_settings
from dcdr.ingest import list_year, make_store


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--first", type=int, default=1901, help="first year to list")
    ap.add_argument("--last", type=int, default=2025, help="last year to list")
    ap.add_argument("--out", type=Path, default=Path("config/stations/all_isd.csv"))
    args = ap.parse_args()

    store = make_store(load_settings().ingest)
    years = range(args.first, args.last + 1)
    with ThreadPoolExecutor(8) as ex:  # listing waits on the network: 8 years at a time
        listed = list(ex.map(lambda y: list_year(store, y), years))
    files = [o for objs in listed for o in objs]
    ids = sorted({o.station_id for o in files})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("station_id\n" + "\n".join(ids) + "\n")
    gb = sum(o.size for o in files) / 1e9
    print(f"{len(ids):,} stations, {len(files):,} station-year files, {gb:,.1f} GB -> {args.out}")


if __name__ == "__main__":
    main()
