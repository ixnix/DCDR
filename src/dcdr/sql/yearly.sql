-- Yearly station statistics: the monthly counts summed per calendar year of the LMST day.
-- Definitions: docs/rules.md section 11. Built from the gold `monthly` table of the same run.
-- Inputs: view `monthly`; variable `min_valid_days_month` (a month with at least this many valid
-- days counts in n_months_covered).
--
-- Output: one row per station per year with at least one monthly row (gold table `yearly`).
-- No visibility (user decision 2026-10-31: visibility only in `monthly`).
--
-- Every additive monthly count starts with `n_` (monthly.sql), so one COLUMNS(...) expression sums
-- them all under their own names: every `n_` column except n_calendar_days (recomputed for the
-- year) and the visibility counts (listed by name: a '_vis' pattern would also drop the `_vis5`
-- counts). DuckDB sums BIGINT into HUGEINT, so the sums are cast back to BIGINT.
SELECT
    station_id,
    year,
    CAST(date_diff('day', make_date(year, 1, 1), make_date(year + 1, 1, 1)) AS BIGINT)
        AS n_calendar_days,
    CAST(sum(COLUMNS(c -> starts_with(c, 'n_') AND c NOT IN (
        'n_calendar_days', 'n_days_vis', 'n_days_vis_dusty', 'n_bins_vis_zero'
    ))) AS BIGINT),
    -- the rates from the summed counts, as in monthly.sql (not means of the monthly rates)
    sum(n_dusty_days) / nullif(sum(n_valid_days), 0) AS dusty_freq,
    sum(n_dusty_days_obs) / nullif(sum(n_obs_days), 0) AS dusty_freq_obs,
    ((sum(n_valid_days) - sum(n_dusty_days)) / nullif(sum(n_obs_days) - sum(n_dusty_days_obs), 0))
        / nullif(sum(n_dusty_days) / nullif(sum(n_dusty_days_obs), 0), 0) AS validity_ratio,
    -- coverage across the months: observed, valid, and with enough valid days
    count(*) FILTER (WHERE n_obs_days > 0) AS n_months_obs,
    count(*) FILTER (WHERE n_valid_days > 0) AS n_months_valid,
    count(*) FILTER (WHERE n_valid_days >= getvariable('min_valid_days_month')) AS n_months_covered,
    any_value(note) AS note  -- the period note of the year (as in monthly.sql)
FROM monthly
GROUP BY station_id, year
ORDER BY station_id, year  -- reproducible files
