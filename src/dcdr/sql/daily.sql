-- Daily station product on the local mean solar time (LMST) day.
-- Definitions: docs/rules.md section 9 (rule tables: section 5.3).
-- Inputs: view `silver`; variables `min_slots` (valid-day threshold) and `vis5_max_m` (dust at
-- or below this visibility counts for dusty_day_vis5).
--
-- Output: one row per station per LMST day (gold table `daily`).
-- Run by aggregate.build_gold, which creates the `silver` view over all silver files and sets
-- the variables (config products.*) before executing this file.
--
-- Two sources per 3-hourly slot (user decision 2026-10-20, method 1.5.0): sampling keeps at most
-- one SYNOP and one METAR report per slot (`is_source_selected`). A slot is dusty if EITHER
-- reports dust, and observed if either observed the weather; per-source columns keep them apart.
-- Wind, temperature and visibility statistics use one PRIMARY report per slot
-- (`is_synoptic_selected`: the SYNOP if it observed the weather, else the METAR).
--
-- How the query is built: a chain of CTEs (WITH name AS (...)), each a named intermediate table.
--   rep           the reports chosen for the slots (one per source), with dust flags
--   slot          one row per station and slot: the sources combined
--   slot_daily    per day, from the slots: slot counts, validity, dusty-day flags, class counts
--   prim          the primary report per slot, with DUP6
--   prim_daily    per day, from the primary reports: wind, visibility, temperature, pressure
--   all_daily     per day, from ALL reports (not just slots): report count, any-dust, gust, past weather
--   summary_daily per day, from daily-summary (SOD) rows: dust in the daily summary
--   keys          the list of station-days that get a row
--   joined        everything joined onto keys; missing counts become 0
--   final SELECT  adds day-to-day changes (d_t_mean, d_slp_mean)
WITH rep AS (
    SELECT
        *,
        -- Is this report dusty? (same list as classify.DUSTY_CLASSES)
        list_contains(
            ['suspended', 'blowing', 'whirls', 'storm', 'storm_severe', 'storm_thunder', 'dust_haze'],
            dust_class_final
        ) AS is_dusty,
        -- Dust that reduced visibility to <= 5 km (dusty_day_vis5). A dusty report whose
        -- visibility is unknown cannot be judged for this measure (dusty_vis_unknown).
        is_dusty AND coalesce(use_vis, false) AND vis_raw <= getvariable('vis5_max_m') AS is_dusty_vis5,
        is_dusty AND NOT coalesce(use_vis, false) AS dusty_vis_unknown,
        -- explicit observation: a code, an iX 2/5 or CAVOK (not inferred, not implied)
        pw_status IN ('reported', 'nil_significant') AS is_explicit
    FROM silver
    WHERE is_source_selected
),
slot AS (
    -- `bool_or(cond)` is true if any of the slot's (at most two) reports meets the condition.
    -- A report filling a slot counts on the SLOT's LMST day (it may sit up to 30 min across midnight).
    SELECT
        station_id,
        slot_time,
        min(slot_lmst_date) AS lmst_date,
        bool_or(has_pw) AS pw,  -- observed by either source
        bool_or(is_explicit) AS pw_explicit,
        bool_or(is_dusty) AS dusty,  -- dust reported by either source
        bool_or(is_dusty_vis5) AS dusty_vis5,
        bool_or(dusty_vis_unknown) AS dusty_vis_unknown,
        bool_or(slot_source = 'synop') AS has_synop,
        bool_or(slot_source = 'synop' AND has_pw) AS pw_synop,
        bool_or(slot_source = 'synop' AND is_dusty) AS dusty_synop,
        bool_or(slot_source = 'metar') AS has_metar,
        bool_or(slot_source = 'metar' AND has_pw) AS pw_metar,
        bool_or(slot_source = 'metar' AND is_dusty) AS dusty_metar,
        bool_or(pw_status = 'nil_inferred') AS nil_inferred,
        bool_or(pw_status = 'nil_metar') AS nil_metar,
        -- dust classes in the slot (a slot counts in each class that one of its reports has)
        bool_or(dust_class_final = 'suspended') AS c_suspended,
        bool_or(dust_class_final = 'blowing') AS c_blowing,
        bool_or(dust_class_final = 'whirls') AS c_whirls,
        bool_or(dust_class_final IN ('storm', 'storm_severe', 'storm_thunder')) AS c_storm,
        bool_or(dust_class_final = 'dust_haze') AS c_dust_haze,
        bool_or(dust_class_final = 'screened') AS c_screened,
        min(lmst_hour) FILTER (WHERE is_dusty) AS dust_lmst_hour
    FROM rep
    GROUP BY station_id, slot_time
),
slot_daily AS (
    -- `count(*) FILTER (WHERE cond)` counts only the slots meeting the condition.
    SELECT
        station_id,
        lmst_date,
        count(*) AS n_slots,  -- slots with at least one SYNOP or METAR (max 8)
        count(*) FILTER (WHERE pw) AS n_slots_pw,  -- observed by either source
        count(*) FILTER (WHERE pw_explicit) AS n_slots_pw_explicit,
        count(*) FILTER (WHERE has_synop) AS n_slots_synop,
        count(*) FILTER (WHERE pw_synop) AS n_slots_pw_synop,
        count(*) FILTER (WHERE has_metar) AS n_slots_metar,
        count(*) FILTER (WHERE pw_metar) AS n_slots_pw_metar,
        -- slots with "nothing significant" inferred by rule V3 / implied by a METAR without group
        count(*) FILTER (WHERE nil_inferred) AS n_slots_nil_inferred,
        count(*) FILTER (WHERE nil_metar) AS n_slots_nil_metar,
        bool_or(dusty) AS dusty_day,  -- the PRIMARY dusty-day definition: either source
        bool_or(dusty_synop) AS dusty_day_synop,
        bool_or(dusty_metar) AS dusty_day_metar,
        bool_or(dusty_vis5) AS dusty_day_vis5,
        -- slots that can be judged for dust at <= 5 km: observed, and not dusty only through a
        -- report of unknown visibility
        count(*) FILTER (WHERE pw AND (dusty_vis5 OR NOT dusty_vis_unknown)) AS n_slots_pw_vis5,
        count(*) FILTER (WHERE c_suspended) AS n_dust_slots_suspended,
        count(*) FILTER (WHERE c_blowing) AS n_dust_slots_blowing,
        count(*) FILTER (WHERE c_whirls) AS n_dust_slots_whirls,
        count(*) FILTER (WHERE c_storm) AS n_dust_slots_storm,
        count(*) FILTER (WHERE c_dust_haze) AS n_dust_slots_dust_haze,
        count(*) FILTER (WHERE c_screened) AS n_slots_rh_screened,
        min(dust_lmst_hour) AS dust_first_lmst_hour  -- first dusty slot of the day
    FROM slot
    GROUP BY station_id, lmst_date
),
metar_vis AS (
    -- visibility of each slot's METAR report (user decision 2026-10-28: visibility only, and only
    -- when the slot's primary SYNOP has none)
    SELECT station_id, slot_time, any_value(vis_capped) AS vis_metar
    FROM silver
    WHERE is_source_selected AND slot_source = 'metar' AND use_vis
    GROUP BY ALL
),
prim AS (
    SELECT
        p.*,
        -- DUP6: dust uplift potential with a 6 m/s threshold, U^3 (1 + Ut/U)(1 - Ut^2/U^2),
        -- a proxy for how strongly the wind can lift dust (0 at or below 6 m/s; null if no wind).
        CASE
            WHEN p.use_wsd AND p.wsd > 6 THEN pow(p.wsd, 3) * (1 + 6 / p.wsd) * (1 - 36 / pow(p.wsd, 2))
            WHEN p.use_wsd THEN 0.0
        END AS dup6,
        -- visibility used for the day: the primary report's, else the slot METAR's
        coalesce(CASE WHEN p.use_vis THEN p.vis_capped END, m.vis_metar) AS vis_used,
        NOT coalesce(p.use_vis, false) AND m.vis_metar IS NOT NULL AS vis_from_metar
    FROM silver AS p
    LEFT JOIN metar_vis AS m ON p.station_id = m.station_id AND p.slot_time = m.slot_time
    WHERE p.is_synoptic_selected
),
prim_daily AS (
    SELECT
        station_id,
        slot_lmst_date AS lmst_date,
        avg(wsd) FILTER (WHERE use_wsd) AS wsd_mean,
        max(wsd) FILTER (WHERE use_wsd) AS wsd_max,
        min(vis_used) AS vis_min,
        -- Harmonic mean visibility, n / sum(1/v): averages extinction (~1/visibility), so a few
        -- very low visibilities in dust weigh in properly. Zero visibilities are counted apart.
        count(*) FILTER (WHERE vis_used > 0)
            / nullif(sum(1.0 / vis_used) FILTER (WHERE vis_used > 0), 0) AS vis_hmean,
        count(*) FILTER (WHERE vis_used = 0) AS n_vis_zero,
        count(*) FILTER (WHERE vis_from_metar) AS n_vis_from_metar,
        avg(t) FILTER (WHERE use_t) AS t_mean,
        min(t) FILTER (WHERE use_t) AS t_min,
        max(t) FILTER (WHERE use_t) AS t_max,
        avg(td) FILTER (WHERE use_td) AS td_mean,
        avg(rh) AS rh_mean,  -- rh is already null unless both t and td passed QC
        avg(slp) FILTER (WHERE use_slp) AS slp_mean,
        avg(dup6) AS dup6_mean
    FROM prim
    GROUP BY station_id, slot_lmst_date
),
all_daily AS (
    -- Every report except daily/monthly summaries, including the extra METAR and SPECI reports
    -- that sampling skips. More sensitive than the slot definition, but depends on how often a
    -- station reports.
    SELECT
        station_id,
        lmst_date,
        count(*) AS n_reports,
        bool_or(list_contains(
            ['suspended', 'blowing', 'whirls', 'storm', 'storm_severe', 'storm_thunder', 'dust_haze'],
            dust_class_final
        )) AS dusty_day_any,
        max(gust) FILTER (WHERE use_gust) AS gust_max,
        bool_or(flag_past) AS past_weather_dust,  -- past-weather codes (AY/AZ) resolved as sand
        -- dust in the vicinity (METAR "VC", SYNOP MV codes), not at the station: a separate flag
        bool_or(flag_vicinity) AS vicinity_dust_day,
        -- dust named only in METAR remarks (away from the station, e.g. "BLDU DSNT SW"): separate
        bool_or(coalesce(metar_rmk_dust, false)) AS remarks_dust_day
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
    -- a station-day exists if it has any non-excluded report or any slot;
    -- a day known only from a daily summary (SOD) has no row (no observations to back it)
    SELECT station_id, lmst_date FROM all_daily
    UNION
    SELECT station_id, lmst_date FROM slot_daily
),
joined AS (
    -- LEFT JOINs keep every key; a day without e.g. any slot gets nulls from slot_daily, which
    -- coalesce(..., 0 / false) turns into "zero slots" / "not dusty".
    SELECT
        k.station_id,
        k.lmst_date,
        coalesce(a.n_reports, 0) AS n_reports,
        coalesce(s.n_slots, 0) AS n_slots,
        coalesce(s.n_slots_pw, 0) AS n_slots_pw,
        -- Valid day: enough slots WITH a weather observation to judge whether it was dusty.
        coalesce(s.n_slots_pw, 0) >= getvariable('min_slots') AS valid_day,
        coalesce(s.dusty_day, false) AS dusty_day,
        -- the same from explicit observations only (codes, iX, CAVOK): no inferred or implied nil
        coalesce(s.n_slots_pw_explicit, 0) AS n_slots_pw_explicit,
        coalesce(s.n_slots_pw_explicit, 0) >= getvariable('min_slots') AS valid_day_explicit,
        coalesce(s.n_slots_nil_inferred, 0) AS n_slots_nil_inferred,
        coalesce(s.n_slots_nil_metar, 0) AS n_slots_nil_metar,
        -- per source: SYNOP alone (comparable with duISD and SYNOP-only stations) and METAR alone
        coalesce(s.n_slots_synop, 0) AS n_slots_synop,
        coalesce(s.n_slots_pw_synop, 0) AS n_slots_pw_synop,
        coalesce(s.n_slots_pw_synop, 0) >= getvariable('min_slots') AS valid_day_synop,
        coalesce(s.dusty_day_synop, false) AS dusty_day_synop,
        coalesce(s.n_slots_metar, 0) AS n_slots_metar,
        coalesce(s.n_slots_pw_metar, 0) AS n_slots_pw_metar,
        coalesce(s.n_slots_pw_metar, 0) >= getvariable('min_slots') AS valid_day_metar,
        coalesce(s.dusty_day_metar, false) AS dusty_day_metar,
        -- dust at <= 5 km (either source): caught alike by SYNOP and METAR
        coalesce(s.n_slots_pw_vis5, 0) AS n_slots_pw_vis5,
        coalesce(s.n_slots_pw_vis5, 0) >= getvariable('min_slots') AS valid_day_vis5,
        coalesce(s.dusty_day_vis5, false) AS dusty_day_vis5,
        coalesce(a.dusty_day_any, false) AS dusty_day_any,
        coalesce(a.past_weather_dust, false) AS past_weather_dust,
        coalesce(m.daily_summary_dust, false) AS daily_summary_dust,
        coalesce(a.vicinity_dust_day, false) AS vicinity_dust_day,
        coalesce(a.remarks_dust_day, false) AS remarks_dust_day,
        -- The most sensitive definition: dust from any report, past weather or daily summary.
        coalesce(a.dusty_day_any, false) OR coalesce(a.past_weather_dust, false)
            OR coalesce(m.daily_summary_dust, false) AS dusty_day_any_source,
        coalesce(s.n_dust_slots_suspended, 0) AS n_dust_slots_suspended,
        coalesce(s.n_dust_slots_blowing, 0) AS n_dust_slots_blowing,
        coalesce(s.n_dust_slots_whirls, 0) AS n_dust_slots_whirls,
        coalesce(s.n_dust_slots_storm, 0) AS n_dust_slots_storm,
        coalesce(s.n_dust_slots_dust_haze, 0) AS n_dust_slots_dust_haze,
        coalesce(s.n_slots_rh_screened, 0) AS n_slots_rh_screened,
        s.dust_first_lmst_hour,
        p.wsd_mean,
        p.wsd_max,
        a.gust_max,
        p.vis_min,
        p.vis_hmean,
        coalesce(p.n_vis_zero, 0) AS n_vis_zero,
        coalesce(p.n_vis_from_metar, 0) AS n_vis_from_metar,
        p.t_mean,
        p.t_min,
        p.t_max,
        p.td_mean,
        p.rh_mean,
        p.slp_mean,
        p.dup6_mean
    FROM keys AS k
    LEFT JOIN all_daily AS a ON k.station_id = a.station_id AND k.lmst_date = a.lmst_date
    LEFT JOIN slot_daily AS s ON k.station_id = s.station_id AND k.lmst_date = s.lmst_date
    LEFT JOIN prim_daily AS p ON k.station_id = p.station_id AND k.lmst_date = p.lmst_date
    LEFT JOIN summary_daily AS m ON k.station_id = m.station_id AND k.lmst_date = m.lmst_date
)
-- Day-to-day changes in mean temperature and pressure (signals of fronts, which often bring
-- dust). `lag(x) OVER w` is x on the previous row of the same station (window w). A change is
-- only computed between two VALID days that are exactly one calendar day apart.
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
