-- ============================================================================
-- GOLD LAYER: unit economics
-- ============================================================================
--
-- Reads : data/silver/billing.parquet              (cleaned billing)
--         data/gold/fact_business_driver.parquet   (business volumes)
-- Writes: data/gold/unit_economics.parquet
--
-- THE QUESTION THIS ANSWERS
--
-- "Our cloud bill went up 30%. Is that a problem?"
--
-- Unanswerable from the bill alone. A 30% rise on 50% more customers is a
-- WIN -- you got cheaper per customer. A 30% rise on flat volume is waste.
-- Same number on the invoice, opposite conclusions, opposite actions.
--
-- The missing piece is a DENOMINATOR: units of business output. Cost alone
-- is a numerator looking for one. Only 49% of organisations measure this
-- (Flexera 2026), which means the majority genuinely cannot tell growth from
-- waste in their own spending.
--
-- Formula:      unit cost = cost / business volume
--
-- Interpretation:
--   cost UP,   unit cost FLAT  -> growth. Not a problem.
--   cost UP,   unit cost UP    -> efficiency is degrading. Investigate.
--   cost FLAT, unit cost DOWN  -> you are scaling well. Report it.
-- ============================================================================

COPY (

    WITH daily_cost AS (
        -- Collapse billing to one row per business unit per day.
        --
        -- Usage rows only. Purchase rows are commitment prepayments with no
        -- resource and no owner; including them would attribute a lump-sum
        -- capacity purchase to whichever day it landed on and produce a spike
        -- that no workload caused.
        SELECT
            business_unit,
            charge_date,
            SUM(effective_cost)                                  AS cost,
            SUM(CASE WHEN is_tagged THEN effective_cost END)     AS tagged_cost,
            COUNT(DISTINCT resource_id)                          AS active_resources
        FROM 'data/silver/billing.parquet'
        WHERE charge_category = 'Usage'
        GROUP BY 1, 2
    ),

    joined AS (
        -- Attach the business driver volume for that unit and day.
        --
        -- INNER JOIN, deliberately. Two groups have no driver and are dropped:
        --   * 'unallocated'  -- untagged spend, no owner, so no denominator
        --   * 'bu-platform'  -- shared services consumed by everyone
        --
        -- Both are real spend and both matter. They are excluded HERE because
        -- a unit cost with no meaningful denominator is a fabricated number,
        -- and a fabricated number is worse than a missing one. Allocating
        -- shared and untagged spend across the units that consume it is its
        -- own problem with its own defensible methods; it does not belong
        -- hidden inside this calculation.
        SELECT
            c.business_unit,
            c.charge_date,
            c.cost,
            c.active_resources,
            d.driver,
            d.volume
        FROM daily_cost c
        INNER JOIN 'data/gold/fact_business_driver.parquet' d
               ON  d.business_unit = c.business_unit
               AND CAST(d.date AS DATE) = c.charge_date
    )

    SELECT
        business_unit,
        charge_date,
        driver,
        cost,
        volume,
        active_resources,

        -- The unit economic. Guarded against divide-by-zero: a single zero
        -- volume day would otherwise produce infinity and poison every
        -- average computed downstream.
        CASE WHEN volume > 0 THEN cost / volume END              AS cost_per_unit,

        -- Scaled to a readable magnitude. Cost per single transaction is
        -- $0.0000034, which nobody can compare at a glance; per thousand is
        -- how these get reported.
        CASE WHEN volume > 0 THEN 1000 * cost / volume END       AS cost_per_1k_units,

        -- 28-day trailing averages. Daily unit cost is far too noisy to read
        -- directly -- weekends alone swing it. A 28-day window covers exactly
        -- four weeks, so day-of-week effects cancel instead of leaking into
        -- the trend.
        AVG(cost) OVER w28                                       AS cost_28d_avg,
        AVG(CASE WHEN volume > 0 THEN cost / volume END) OVER w28
                                                                 AS unit_cost_28d_avg,

        -- Efficiency trend: today's unit cost against the trailing average.
        -- Above 1.0 means each unit of output is costing MORE than it recently
        -- did -- efficiency is degrading regardless of what the total bill says.
        -- This is the number that separates growth from waste.
        CASE
            WHEN volume > 0
             AND AVG(CASE WHEN volume > 0 THEN cost / volume END) OVER w28 > 0
            THEN (cost / volume) / AVG(CASE WHEN volume > 0 THEN cost / volume END) OVER w28
        END                                                      AS unit_cost_vs_trend

    FROM joined
    WINDOW w28 AS (
        PARTITION BY business_unit
        ORDER BY charge_date
        ROWS BETWEEN 27 PRECEDING AND CURRENT ROW
    )
    ORDER BY business_unit, charge_date

) TO 'data/gold/unit_economics.parquet' (FORMAT PARQUET, OVERWRITE TRUE);
