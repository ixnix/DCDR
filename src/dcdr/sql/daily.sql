-- Daily station product on the local mean solar time (LMST) day.
-- Definitions: docs/rules.md section 9 (rule tables: section 5.3).
-- Inputs: view `three_hourly` (the bins, built just before in the same gold run); view `silver`,
-- read only for daily-summary (SOD) dust, dated with the trusted longitude in view `stations`;
-- variable `min_bins` (valid-day threshold); view `note_years` (station_id, year, note: the
-- period notes of that year, docs/rules.md 4.3).
--
-- Output: one row per station per LMST day (gold table `daily`).
-- Run by aggregate.build_gold, which creates the views and sets the variables (config
-- products.*) before executing this file.
--
-- All reports, in 3-hour bins (method 2.0.0, user decisions 2026-11-02): a day holds the 8 bins
-- that START on its LMST date (one LMST offset per station, three_hourly.sql). A bin is observed
-- if any report in it observed the weather, and dusty if any report in it is dusty. Dusty day:
-- >= 1 dusty bin; valid day: >= min_bins observed bins. Each source group is also judged alone
-- (`dust_synop` etc.: true = dust, false = observed without dust, null = not observed by it).
-- Weather means come from the bins' state reports; the extremes (wind, gust, visibility) from all
-- reports. Evidence (past weather, daily summaries, vicinity, remarks) is never counted as dust.
--
-- How the query is built: a chain of CTEs (WITH name AS (...)), each a named intermediate table.
--   bin_daily     per day, from the bins: counts, dust, per source, vis5, classes, weather
--   summary_daily per day, from daily-summary (SOD) rows: dust in the daily summary
--   joined        bin_daily with the summaries and the validity flags
--   final SELECT  adds day-to-day changes (d_t_mean, d_slp_mean)
WITH bin_daily AS (
    -- `count(*) FILTER (WHERE cond)` counts the bins meeting the condition; `count(x)` counts the
    -- bins where x is not null; `bool_or(x)` is true if x is true in any bin.
    SELECT
        station_id,
        lmst_date,
        CAST(sum(n_reports) AS BIGINT) AS n_reports,
        count(*) AS n_bins,  -- bins with at least one report (max 8)
        count(*) FILTER (WHERE observed) AS n_bins_obs,
        count(*) FILTER (WHERE observed_explicit) AS n_bins_obs_explicit,
        count(*) FILTER (WHERE dusty) AS n_bins_dusty,
        count(*) FILTER (WHERE nil_inferred) AS n_bins_nil_inferred,
        count(*) FILTER (WHERE nil_metar) AS n_bins_nil_metar,
        count(*) FILTER (WHERE c_screened) AS n_bins_rh_screened,
        -- each source group alone: bins it observed, bins it saw dust in
        count(dust_synop) AS n_bins_obs_synop,
        count(dust_metar) AS n_bins_obs_metar,
        count(dust_other) AS n_bins_obs_other,
        count(*) FILTER (WHERE dust_synop) AS n_bins_dust_synop,
        count(*) FILTER (WHERE dust_metar) AS n_bins_dust_metar,
        count(*) FILTER (WHERE dust_other) AS n_bins_dust_other,
        -- dust at <= 5 km: bins that can be judged, and bins with such dust
        count(dust_vis5) AS n_bins_obs_vis5,
        count(*) FILTER (WHERE dust_vis5) AS n_bins_dust_vis5,
        -- dust classes (a bin counts in each class that one of its reports has)
        count(*) FILTER (WHERE c_suspended) AS n_dust_bins_suspended,
        count(*) FILTER (WHERE c_blowing) AS n_dust_bins_blowing,
        count(*) FILTER (WHERE c_whirls) AS n_dust_bins_whirls,
        count(*) FILTER (WHERE c_storm) AS n_dust_bins_storm,
        count(*) FILTER (WHERE c_dust_haze) AS n_dust_bins_dust_haze,
        min(lmst_hour) FILTER (WHERE dusty) AS dust_first_lmst_hour,  -- start of the first dusty bin
        -- evidence, kept apart
        bool_or(past_weather_dust) AS past_weather_dust,  -- past-weather codes (AY/AZ) as sand
        bool_or(vicinity_dust) AS vicinity_dust_day,  -- METAR "VC", SYNOP MV codes
        bool_or(remarks_dust) AS remarks_dust_day,  -- e.g. "BLDU DSNT SW" in METAR remarks
        -- weather means from the state reports (one per bin); each value only if it passed QC
        avg(wsd) AS wsd_mean,
        -- Harmonic mean visibility, n / sum(1/v): averages extinction (~1/visibility), so a few
        -- very low visibilities in dust weigh in properly. Zero visibilities are counted apart.
        count(*) FILTER (WHERE vis > 0)
            / nullif(sum(1.0 / vis) FILTER (WHERE vis > 0), 0) AS vis_hmean,
        count(*) FILTER (WHERE vis = 0) AS n_vis_zero,
        avg(t) AS t_mean,
        min(t) AS t_min,
        max(t) AS t_max,
        avg(td) AS td_mean,
        avg(rh) AS rh_mean,
        avg(slp) AS slp_mean,
        avg(dup6) AS dup6_mean,
        -- extremes from all reports
        max(wsd_max) AS wsd_max,
        max(gust_max) AS gust_max,
        min(vis_min) AS vis_min
    FROM three_hourly
    GROUP BY station_id, lmst_date
),
summary_daily AS (
    -- daily-summary (SOD) rows are not in the bins but carry AT/AX dust codes. They are stamped
    -- 23:59 local STANDARD time, which can fall after LMST midnight, so they are dated to the day
    -- they summarise, date(LMST - 12 h) (as parse.py does), with the station's TRUSTED longitude
    -- (docs/rules.md 4.3), not the report's own (wrong at some 949999 stations)
    SELECT
        r.station_id,
        CAST(
            r.time_utc + to_milliseconds(CAST(round(st.trusted_lon * 240000) AS BIGINT))
                - CASE WHEN r.report_type = 'SOD' THEN INTERVAL 12 HOUR ELSE INTERVAL 0 HOUR END
            AS DATE
        ) AS lmst_date,
        bool_or(r.flag_daily_dust) AS daily_summary_dust
    FROM silver AS r
    JOIN stations AS st ON r.station_id = st.station_id
    WHERE r.is_excluded_type
    GROUP BY ALL
),
joined AS (
    -- A day exists if it has a bin; a day known only from a daily summary has no row (no
    -- observations to back it).
    SELECT
        b.station_id,
        b.lmst_date,
        b.n_reports,
        b.n_bins,
        b.n_bins_obs,
        -- Valid day: enough bins WITH a weather observation to judge whether it was dusty.
        b.n_bins_obs >= getvariable('min_bins') AS valid_day,
        b.n_bins_dusty > 0 AS dusty_day,
        b.n_bins_dusty,
        -- the same from explicit observations only (codes, iX, CAVOK): no inferred or implied nil
        b.n_bins_obs_explicit,
        b.n_bins_obs_explicit >= getvariable('min_bins') AS valid_day_explicit,
        b.n_bins_nil_inferred,
        b.n_bins_nil_metar,
        -- each source group judged alone
        b.n_bins_obs_synop,
        b.n_bins_dust_synop,
        b.n_bins_obs_synop >= getvariable('min_bins') AS valid_day_synop,
        b.n_bins_dust_synop > 0 AS dusty_day_synop,
        b.n_bins_obs_metar,
        b.n_bins_dust_metar,
        b.n_bins_obs_metar >= getvariable('min_bins') AS valid_day_metar,
        b.n_bins_dust_metar > 0 AS dusty_day_metar,
        b.n_bins_obs_other,
        b.n_bins_dust_other,
        b.n_bins_obs_other >= getvariable('min_bins') AS valid_day_other,
        b.n_bins_dust_other > 0 AS dusty_day_other,
        -- dust at <= 5 km: caught alike by SYNOP and METAR
        b.n_bins_obs_vis5,
        b.n_bins_obs_vis5 >= getvariable('min_bins') AS valid_day_vis5,
        b.n_bins_dust_vis5 > 0 AS dusty_day_vis5,
        coalesce(b.past_weather_dust, false) AS past_weather_dust,
        coalesce(m.daily_summary_dust, false) AS daily_summary_dust,
        coalesce(b.vicinity_dust_day, false) AS vicinity_dust_day,
        coalesce(b.remarks_dust_day, false) AS remarks_dust_day,
        b.n_dust_bins_suspended,
        b.n_dust_bins_blowing,
        b.n_dust_bins_whirls,
        b.n_dust_bins_storm,
        b.n_dust_bins_dust_haze,
        b.n_bins_rh_screened,
        b.dust_first_lmst_hour,
        b.wsd_mean,
        b.wsd_max,
        b.gust_max,
        b.vis_min,
        b.vis_hmean,
        b.n_vis_zero,
        b.t_mean,
        b.t_min,
        b.t_max,
        b.td_mean,
        b.rh_mean,
        b.slp_mean,
        b.dup6_mean,
        -- reports from another place under this station ID in this year (period notes)
        nt.note
    FROM bin_daily AS b
    LEFT JOIN summary_daily AS m ON b.station_id = m.station_id AND b.lmst_date = m.lmst_date
    LEFT JOIN note_years AS nt ON b.station_id = nt.station_id AND nt.year = year(b.lmst_date)
)
-- Day-to-day changes in mean temperature and pressure (signals of fronts, which often bring
-- dust). `lag(x) OVER w` is x on the previous row of the same station (window w). A change is
-- only computed between two VALID days that are exactly one calendar day apart.
SELECT
    * EXCLUDE (note),
    CASE
        WHEN valid_day AND lag(valid_day) OVER w AND date_diff('day', lag(lmst_date) OVER w, lmst_date) = 1
        THEN t_mean - lag(t_mean) OVER w
    END AS d_t_mean,
    CASE
        WHEN valid_day AND lag(valid_day) OVER w AND date_diff('day', lag(lmst_date) OVER w, lmst_date) = 1
        THEN slp_mean - lag(slp_mean) OVER w
    END AS d_slp_mean,
    note  -- last, as in every gold table
FROM joined
WINDOW w AS (PARTITION BY station_id ORDER BY lmst_date)
ORDER BY station_id, lmst_date
