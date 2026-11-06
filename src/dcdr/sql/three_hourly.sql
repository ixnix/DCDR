-- 3-hourly station product on fixed UTC bins: 00 = 00:00-02:59, 03 = 03:00-05:59, ..., 21.
-- Definitions: docs/rules.md section 10. Built from silver, independently of the daily table.
-- Inputs: view `silver` (its `source_group`: synop / metar / other, method 2.0.0); variable
-- `vis5_max_m` (dust at or below this visibility counts for dust_vis5); view `note_years`
-- (station_id, year, note: the period notes of that year, docs/rules.md 4.3).
--
-- Output: one row per station per bin with at least one report (gold table `three_hourly`).
-- Run by aggregate.build_gold, like daily.sql.
--
-- All reports count except daily/monthly summaries (is_excluded_type): SPECIs too (user decision
-- 2026-10-30, "aggregate all reports"). State values (wind, temperature, humidity, pressure,
-- visibility) come from ONE report, the first regular SYNOP or METAR in the bin (user decision
-- 2026-10-30), so a row never mixes values from different reports. Extremes, dust and counts use
-- all reports; they grow with how often a station reports, which the counts show.
-- Regular reports: the SYNOP group, and the METAR group without its special reports (SPECI FM-16,
-- SAOSP), which are sent because conditions changed.
--
-- Local time (method 2.0.0): ONE LMST offset per station, from its TRUSTED longitude (gold table
-- `stations`, docs/rules.md 4.3: the most common in-file longitude, or isd-history / an override
-- where those are trusted), so every local day holds exactly the 8 bins that start on it.
-- With the state report's own longitude, a 0.1-degree difference between report types moved a
-- bin across local midnight near some longitudes (9 bins on one day, 7 on the next).
--
-- How the query is built (CTEs):
--   station the station's LMST offset, from `stations`
--   rep     the reports in scope, with their bin, whether they are regular, and dust
--   ranked  the reports numbered within each bin: 1 = the state report
--   state   the state report of each bin, with DUP6
--   agg     per bin, from all reports: extremes, dust, counts
--   final SELECT  state joined to agg, plus local time of the bin start
WITH station AS (
    -- 4 min per degree of the trusted longitude (never null for a station in gold)
    SELECT station_id, to_milliseconds(CAST(round(trusted_lon * 240000) AS BIGINT)) AS lmst_offset
    FROM stations
    WHERE in_gold
),
rep AS (
    SELECT
        *,
        -- time_bucket floors to 3-hour steps counted from a midnight, so bins start at 00, 03, ...
        time_bucket(INTERVAL 3 HOUR, time_utc) AS bin_time,
        -- special reports, sent because conditions changed: SPECI (FM-16) and its pre-1996 US
        -- predecessor SAOSP, "Airways special report" (ISD format document p. 6)
        report_type IN ('FM-16', 'SAOSP') AS is_special,
        source_group = 'synop' AS is_synop,
        source_group = 'metar' AND NOT is_special AS is_metar,
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
        max(wsd) FILTER (WHERE use_wsd) AS wsd_max,
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
        -- per source group: did it observe the weather, did it see dust
        bool_or(has_pw AND source_group = 'synop') AS obs_synop,
        bool_or(has_pw AND source_group = 'metar') AS obs_metar,
        bool_or(has_pw AND source_group = 'other') AS obs_other,
        bool_or(is_dusty AND source_group = 'synop') AS any_dust_synop,
        bool_or(is_dusty AND source_group = 'metar') AS any_dust_metar,
        bool_or(is_dusty AND source_group = 'other') AS any_dust_other,
        -- dust at <= 5 km, judged with each dusty report's own visibility; a dusty report of
        -- unknown visibility might have been at <= 5 km
        bool_or(is_dusty AND coalesce(use_vis, false) AND vis_raw <= getvariable('vis5_max_m'))
            AS any_dust_vis5,
        bool_or(is_dusty AND NOT coalesce(use_vis, false)) AS any_dust_vis_unknown,
        -- "nothing significant" inferred (rule V3, METAR fragments) or implied (METAR, no group)
        bool_or(pw_status = 'nil_inferred') AS nil_inferred,
        bool_or(pw_status = 'nil_metar') AS nil_metar,
        min(time_utc) FILTER (WHERE is_dusty) AS first_dust_time_utc,
        -- separate evidence, not part of `dusty` (as in the daily table)
        bool_or(flag_vicinity) AS vicinity_dust,
        bool_or(flag_past) AS past_weather_dust,
        bool_or(coalesce(metar_rmk_dust, false)) AS remarks_dust,
        -- counts: how often the station reported in this bin, and with which report types
        count(*) AS n_reports,
        count(*) FILTER (WHERE has_pw) AS n_reports_pw,
        count(*) FILTER (WHERE is_synop) AS n_synop,
        count(*) FILTER (WHERE is_metar) AS n_metar,
        count(*) FILTER (WHERE is_special) AS n_speci,  -- SPECI and SAOSP
        count(*) FILTER (WHERE NOT is_regular AND NOT is_special) AS n_other
    FROM rep
    GROUP BY station_id, bin_time
)
SELECT
    s.station_id,
    s.bin_time,
    CAST(s.bin_time + st.lmst_offset AS DATE) AS lmst_date,
    hour(s.bin_time + st.lmst_offset) + minute(s.bin_time + st.lmst_offset) / 60
        + microsecond(s.bin_time + st.lmst_offset) / 3.6e9 AS lmst_hour,
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
    a.wsd_max,
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
    -- per source: true = dust, false = observed without dust, null = that source did not observe
    CASE WHEN a.obs_synop THEN a.any_dust_synop END AS dust_synop,
    CASE WHEN a.obs_metar THEN a.any_dust_metar END AS dust_metar,
    CASE WHEN a.obs_other THEN a.any_dust_other END AS dust_other,
    CASE
        WHEN NOT a.observed THEN NULL
        WHEN a.any_dust_vis5 THEN true
        WHEN a.any_dust_vis_unknown THEN NULL
        ELSE false
    END AS dust_vis5,
    a.nil_inferred,
    a.nil_metar,
    a.first_dust_time_utc,
    a.vicinity_dust,
    a.past_weather_dust,
    a.remarks_dust,
    a.n_reports,
    a.n_reports_pw,
    a.n_synop,
    a.n_metar,
    a.n_speci,
    a.n_other,
    -- reports from another place under this station ID in this year (period notes); null if none
    nt.note
FROM state AS s
JOIN agg AS a ON s.station_id = a.station_id AND s.bin_time = a.bin_time
JOIN station AS st ON s.station_id = st.station_id
LEFT JOIN note_years AS nt
    ON s.station_id = nt.station_id AND nt.year = year(CAST(s.bin_time + st.lmst_offset AS DATE))
ORDER BY s.station_id, s.bin_time
