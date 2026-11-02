-- Monthly station statistics: dust day counts, coverage, slot counts and visibility.
-- Definitions: docs/rules.md section 11. Built from the gold `daily` table of the same run.
-- Inputs: view `daily` (written and checked just before).
--
-- Output: one row per station per calendar month of the LMST day that has at least one `daily`
-- row (gold table `monthly`). Run by aggregate.build_gold, like daily.sql.
--
-- The table is a record of counts, so any filter can be applied later (user decision 2026-10-31):
-- nothing is dropped for low coverage. Frequencies use valid days only; the `*_obs` counts take
-- any day with an observed dusty slot (a dust report is verified on its own).
--
-- Column contract: every additive count starts with `n_` and nothing else does. yearly.sql and
-- decadal.sql sum the `n_` columns generically (all but `n_calendar_days`, which they recompute,
-- and the visibility counts, which only this table reports).
--
-- `count(*) FILTER (WHERE cond)` counts the days meeting the condition. DuckDB sums BIGINT columns
-- into HUGEINT, so sums are cast back to BIGINT.
WITH d AS (
    SELECT
        *,
        CAST(year(lmst_date) AS INTEGER) AS year,
        CAST(month(lmst_date) AS INTEGER) AS month
    FROM daily
)
SELECT
    station_id,
    year,
    month,
    -- days in the calendar month (also for the first and last month of a record)
    CAST(day(last_day(make_date(year, month, 1))) AS BIGINT) AS n_calendar_days,
    count(*) AS n_days,
    -- per definition: observed (>= 1 observed slot), valid, dusty on a valid day, dusty on any day
    count(*) FILTER (WHERE n_slots_pw >= 1) AS n_obs_days,
    count(*) FILTER (WHERE valid_day) AS n_valid_days,
    count(*) FILTER (WHERE dusty_day AND valid_day) AS n_dusty_days,
    count(*) FILTER (WHERE dusty_day) AS n_dusty_days_obs,
    count(*) FILTER (WHERE n_slots_pw_synop >= 1) AS n_obs_days_synop,
    count(*) FILTER (WHERE valid_day_synop) AS n_valid_days_synop,
    count(*) FILTER (WHERE dusty_day_synop AND valid_day_synop) AS n_dusty_days_synop,
    count(*) FILTER (WHERE dusty_day_synop) AS n_dusty_days_obs_synop,
    count(*) FILTER (WHERE n_slots_pw_metar >= 1) AS n_obs_days_metar,
    count(*) FILTER (WHERE valid_day_metar) AS n_valid_days_metar,
    count(*) FILTER (WHERE dusty_day_metar AND valid_day_metar) AS n_dusty_days_metar,
    count(*) FILTER (WHERE dusty_day_metar) AS n_dusty_days_obs_metar,
    count(*) FILTER (WHERE n_slots_pw_vis5 >= 1) AS n_obs_days_vis5,
    count(*) FILTER (WHERE valid_day_vis5) AS n_valid_days_vis5,
    count(*) FILTER (WHERE dusty_day_vis5 AND valid_day_vis5) AS n_dusty_days_vis5,
    count(*) FILTER (WHERE dusty_day_vis5) AS n_dusty_days_obs_vis5,
    -- explicit observations only: a dust slot always carries an explicit code, so the dusty flag
    -- is dusty_day; only the validity changes
    count(*) FILTER (WHERE n_slots_pw_explicit >= 1) AS n_obs_days_explicit,
    count(*) FILTER (WHERE valid_day_explicit) AS n_valid_days_explicit,
    count(*) FILTER (WHERE dusty_day AND valid_day_explicit) AS n_dusty_days_explicit,
    count(*) FILTER (WHERE dusty_day) AS n_dusty_days_obs_explicit,
    count(*) FILTER (WHERE dusty_day_any AND valid_day) AS n_dusty_days_any,
    -- dust classes on valid days; raised dust = blowing, whirls or storm (codes 07 and up)
    count(*) FILTER (WHERE valid_day AND n_dust_slots_suspended > 0) AS n_days_suspended,
    count(*) FILTER (WHERE valid_day AND n_dust_slots_blowing > 0) AS n_days_blowing,
    count(*) FILTER (WHERE valid_day AND n_dust_slots_whirls > 0) AS n_days_whirls,
    count(*) FILTER (WHERE valid_day AND n_dust_slots_storm > 0) AS n_days_storm,
    count(*) FILTER (WHERE valid_day AND n_dust_slots_dust_haze > 0) AS n_days_dust_haze,
    count(*) FILTER (
        WHERE valid_day AND n_dust_slots_blowing + n_dust_slots_whirls + n_dust_slots_storm > 0
    ) AS n_days_raised,
    -- other evidence, kept apart, on any day
    count(*) FILTER (WHERE vicinity_dust_day) AS n_days_vicinity,
    count(*) FILTER (WHERE remarks_dust_day) AS n_days_remarks,
    count(*) FILTER (WHERE past_weather_dust) AS n_days_past_weather,
    count(*) FILTER (WHERE daily_summary_dust) AS n_days_daily_summary,
    -- observing density: slot sums
    CAST(sum(n_slots) AS BIGINT) AS n_slots,
    CAST(sum(n_slots_pw) AS BIGINT) AS n_slots_pw,
    CAST(sum(n_slots_pw_synop) AS BIGINT) AS n_slots_pw_synop,
    CAST(sum(n_slots_pw_metar) AS BIGINT) AS n_slots_pw_metar,
    CAST(sum(n_slots_nil_inferred) AS BIGINT) AS n_slots_nil_inferred,
    CAST(sum(n_slots_nil_metar) AS BIGINT) AS n_slots_nil_metar,
    CAST(sum(n_slots_rh_screened) AS BIGINT) AS n_slots_rh_screened,
    CAST(sum(n_dust_slots_suspended) AS BIGINT) AS n_dust_slots_suspended,
    CAST(sum(n_dust_slots_blowing) AS BIGINT) AS n_dust_slots_blowing,
    CAST(sum(n_dust_slots_whirls) AS BIGINT) AS n_dust_slots_whirls,
    CAST(sum(n_dust_slots_storm) AS BIGINT) AS n_dust_slots_storm,
    CAST(sum(n_dust_slots_dust_haze) AS BIGINT) AS n_dust_slots_dust_haze,
    CAST(sum(n_slots_dusty) AS BIGINT) AS n_slots_dusty,
    -- the same over valid days: slots per valid day and the slot frequency of dust
    CAST(coalesce(sum(n_slots_pw) FILTER (WHERE valid_day), 0) AS BIGINT) AS n_slots_pw_valid,
    CAST(coalesce(sum(n_slots_dusty) FILTER (WHERE valid_day), 0) AS BIGINT) AS n_slots_dusty_valid,
    -- rates (primary definition only; docs/rules.md 11.1):
    -- dusty_freq = n_dusty_days / n_valid_days, null without valid days
    (count(*) FILTER (WHERE dusty_day AND valid_day))
        / nullif(count(*) FILTER (WHERE valid_day), 0) AS dusty_freq,
    -- dusty_freq_obs = n_dusty_days_obs / n_obs_days, a lower bound; null without observed days
    (count(*) FILTER (WHERE dusty_day))
        / nullif(count(*) FILTER (WHERE n_slots_pw >= 1), 0) AS dusty_freq_obs,
    -- validity_ratio = (valid share of clear observed days) / (valid share of dusty observed days);
    -- a dusty day is always observed, so clear observed days = n_obs_days - n_dusty_days_obs.
    -- Null without dust seen, without a clear observed day, or without a valid dusty day.
    ((count(*) FILTER (WHERE valid_day AND NOT dusty_day))
        / nullif(count(*) FILTER (WHERE n_slots_pw >= 1 AND NOT dusty_day), 0))
        / nullif((count(*) FILTER (WHERE dusty_day AND valid_day))
            / nullif(count(*) FILTER (WHERE dusty_day), 0), 0) AS validity_ratio,
    -- visibility (this table only): extinction ~ 1/visibility, km^-1, from each day's harmonic
    -- mean visibility; days without one (none reported, or only zero visibility) are left out
    count(*) FILTER (WHERE vis_hmean > 0) AS n_days_vis,
    avg(1000.0 / vis_hmean) FILTER (WHERE vis_hmean > 0) AS ext_mean,
    count(*) FILTER (WHERE vis_hmean > 0 AND dusty_day) AS n_days_vis_dusty,
    avg(1000.0 / vis_hmean) FILTER (WHERE vis_hmean > 0 AND dusty_day) AS ext_mean_dusty,
    min(vis_min) AS vis_min,
    CAST(coalesce(sum(n_vis_zero), 0) AS BIGINT) AS n_slots_vis_zero
FROM d
GROUP BY station_id, year, month
-- sorted, so the same input gives the same file (as daily.sql and three_hourly.sql)
ORDER BY station_id, year, month
