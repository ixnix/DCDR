-- Station positions: which position to trust for each station (gold table `stations`).
-- Definitions: docs/rules.md, "Station positions" (user decisions 2026-11-02 and 2026-11-03).
-- Inputs: view `silver_all` (every silver report, before stations are left out); views
-- `isd_history` (station_id, isd_name, isd_lat, isd_lon; null = no position) and `overrides`
-- (station_id, lat, lon, action, reason, decided), registered by aggregate.build_gold from
-- stations.py; variables `agree_km`, `conflict_km`, `isd_history_md5`, `overrides_md5`.
--
-- Output: one row per station in silver. Built FIRST: build_gold then narrows the `silver` view
-- to the stations with `in_gold`, which every later product reads.
--
-- The rule per station (its LARGEST distance over its station-years, in-file vs isd-history):
--   no isd-history position (or not listed)        -> unverified (kept in gold, marked)
--   USAF 949999, isd-history not in the south      -> conflict (southern-hemisphere gate)
--   no in-file latitude/longitude to compare       -> unverified
--   >= conflict_km: USAF 949999 -> resolved_isd_history (isd-history position), else conflict
--   >= agree_km -> moved (in-file, flagged); else agree (in-file)
-- An override row wins over the rule: `use` its position, or `exclude` the station.
--
-- How the query is built (CTEs):
--   rep      the reports: year, in-file position, dusty or not
--   sy       each station-year's most common in-file position (both coordinates known)
--   dist     each station's largest distance to its isd-history position (haversine, 6371 km)
--   file_lon / file_lat  the station's most common lon_used / lat (the offset gold used so far)
--   base     years and dusty reports per station
--   judged   the rule's verdict (computed_status)
--   final SELECT  the override applied: status, in_gold, trusted position
WITH rep AS (
    SELECT
        station_id,
        CAST(year(time_utc) AS INTEGER) AS year,
        lat,
        lon,
        lon_used,
        -- dusty report (same list as classify.DUSTY_CLASSES); summaries do not count
        NOT is_excluded_type AND list_contains(
            ['suspended', 'blowing', 'whirls', 'storm', 'storm_severe', 'storm_thunder', 'dust_haze'],
            dust_class_final
        ) AS is_dusty
    FROM silver_all
),
sy AS (
    SELECT station_id, year, lat, lon
    FROM (
        SELECT station_id, year, lat, lon, count(*) AS n
        FROM rep
        WHERE lat IS NOT NULL AND lon IS NOT NULL
        GROUP BY station_id, year, lat, lon
    )
    QUALIFY row_number() OVER (PARTITION BY station_id, year ORDER BY n DESC, lat, lon) = 1
),
dist AS (
    SELECT
        sy.station_id,
        max(2 * 6371 * asin(sqrt(
            pow(sin(radians(sy.lat - h.isd_lat) / 2), 2)
            + cos(radians(sy.lat)) * cos(radians(h.isd_lat))
                * pow(sin(radians(sy.lon - h.isd_lon) / 2), 2)
        ))) AS max_km
    FROM sy
    JOIN isd_history AS h ON sy.station_id = h.station_id
    WHERE h.isd_lat IS NOT NULL
    GROUP BY sy.station_id
),
file_lon AS (
    SELECT station_id, lon_used AS file_lon
    FROM (SELECT station_id, lon_used, count(*) AS n FROM rep GROUP BY station_id, lon_used)
    QUALIFY row_number() OVER (PARTITION BY station_id ORDER BY n DESC, lon_used) = 1
),
file_lat AS (
    SELECT station_id, lat AS file_lat
    FROM (
        SELECT station_id, lat, count(*) AS n
        FROM rep
        WHERE lat IS NOT NULL
        GROUP BY station_id, lat
    )
    QUALIFY row_number() OVER (PARTITION BY station_id ORDER BY n DESC, lat) = 1
),
base AS (
    SELECT
        station_id,
        min(year) AS first_year,
        max(year) AS last_year,
        count(DISTINCT year) AS n_years,
        count(*) FILTER (WHERE is_dusty) AS n_dusty_reports
    FROM rep
    GROUP BY station_id
),
judged AS (
    SELECT
        b.*,
        fl.file_lon,
        fa.file_lat,
        h.isd_name,
        h.isd_lat,
        h.isd_lon,
        d.max_km,
        CASE
            WHEN h.isd_lat IS NULL THEN 'unverified'
            WHEN starts_with(b.station_id, '949999') AND h.isd_lat >= 0 THEN 'conflict'
            WHEN d.max_km IS NULL THEN 'unverified'
            WHEN d.max_km >= getvariable('conflict_km') AND starts_with(b.station_id, '949999')
                THEN 'resolved_isd_history'
            WHEN d.max_km >= getvariable('conflict_km') THEN 'conflict'
            WHEN d.max_km >= getvariable('agree_km') THEN 'moved'
            ELSE 'agree'
        END AS computed_status
    FROM base AS b
    JOIN file_lon AS fl ON b.station_id = fl.station_id
    LEFT JOIN file_lat AS fa ON b.station_id = fa.station_id
    LEFT JOIN isd_history AS h ON b.station_id = h.station_id
    LEFT JOIN dist AS d ON b.station_id = d.station_id
)
SELECT
    j.station_id,
    CASE WHEN o.station_id IS NOT NULL THEN 'override' ELSE j.computed_status END AS status,
    j.computed_status,
    CASE
        WHEN o.action = 'exclude' THEN false
        WHEN o.action = 'use' THEN true
        ELSE j.computed_status <> 'conflict'
    END AS in_gold,
    CASE
        WHEN o.action = 'use' THEN o.lat
        WHEN o.action = 'exclude' OR j.computed_status = 'conflict' THEN NULL
        WHEN j.computed_status = 'resolved_isd_history' THEN j.isd_lat
        ELSE j.file_lat
    END AS trusted_lat,
    CASE
        WHEN o.action = 'use' THEN o.lon
        WHEN o.action = 'exclude' OR j.computed_status = 'conflict' THEN NULL
        WHEN j.computed_status = 'resolved_isd_history' THEN j.isd_lon
        ELSE j.file_lon
    END AS trusted_lon,
    j.file_lat,
    j.file_lon,
    j.isd_name,
    j.isd_lat,
    j.isd_lon,
    j.max_km,
    j.first_year,
    j.last_year,
    j.n_years,
    j.n_dusty_reports,
    o.action AS override_action,
    o.reason AS override_reason,
    getvariable('isd_history_md5') AS isd_history_md5,
    getvariable('overrides_md5') AS overrides_md5
FROM judged AS j
LEFT JOIN overrides AS o ON j.station_id = o.station_id
ORDER BY j.station_id  -- reproducible files
