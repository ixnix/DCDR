-- 3-hourly station product on fixed UTC bins: 00 = 00:00-02:59, 03 = 03:00-05:59, ..., 21.
-- Definitions: docs/rules.md section 10. Built from silver, independently of the daily table.
-- Inputs: view `silver`; variables `synop_types` and `metar_types` (the slot source lists,
-- config sampling.sources), which define a "regular" report.
--
-- Output: one row per station per bin with at least one report (gold table `three_hourly`).
-- Run by aggregate.build_gold, like daily.sql.
--
-- All reports count except daily/monthly summaries (is_excluded_type): SPECIs too (user decision
-- 2026-10-30, "aggregate all reports"). State values (wind, temperature, humidity, pressure,
-- visibility) come from ONE report, the first regular SYNOP or METAR in the bin (user decision
-- 2026-10-30), so a row never mixes values from different reports. Extremes, dust and counts use
-- all reports; they grow with how often a station reports, which the counts show.
--
-- How the query is built (CTEs):
--   rep     the reports in scope, with their bin, whether they are regular, and dust
--   ranked  the reports numbered within each bin: 1 = the state report
--   state   the state report of each bin, with DUP6
--   agg     per bin, from all reports: extremes, dust, counts
--   final SELECT  state joined to agg, plus local time of the bin start
WITH rep AS (
    SELECT
        *,
        -- time_bucket floors to 3-hour steps counted from a midnight, so bins start at 00, 03, ...
        time_bucket(INTERVAL 3 HOUR, time_utc) AS bin_time,
        list_contains(getvariable('synop_types'), report_type) AS is_synop,
        list_contains(getvariable('metar_types'), report_type) AS is_metar,
        is_synop OR is_metar AS is_regular,
        -- Is this report dusty? (same list as classify.DUSTY_CLASSES and daily.sql)
        list_contains(
            ['suspended', 'blowing', 'whirls', 'storm', 'storm_severe', 'storm_thunder', 'dust_haze'],
            dust_class_final
        ) AS is_dusty
    FROM silver
    WHERE NOT is_excluded_type
),
ranked AS (
    -- The state report: regular reports first, then the earliest; at the same minute a SYNOP
    -- before a METAR ("SYNOP when present", method 1.6.0); row_key makes any tie deterministic.
    -- With no regular report in the bin (e.g. only SPECIs), the earliest report of any type.
    SELECT
        *,
        row_number() OVER (
            PARTITION BY station_id, bin_time
            ORDER BY is_regular DESC, time_utc, is_synop DESC, is_metar DESC, row_key
        ) AS rn
    FROM rep
),
state AS (
    -- Each value only if it passed QC; a missing value is NOT filled from another report.
    SELECT
        station_id,
        bin_time,
        time_utc AS state_time_utc,
        report_type AS state_report_type,
        CASE WHEN use_wsd THEN wsd END AS wsd,
        CASE WHEN use_wind_dir THEN wind_dir END AS wind_dir,
        CASE WHEN use_t THEN t END AS t,
        CASE WHEN use_td THEN td END AS td,
        rh,  -- already null unless both t and td passed QC
        CASE WHEN use_slp THEN slp END AS slp,
        CASE WHEN use_vis THEN vis_capped END AS vis,
        -- DUP6, as in daily.sql: U^3 (1 + Ut/U)(1 - Ut^2/U^2) with Ut = 6 m/s
        CASE
            WHEN use_wsd AND wsd > 6 THEN pow(wsd, 3) * (1 + 6 / wsd) * (1 - 36 / pow(wsd, 2))
            WHEN use_wsd THEN 0.0
        END AS dup6
    FROM ranked
    WHERE rn = 1
),
agg AS (
    SELECT
        station_id,
        bin_time,
        -- extremes over all reports in the bin
        max(gust) FILTER (WHERE use_gust) AS gust_max,
        min(vis_capped) FILTER (WHERE use_vis) AS vis_min,
        -- dust: observed if any report observed the weather (a code, an explicit or inferred
        -- "nothing significant"); the dust columns are set in the final SELECT
        bool_or(has_pw) AS observed,
        bool_or(pw_status IN ('reported', 'nil_significant')) AS observed_explicit,
        bool_or(is_dusty) AS any_dusty,
        bool_or(dust_class_final = 'suspended') AS any_suspended,
        bool_or(dust_class_final = 'blowing') AS any_blowing,
        bool_or(dust_class_final = 'whirls') AS any_whirls,
        bool_or(dust_class_final IN ('storm', 'storm_severe', 'storm_thunder')) AS any_storm,
        bool_or(dust_class_final = 'dust_haze') AS any_dust_haze,
        bool_or(dust_class_final = 'screened') AS any_screened,
        min(time_utc) FILTER (WHERE is_dusty) AS first_dust_time_utc,
        -- separate evidence, not part of `dusty` (as in the daily table)
        bool_or(flag_vicinity) AS vicinity_dust,
        bool_or(flag_past) AS past_weather_dust,
        bool_or(coalesce(metar_rmk_dust, false)) AS remarks_dust,
        -- local mean solar time offset: 4 minutes per degree of longitude (docs/rules.md 4.2)
        any_value(lon_used) AS lon_used,
        -- counts: how often the station reported in this bin, and with which report types
        count(*) AS n_reports,
        count(*) FILTER (WHERE has_pw) AS n_reports_pw,
        count(*) FILTER (WHERE is_synop) AS n_synop,
        count(*) FILTER (WHERE is_metar) AS n_metar,
        count(*) FILTER (WHERE report_type = 'FM-16') AS n_speci,  -- SPECI
        count(*) FILTER (WHERE NOT is_regular AND report_type <> 'FM-16') AS n_other
    FROM rep
    GROUP BY station_id, bin_time
)
SELECT
    s.station_id,
    s.bin_time,
    CAST(l.lmst AS DATE) AS lmst_date,
    hour(l.lmst) + minute(l.lmst) / 60 + microsecond(l.lmst) / 3.6e9 AS lmst_hour,
    s.state_time_utc,
    s.state_report_type,
    s.wsd,
    s.wind_dir,
    s.t,
    s.td,
    s.rh,
    s.slp,
    s.vis,
    s.dup6,
    a.gust_max,
    a.vis_min,
    a.observed,
    a.observed_explicit,
    -- "if it cannot be verified, it is 'not observed'": no dust verdict without an observation
    CASE WHEN a.observed THEN a.any_dusty END AS dusty,
    CASE WHEN a.observed THEN a.any_suspended END AS c_suspended,
    CASE WHEN a.observed THEN a.any_blowing END AS c_blowing,
    CASE WHEN a.observed THEN a.any_whirls END AS c_whirls,
    CASE WHEN a.observed THEN a.any_storm END AS c_storm,
    CASE WHEN a.observed THEN a.any_dust_haze END AS c_dust_haze,
    CASE WHEN a.observed THEN a.any_screened END AS c_screened,
    a.first_dust_time_utc,
    a.vicinity_dust,
    a.past_weather_dust,
    a.remarks_dust,
    a.n_reports,
    a.n_reports_pw,
    a.n_synop,
    a.n_metar,
    a.n_speci,
    a.n_other
FROM state AS s
JOIN agg AS a ON s.station_id = a.station_id AND s.bin_time = a.bin_time
-- local mean solar time of the bin start (lon_used degrees x 4 minutes)
CROSS JOIN LATERAL (
    SELECT s.bin_time + to_microseconds(CAST(round(a.lon_used * 240e6) AS BIGINT)) AS lmst
) AS l
ORDER BY s.station_id, s.bin_time
