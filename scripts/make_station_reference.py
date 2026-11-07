"""Write the station reference file: one row per station of gold's station table, with NOAA's
station facts, gold's trusted position and dust totals, the aridity index and the first-level
region (state, province, ...). The single file the website and the station table read
(user decision 2026-11-06).

Sources (all under the data root, except the aridity grid):
- gold `stations` (active version, or --gold): trusted position, position status, years, dust
  totals, in_gold
- catalog/isd-history.csv: name, ICAO, country code, US state, elevation
- catalog/noaa_country_list/country-list.txt: NOAA's names for the FIPS country codes
- legacy/station_history/ai_v3_yr.tif: Global Aridity Index v3 (30", value x 10000; 0 = sea)
- catalog/naturalearth/ne_10m_admin_1_states_provinces.shp: Natural Earth admin-1, v5.1.1

Rules (user decisions 2026-11-06): aridity and region are taken at the trusted position; a
station on a sea cell (aridity) or just outside every region (coast) takes the nearest land cell
or region within 2 km, else none (`aridity_km`, `region_km` give the distance, 0 = at the
station). Elevation -0999.0 / -0999.9 / empty in isd-history means unknown.

Country: isd-history's CTRY is a FIPS code for most stations but an ISO code for some ("CH" is
China in FIPS, Switzerland in ISO). `country_code` is the FIPS code after the corrections in
CODE_FIXES (an ISO code that is another country in FIPS, for the stations lying in the ISO
country, e.g. "CH" in Switzerland -> "SZ", "AU" in Australia -> "AS") and STATION_CODE_FIXES;
`country_name` is NOAA's name for it. The raw readings are kept too:
`isd_country_name_fips` (NOAA's name for the code read as FIPS; may be wrong for ISO-coded
stations) and `map_country`, `map_country_iso` (the country of the region polygon). Which one
the published table uses is an open decision (docs/worklog.md).

Needs geopandas and rasterio, which are not project dependencies; run (from the repo root):
    uv run --with geopandas --with rasterio python scripts/make_station_reference.py
    (writes <data root>/catalog/station_reference.csv; --gold DIR, --out FILE to change)
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path

import duckdb
import geopandas as gpd  # type: ignore[import-untyped]
import numpy as np
import pandas as pd
import rasterio  # type: ignore[import-untyped]
from rasterio.windows import Window  # type: ignore[import-untyped]
from shapely.geometry import Point
from shapely.ops import nearest_points

from dcdr.aggregate import current_gold_dir
from dcdr.config import load_settings

MAX_KM = 2.0  # nearest land cell or region at most this far (the station is on land)
STATE_KM = 5.0  # a US station this close to its isd-history state keeps that state
# isd-history's CTRY is FIPS for most stations but ISO for some; where an ISO code clashes with
# a FIPS code of another country, the stations that lie in the ISO country get its FIPS code
# (user decisions; checked by position and WMO block): (CTRY, map country) -> FIPS code, name.
# Target codes from NOAA's country-list.txt (FIPS). User decisions 2026-11-06 ("CH" in Switzerland
# first, then all the clashes found by the same check). Not fixed: "TL" in East Timor (1 station;
# NOAA's list has no East Timor, so its FIPS code cannot be checked here).
CODE_FIXES = {
    ("CH", "Switzerland"): "SZ",  # FIPS CH = China; 167 stations, all WMO block 06
    ("AU", "Australia"): "AS",  # FIPS AU = Austria
    ("AQ", "Antarctica"): "AY",  # FIPS AQ = American Samoa
    ("CN", "China"): "CH",  # FIPS CN = Comoros
    ("GB", "United Kingdom"): "UK",  # FIPS GB = Gabon
    ("ZA", "South Africa"): "SF",  # FIPS ZA = Zambia
    ("SE", "Sweden"): "SW",  # FIPS SE = Seychelles
    ("BO", "Bolivia"): "BL",  # FIPS BO = Belarus
    ("CL", "Chile"): "CI",  # FIPS CL = Caroline Islands
    ("NG", "Nigeria"): "NI",  # FIPS NG = Niger
    ("MA", "Morocco"): "MO",  # FIPS MA = Madagascar
    ("GA", "Gabon"): "GB",  # FIPS GA = Gambia
    ("TN", "Tunisia"): "TS",  # FIPS TN = Tonga
    ("KR", "South Korea"): "KS",  # FIPS KR = Kiribati
    ("BD", "Bangladesh"): "BG",  # FIPS BD = Bermuda
    ("SN", "Senegal"): "SG",  # FIPS SN = Singapore
    ("PF", "French Polynesia"): "FP",  # FIPS PF = Paracel Islands
    ("DO", "Dominican Republic"): "DR",  # FIPS DO = Dominica
    ("CG", "Republic of the Congo"): "CF",  # FIPS CG = Zaire
    ("BY", "Belarus"): "BO",  # FIPS BY = Burundi
    ("ES", "Spain"): "SP",  # FIPS ES = El Salvador
    ("RS", "Republic of Serbia"): "RI",  # FIPS RS = Russia
    ("AI", "Anguilla"): "AV",  # FIPS AI = Ascension Island
    ("BG", "Bulgaria"): "BU",  # FIPS BG = Bangladesh
    ("NI", "Nicaragua"): "NU",  # FIPS NI = Nigeria
    ("AT", "Austria"): "AU",  # FIPS AT = Ashmore and Cartier Islands
    ("BA", "Bosnia and Herzegovina"): "BK",  # FIPS BA = Bahrain
    ("SK", "Slovakia"): "LO",  # FIPS SK = Sarawak and Saba
    ("CR", "Costa Rica"): "CS",  # FIPS CR = Coral Sea Islands
    ("PM", "Saint Pierre and Miquelon"): "SB",  # FIPS PM = Panama
    ("ZM", "Zambia"): "ZA",  # FIPS ZM = Samoa
    ("NE", "Niger"): "NG",  # FIPS NE = Niue
    ("SV", "El Salvador"): "ES",  # FIPS SV = Svalbard
    ("BS", "The Bahamas"): "BF",  # FIPS BS = Bassas da India
    ("PA", "Panama"): "PM",  # FIPS PA = Paraguay
    ("MN", "Mongolia"): "MG",  # FIPS MN = Monaco
    ("UA", "Ukraine"): "UP",  # FIPS UA = former USSR (Asia)
    # ISO codes that are no FIPS code at all (no name in NOAA's list; user decision 2026-11-06,
    # country names from the code)
    ("AE", "United Arab Emirates"): "TC",
    ("BJ", "Benin"): "BN",
    ("DE", "Germany"): "GM",
    ("DK", "Denmark"): "DA",
    ("DZ", "Algeria"): "AG",
    ("EE", "Estonia"): "EN",
    ("EH", "Morocco"): "WI",  # ISO EH = Western Sahara (the map counts it to Morocco)
    ("GF", "France"): "FG",  # French Guiana (the map counts it to France)
    ("GN", "Guinea"): "GV",
    ("GU", "Guam"): "GQ",
    ("HN", "Honduras"): "HO",
    ("HT", "Haiti"): "HA",
    ("IE", "Ireland"): "EI",
    ("JP", "Japan"): "JA",
    ("KY", "Cayman Islands"): "CJ",
    ("OM", "Oman"): "MU",
    ("PH", "Philippines"): "RP",
    ("PT", "Portugal"): "PO",
    ("PY", "Paraguay"): "PA",
    ("RU", "Russia"): "RS",
    ("TJ", "Tajikistan"): "TI",
    ("TR", "Turkey"): "TU",
    ("VN", "Vietnam"): "VM",
    ("XK", "Kosovo"): "KV",
    ("YE", "Yemen"): "YM",
    ("TL", "East Timor"): "TT",  # FIPS TL = Tokelau; ISD codes its other East Timor stations TT
}
# Display names: NOAA's names, title-cased, with modern short forms (user decision 2026-11-06:
# the name comes from the code). A code without a NOAA name (e.g. TT, UC, NN) takes the map's.
MODERN_NAMES = {
    "Bahamas The": "The Bahamas",
    "Gambia  The": "The Gambia",
    "Burma": "Myanmar",
    "Zaire": "DR Congo",
    "Congo": "Republic of the Congo",
    "Korea, North": "North Korea",
    "Korea, South": "South Korea",
    "Cote D'Ivoire": "Côte d'Ivoire",
    "Macedonia": "North Macedonia",
    "Swaziland": "Eswatini",
    "Western Samoa": "Samoa",
    "Falkland Islands (Islas Malvinas)": "Falkland Islands",
    "Micronesia, Federated States Of": "Micronesia",
    "Palau - Trust Territory Of The Pacific Islands": "Palau",
    "Reunion And Associated Islands": "Réunion",
    "Virgin Islands (British)": "British Virgin Islands",
    "Man  Isle Of": "Isle of Man",
    "Sao Tome And Principe": "São Tomé and Príncipe",
}


def display_name(noaa: str | None) -> str | None:
    """NOAA's upper-case country name as a display name: title case, small words lower case,
    "St." spelled out, modern short forms."""
    if not noaa or not isinstance(noaa, str):
        return None
    name = noaa.title()
    if name in MODERN_NAMES:
        return MODERN_NAMES[name]
    for word in (" And ", " The ", " Of "):
        name = name.replace(word, word.lower())
    return name.replace("St. ", "Saint ").replace("  ", " ")


# Single stations: Swedish WMO numbers (block 02) coded "SZ" (FIPS Switzerland); Karesuando lies
# on the Swedish-Finnish border river, where the map places it in Finland.
STATION_CODE_FIXES = {"02051099999": "SW", "02056099999": "SW", "02097099999": "SW"}
CELL_KM = 0.9266  # a 30" aridity cell north-south
AI_TIF = Path("legacy/station_history/ai_v3_yr.tif")
UNEP = [(0.03, "hyper-arid"), (0.2, "arid"), (0.5, "semi-arid"), (0.65, "dry sub-humid")]


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    r = math.pi / 180
    a = (
        math.sin((lat2 - lat1) * r / 2) ** 2
        + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lon2 - lon1) * r / 2) ** 2
    )
    return 12742 * math.asin(math.sqrt(a))


def aridity_class(ai: float | None) -> str | None:
    """UNEP class of an aridity index (P / PET); humid from 0.65."""
    if ai is None:
        return None
    return next((name for limit, name in UNEP if ai < limit), "humid")


def aridity(lats: list[float], lons: list[float]) -> tuple[list, list]:
    """Aridity index and its distance (km) per station: the cell at the position, or the nearest
    land cell within MAX_KM (the window is wide enough east-west near the poles)."""
    w = 30
    values: list[float | None] = []
    dists: list[float | None] = []
    with rasterio.open(AI_TIF) as r:
        raw = [v[0] for v in r.sample(list(zip(lons, lats, strict=True)))]
        for lat, lon, a in zip(lats, lons, raw, strict=True):
            dist: float | None = 0.0
            if a == 0:
                dist = None
                row, col = r.index(lon, lat)
                win = r.read(
                    1,
                    window=Window(col - w, row - w, 2 * w + 1, 2 * w + 1),
                    boundless=True,
                    fill_value=0,
                )
                yy, xx = np.nonzero(win)
                if len(yy):
                    d = np.hypot(
                        (yy - w) * CELL_KM, (xx - w) * CELL_KM * math.cos(math.radians(lat))
                    )
                    k = int(np.argmin(d))
                    if d[k] <= MAX_KM:
                        a, dist = int(win[yy[k], xx[k]]), round(float(d[k]), 2)
            values.append(None if a == 0 else round(a / 10000, 4))
            dists.append(dist)
    return values, dists


def regions(lats: list[float], lons: list[float], shp: Path) -> pd.DataFrame:
    """Admin-1 region per station: the polygon holding it, or the nearest within MAX_KM."""
    cols = ["name", "name_en", "type_en", "postal", "admin", "iso_a2"]
    ne = gpd.read_file(shp)[[*cols, "geometry"]].to_crs("EPSG:4326")
    pts = gpd.GeoDataFrame(
        geometry=[Point(xy) for xy in zip(lons, lats, strict=True)], crs="EPSG:4326"
    )
    hit = gpd.sjoin(pts, ne, how="left", predicate="within")
    hit = hit[~hit.index.duplicated(keep="first")]  # on a shared border: the first polygon
    hit["region_km"] = np.where(hit["name"].notna(), 0.0, np.nan)
    for i in hit.index[hit["name"].isna()]:
        p = pts.geometry[i]
        best = None
        for j in ne.sindex.query(p.buffer(0.1)):  # polygons within ~0.1 degree
            q = nearest_points(p, ne.geometry.iloc[j])[1]
            d = km(p.y, p.x, q.y, q.x)
            if best is None or d < best[0]:
                best = (d, j)
        if best and best[0] <= MAX_KM:
            for c in cols:
                hit.at[i, c] = ne.iloc[best[1]][c]
            hit.at[i, "region_km"] = round(best[0], 2)
    return pd.DataFrame(
        {
            "region": hit["name_en"].fillna(hit["name"]),
            "region_local": hit["name"],
            "region_type": hit["type_en"],
            "region_km": hit["region_km"],
            "region_postal": hit["postal"],
            "map_country": hit["admin"],
            "map_country_iso": hit["iso_a2"],
        }
    ).reset_index(drop=True)


def us_states(st: pd.DataFrame, shp: Path) -> pd.DataFrame:
    """US stations with a state in isd-history: take that state when the station lies within
    STATE_KM of it but the map placed it next door or nowhere (user 2026-11-06: border and
    rounding cases, e.g. St Joseph MO on the Missouri River, Lawson AAF on the GA/AL line,
    Dutch Harbor AK on the coast). Farther away the map's region stands: there isd-history's
    name and state belong to a later station under the same ID (e.g. 72205112841 "ATOKA MUNI,
    OK" at Orlando, Florida, 1941-1990)."""
    ne = gpd.read_file(shp)[["name", "name_en", "type_en", "postal", "admin", "iso_a2", "geometry"]]
    us = ne[ne["iso_a2"] == "US"].set_index("postal")
    st["region_source"] = np.where(st["region"].notna(), "map", None)
    rows = st["isd_ctry"].eq("US") & st["isd_state"].notna() & st["lat"].notna()
    for i in st.index[rows & st["region_postal"].ne(st["isd_state"])]:
        state = st.at[i, "isd_state"]
        if state not in us.index:
            continue
        p = Point(st.at[i, "lon"], st.at[i, "lat"])
        q = nearest_points(p, us.loc[state].geometry)[1]
        d = km(p.y, p.x, q.y, q.x)
        if d <= STATE_KM:
            row = us.loc[state]
            st.loc[i, ["region", "region_local", "region_type", "region_postal"]] = [
                row["name_en"] or row["name"],
                row["name"],
                row["type_en"],
                state,
            ]
            st.loc[i, ["map_country", "map_country_iso"]] = [row["admin"], row["iso_a2"]]
            st.at[i, "region_km"], st.at[i, "region_source"] = round(d, 2), "isd_state"
    return st


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gold", type=Path, help="gold version folder (default: the active one)")
    ap.add_argument("--out", type=Path, help="output CSV (default: catalog/station_reference.csv)")
    args = ap.parse_args()
    settings = load_settings()
    cat = settings.data_root / "catalog"
    gold = args.gold or current_gold_dir(settings)
    if gold is None:
        raise SystemExit("no gold version: build gold first")
    out = args.out or cat / "station_reference.csv"

    st = duckdb.sql(f"""
        SELECT s.station_id, s.in_gold, h."STATION NAME" AS name, nullif(h."ICAO", '') AS icao,
               nullif(h."CTRY", '') AS isd_ctry, nullif(h."STATE", '') AS isd_state,
               s.trusted_lat AS lat, s.trusted_lon AS lon, s.status AS position_status,
               CASE WHEN h."ELEV(M)" IN ('', '-0999.0', '-0999.9') THEN NULL
                    ELSE try_cast(h."ELEV(M)" AS DOUBLE) END AS elev_m,
               s.first_year, s.last_year, s.n_dusty_days, s.n_dusty_years
        FROM read_parquet('{gold}/stations/part-*.parquet') s
        LEFT JOIN read_csv('{cat}/isd-history.csv', all_varchar = true) h
          ON h."USAF" || h."WBAN" = s.station_id
        ORDER BY s.station_id""").df()

    names: dict[str, str] = {}  # NOAA's fixed-width list: "FIPS ID     COUNTRY NAME"
    with open(cat / "noaa_country_list" / "country-list.txt", encoding="ascii") as fh:
        for line in fh.read().splitlines()[1:]:
            if line.strip():
                names[line[:2]] = line[12:].strip()
    st["isd_country_name_fips"] = st["isd_ctry"].map(names)

    has = st["lat"].notna() & st["lon"].notna()
    lats, lons = st.loc[has, "lat"].tolist(), st.loc[has, "lon"].tolist()
    ai, ai_km = aridity(lats, lons)
    st.loc[has, "aridity_index"], st.loc[has, "aridity_km"] = ai, ai_km
    st["aridity_class"] = [aridity_class(None if pd.isna(a) else a) for a in st["aridity_index"]]
    reg = regions(lats, lons, cat / "naturalearth" / "ne_10m_admin_1_states_provinces.shp")
    for c in reg.columns:
        st.loc[has, c] = reg[c].to_numpy()
    st = us_states(st, cat / "naturalearth" / "ne_10m_admin_1_states_provinces.shp")
    # country code: isd-history's, with the CODE_FIXES corrections (FIPS throughout)
    st["country_code"] = st["isd_ctry"]
    for (code, where), fips in CODE_FIXES.items():
        st.loc[st["isd_ctry"].eq(code) & st["map_country"].eq(where), "country_code"] = fips
    for sid, fips in STATION_CODE_FIXES.items():
        st.loc[st["station_id"].eq(sid), "country_code"] = fips
    # the name comes from the code (NOAA's, modernised); without one, from the map
    st["country_name"] = [
        display_name(names.get(c)) if isinstance(c, str) else None for c in st["country_code"]
    ]
    st["country_name"] = st["country_name"].fillna(st["map_country"])
    st["gold_version"] = gold.name

    order = ["station_id", "in_gold", "name", "icao", "country_code", "country_name", "isd_ctry",
             "isd_country_name_fips",
             "isd_state", "region", "region_local", "region_type", "region_postal", "region_km",
             "region_source", "map_country",
             "map_country_iso", "lat", "lon", "position_status", "elev_m", "aridity_index",
             "aridity_class", "aridity_km", "first_year", "last_year", "n_dusty_days",
             "n_dusty_years", "gold_version"]  # fmt: skip
    st[order].to_csv(out, index=False, quoting=csv.QUOTE_MINIMAL)
    g = st[st["in_gold"]]
    print(f"{out}: {len(st)} stations ({len(g)} in gold) from {gold.name}")
    print(
        f"  in gold: region {g['region'].notna().sum()}, aridity {g['aridity_index'].notna().sum()}, "
        f"elevation {g['elev_m'].notna().sum()}, ICAO {g['icao'].notna().sum()}"
    )


if __name__ == "__main__":
    main()
