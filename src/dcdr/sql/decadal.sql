-- Decadal station statistics: decadal, month-wise and season-wise counts and means.
-- Definitions: docs/rules.md section 11. Built from the gold `monthly` table of the same run.
-- Inputs: view `monthly`.
--
-- Output: one row per station, decade and period with at least one monthly row (gold table
-- `decadal`). Decades start in a year ending in 1 (1991-2000, 2001-2010, ...), like the WMO
-- normal periods. `period` is `annual` (the decadal mean), `01`..`12` (the same month over the
-- decade's years) or `DJF`, `MAM`, `JJA`, `SON` (the same season; DJF pools December, January
-- and February of the decade's years). No visibility (user decision 2026-10-31).
--
-- Mean per year (user decision 2026-10-31, "decadal means, month-wise means, season-wise means"):
-- dusty_days_mean = dusty_freq * n_calendar_days / 10, the frequency on valid days times the
-- period's mean length. It equals the decade's dusty days / 10 when every day is valid, and does
-- not count missing days as dust-free (also in the partial decade 2021-2030).
--
-- How the query is built (CTEs):
--   periods   each month mapped to its three periods: annual, its season, itself
--   m         monthly rows with their decade, one copy per period
--   calendar  days of each period over the decade's 10 calendar years (full length)
--   agg       counts summed per station, decade and period; years with data
--   final SELECT  agg joined to calendar, plus the mean
WITH periods AS (
    SELECT
        month,
        unnest(['annual', season, lpad(CAST(month AS VARCHAR), 2, '0')]) AS period
    FROM (
        SELECT
            CAST(range AS INTEGER) AS month,
            CASE
                WHEN range IN (12, 1, 2) THEN 'DJF'
                WHEN range IN (3, 4, 5) THEN 'MAM'
                WHEN range IN (6, 7, 8) THEN 'JJA'
                ELSE 'SON'
            END AS season
        FROM range(1, 13)
    )
),
m AS (
    SELECT monthly.*, year - (year - 1) % 10 AS decade, p.period
    FROM monthly
    JOIN periods p USING (month)
),
calendar AS (
    SELECT
        d.decade,
        p.period,
        CAST(sum(day(last_day(make_date(d.decade + y.i, p.month, 1)))) AS BIGINT)
            AS n_calendar_days
    FROM (SELECT DISTINCT decade FROM m) d
    CROSS JOIN range(10) y(i)
    JOIN periods p ON true
    GROUP BY d.decade, p.period
),
agg AS (
    SELECT
        station_id,
        decade,
        period,
        -- every additive monthly count, as in yearly.sql (n_calendar_days recomputed, no
        -- visibility counts); DuckDB sums BIGINT into HUGEINT, so cast back
        CAST(sum(COLUMNS(c -> starts_with(c, 'n_') AND c NOT IN (
            'n_calendar_days', 'n_days_vis', 'n_days_vis_dusty', 'n_slots_vis_zero'
        ))) AS BIGINT),
        sum(n_dusty_days) / nullif(sum(n_valid_days), 0) AS dusty_freq,
        -- years of the decade with such a month in the period
        count(DISTINCT year) FILTER (WHERE n_obs_days > 0) AS n_years_obs,
        count(DISTINCT year) FILTER (WHERE n_valid_days > 0) AS n_years_valid,
        count(DISTINCT year) FILTER (WHERE n_dusty_days > 0) AS n_dust_years,
        count(DISTINCT year) FILTER (WHERE n_dusty_days_obs > 0) AS n_dust_years_obs
    FROM m
    GROUP BY station_id, decade, period
)
SELECT
    a.station_id,
    a.decade,
    a.period,
    c.n_calendar_days,
    a.* EXCLUDE (station_id, decade, period),
    -- mean dusty days per year in the period; null without valid days
    a.dusty_freq * c.n_calendar_days / 10 AS dusty_days_mean
FROM agg a
JOIN calendar c USING (decade, period)
ORDER BY a.station_id, a.decade, a.period  -- reproducible files
