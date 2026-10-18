-- Daily station product on the local mean solar time (LMST) day.
-- Definitions: docs/superpowers/specs/2026-10-15-dcdr-pilot-design.md section 7.3.2.
-- Inputs: view `silver`; variable `min_slots` (valid-day threshold).
WITH sel AS (
    SELECT
        *,
        list_contains(
            ['suspended', 'blowing', 'whirls', 'storm', 'storm_severe', 'storm_thunder', 'dust_haze'],
            dust_class_final
        ) AS is_dusty,
        CASE
            WHEN use_wsd AND wsd > 6 THEN pow(wsd, 3) * (1 + 6 / wsd) * (1 - 36 / pow(wsd, 2))
            WHEN use_wsd THEN 0.0
        END AS dup6
    FROM silver
    WHERE is_synoptic_selected
),
sel_daily AS (
    -- a report filling a slot counts on the SLOT's LMST day (it may sit up to 30 min across midnight)
    SELECT
        station_id,
        slot_lmst_date AS lmst_date,
        count(*) AS n_slots,
        count(*) FILTER (WHERE has_pw) AS n_slots_pw,
        bool_or(is_dusty) AS dusty_day_synoptic,
        count(*) FILTER (WHERE dust_class_final = 'suspended') AS n_dust_slots_suspended,
        count(*) FILTER (WHERE dust_class_final = 'blowing') AS n_dust_slots_blowing,
        count(*) FILTER (WHERE dust_class_final = 'whirls') AS n_dust_slots_whirls,
        count(*) FILTER (WHERE dust_class_final IN ('storm', 'storm_severe', 'storm_thunder')) AS n_dust_slots_storm,
        count(*) FILTER (WHERE dust_class_final = 'dust_haze') AS n_dust_slots_dust_haze,
        count(*) FILTER (WHERE dust_class_final = 'screened') AS n_slots_rh_screened,
        min(lmst_hour) FILTER (WHERE is_dusty) AS dust_first_lmst_hour,
        avg(wsd) FILTER (WHERE use_wsd) AS wsd_mean,
        max(wsd) FILTER (WHERE use_wsd) AS wsd_max,
        min(vis_capped) FILTER (WHERE use_vis) AS vis_min,
        count(*) FILTER (WHERE use_vis AND vis_capped > 0)
            / nullif(sum(1.0 / vis_capped) FILTER (WHERE use_vis AND vis_capped > 0), 0) AS vis_hmean,
        count(*) FILTER (WHERE use_vis AND vis_capped = 0) AS n_vis_zero,
        avg(t) FILTER (WHERE use_t) AS t_mean,
        min(t) FILTER (WHERE use_t) AS t_min,
        max(t) FILTER (WHERE use_t) AS t_max,
        avg(td) FILTER (WHERE use_td) AS td_mean,
        avg(rh) AS rh_mean,
        avg(slp) FILTER (WHERE use_slp) AS slp_mean,
        avg(dup6) AS dup6_mean
    FROM sel
    GROUP BY station_id, slot_lmst_date
),
all_daily AS (
    SELECT
        station_id,
        lmst_date,
        count(*) AS n_reports,
        bool_or(list_contains(
            ['suspended', 'blowing', 'whirls', 'storm', 'storm_severe', 'storm_thunder', 'dust_haze'],
            dust_class_final
        )) AS dusty_day_any,
        max(gust) FILTER (WHERE use_gust) AS gust_max,
        bool_or(flag_past) AS past_weather_dust
    FROM silver
    WHERE NOT is_excluded_type
    GROUP BY station_id, lmst_date
),
summary_daily AS (
    -- daily-summary (SOD) rows are excluded from sampling but carry AT/AX dust codes;
    -- they are dated to the day they summarise (summary_lmst_date), not their timestamp
    SELECT
        station_id,
        coalesce(summary_lmst_date, lmst_date) AS lmst_date,
        bool_or(flag_daily_dust) AS daily_summary_dust
    FROM silver
    GROUP BY ALL
),
keys AS (
    -- a station-day exists if it has any non-excluded report or any selected slot;
    -- a day known only from a daily summary (SOD) has no row (no observations to back it)
    SELECT station_id, lmst_date FROM all_daily
    UNION
    SELECT station_id, lmst_date FROM sel_daily
),
joined AS (
    SELECT
        k.station_id,
        k.lmst_date,
        coalesce(a.n_reports, 0) AS n_reports,
        coalesce(s.n_slots, 0) AS n_slots,
        coalesce(s.n_slots_pw, 0) AS n_slots_pw,
        coalesce(s.n_slots_pw, 0) >= getvariable('min_slots') AS valid_day,
        coalesce(s.dusty_day_synoptic, false) AS dusty_day_synoptic,
        coalesce(a.dusty_day_any, false) AS dusty_day_any,
        coalesce(a.past_weather_dust, false) AS past_weather_dust,
        coalesce(m.daily_summary_dust, false) AS daily_summary_dust,
        coalesce(a.dusty_day_any, false) OR coalesce(a.past_weather_dust, false)
            OR coalesce(m.daily_summary_dust, false) AS dusty_day_any_source,
        coalesce(s.n_dust_slots_suspended, 0) AS n_dust_slots_suspended,
        coalesce(s.n_dust_slots_blowing, 0) AS n_dust_slots_blowing,
        coalesce(s.n_dust_slots_whirls, 0) AS n_dust_slots_whirls,
        coalesce(s.n_dust_slots_storm, 0) AS n_dust_slots_storm,
        coalesce(s.n_dust_slots_dust_haze, 0) AS n_dust_slots_dust_haze,
        coalesce(s.n_slots_rh_screened, 0) AS n_slots_rh_screened,
        s.dust_first_lmst_hour,
        s.wsd_mean,
        s.wsd_max,
        a.gust_max,
        s.vis_min,
        s.vis_hmean,
        coalesce(s.n_vis_zero, 0) AS n_vis_zero,
        s.t_mean,
        s.t_min,
        s.t_max,
        s.td_mean,
        s.rh_mean,
        s.slp_mean,
        s.dup6_mean
    FROM keys AS k
    LEFT JOIN all_daily AS a ON k.station_id = a.station_id AND k.lmst_date = a.lmst_date
    LEFT JOIN sel_daily AS s ON k.station_id = s.station_id AND k.lmst_date = s.lmst_date
    LEFT JOIN summary_daily AS m ON k.station_id = m.station_id AND k.lmst_date = m.lmst_date
)
SELECT
    *,
    CASE
        WHEN valid_day AND lag(valid_day) OVER w AND date_diff('day', lag(lmst_date) OVER w, lmst_date) = 1
        THEN t_mean - lag(t_mean) OVER w
    END AS d_t_mean,
    CASE
        WHEN valid_day AND lag(valid_day) OVER w AND date_diff('day', lag(lmst_date) OVER w, lmst_date) = 1
        THEN slp_mean - lag(slp_mean) OVER w
    END AS d_slp_mean
FROM joined
WINDOW w AS (PARTITION BY station_id ORDER BY lmst_date)
ORDER BY station_id, lmst_date
