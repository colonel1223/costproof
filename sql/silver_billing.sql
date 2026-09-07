-- ============================================================================
-- SILVER LAYER: clean, conformed billing rows
-- ============================================================================
--
-- Reads : data/bronze/focus_billing.parquet   (raw FOCUS 1.2, never modified)
-- Writes: data/silver/billing.parquet         (analysis-ready)
--
-- Bronze is immutable. Every transformation lives here, so if this logic is
-- wrong we fix it and re-run -- we never have to recover a corrupted source.
--
-- What this layer does:
--   1. Lifts ownership tags out of the nested Tags map into real columns
--   2. Makes untagged spend EXPLICIT rather than NULL
--   3. Adds calendar columns for time-based analysis
--   4. Derives the discount and commitment measures the economics layer needs
-- ============================================================================

COPY (

    SELECT
        -- ---------------------------------------------------------------
        -- Identity
        -- ---------------------------------------------------------------
        ResourceId                                      AS resource_id,
        ResourceName                                    AS resource_name,
        ResourceType                                    AS resource_type,
        ServiceName                                     AS service_name,
        ServiceCategory                                 AS service_category,
        ProviderName                                    AS provider,
        RegionId                                        AS region_id,

        -- ---------------------------------------------------------------
        -- Calendar
        --
        -- Cloud spend has strong weekly seasonality: batch and analytics
        -- workloads collapse at weekends, consumer traffic rises. Any
        -- comparison of "this week vs last week" that ignores day of week
        -- is measuring the calendar, not the workload.
        -- ---------------------------------------------------------------
        CAST(ChargePeriodStart AS DATE)                 AS charge_date,
        YEAR(ChargePeriodStart)                         AS charge_year,
        MONTH(ChargePeriodStart)                        AS charge_month,
        DAYOFWEEK(ChargePeriodStart)                    AS day_of_week,
        DAYOFWEEK(ChargePeriodStart) IN (0, 6)          AS is_weekend,

        -- ---------------------------------------------------------------
        -- Ownership
        --
        -- COALESCE replaces a missing value with a fallback. We use it
        -- instead of leaving NULL for a specific reason: NULL is contagious.
        -- It silently disappears from JOINs, fails equality comparisons
        -- (NULL = NULL is not true in SQL), and gets quietly dropped by
        -- filters. Untagged spend would vanish from reports rather than
        -- showing up as a problem -- which is precisely how $1.68M goes
        -- unnoticed. Naming it 'unallocated' forces it to appear in every
        -- report as its own line.
        --
        -- is_tagged is kept alongside so we never lose the distinction
        -- between what was KNOWN and what we filled in.
        -- ---------------------------------------------------------------
        COALESCE(Tags['business_unit'], 'unallocated')   AS business_unit,
        COALESCE(Tags['cost_centre'],   'unallocated')   AS cost_centre,
        COALESCE(Tags['environment'],   'unknown')       AS environment,
        Tags['business_unit'] IS NOT NULL                AS is_tagged,

        -- ---------------------------------------------------------------
        -- Charge classification
        -- ---------------------------------------------------------------
        ChargeCategory                                  AS charge_category,
        ChargeDescription                               AS charge_description,
        PricingCategory                                 AS pricing_category,

        -- ---------------------------------------------------------------
        -- Cost measures
        --
        -- Four costs, four meanings. EffectiveCost is the one to use for
        -- anything economic: it spreads prepayments across the days they
        -- cover. BilledCost shows $0 on committed usage because the money
        -- left months earlier, which makes prepaid resources look free.
        -- ---------------------------------------------------------------
        ListCost                                        AS list_cost,
        ContractedCost                                  AS contracted_cost,
        EffectiveCost                                   AS effective_cost,
        BilledCost                                      AS billed_cost,

        -- The three savings measures below are only meaningful on USAGE rows.
        --
        -- A Purchase row records money leaving to buy a commitment. It has no
        -- resource, no tags, and its ContractedCost is the whole prepayment
        -- while its EffectiveCost is zero (the value is amortised onto the
        -- usage rows that draw against it). Subtracting one from the other
        -- produces a number that looks like a saving and is nothing of the
        -- kind. Left unguarded it reported $2.58M of phantom commitment
        -- savings against 'unallocated' -- ten times any real business unit,
        -- which is how the error was caught.
        --
        -- Returning NULL rather than 0 matters: SUM() skips NULLs, so these
        -- rows drop out of savings totals instead of contributing a false zero
        -- that would drag every average down.

        -- Negotiated saving vs public list price.
        CASE WHEN ChargeCategory = 'Usage'
             THEN ListCost - ContractedCost
        END                                             AS negotiated_saving,

        -- Additional saving from committing to 1-3 year capacity.
        CASE WHEN ChargeCategory = 'Usage'
             THEN ContractedCost - EffectiveCost
        END                                             AS commitment_saving,

        -- Effective Savings Rate: the share of list price actually avoided.
        -- The headline KPI FinOps teams report to finance. Guarded against
        -- divide-by-zero, which would otherwise poison every downstream
        -- average with NaN.
        CASE WHEN ChargeCategory = 'Usage' AND ListCost > 0
             THEN (ListCost - EffectiveCost) / ListCost
        END                                             AS effective_savings_rate,

        -- ---------------------------------------------------------------
        -- Consumption
        -- ---------------------------------------------------------------
        ConsumedQuantity                                AS consumed_quantity,
        ConsumedUnit                                    AS consumed_unit,
        ListUnitPrice                                   AS list_unit_price,

        -- ---------------------------------------------------------------
        -- Commitment coverage
        --
        -- 'Unused' commitment is capacity that was paid for and never
        -- consumed. It is invisible to any rightsizing exercise because it
        -- is attached to no resource -- there is nothing to resize. It only
        -- shows up if you look for it, which is why FOCUS gives it a field.
        -- ---------------------------------------------------------------
        CommitmentDiscountId                            AS commitment_id,
        CommitmentDiscountStatus                        AS commitment_status,
        COALESCE(CommitmentDiscountStatus = 'Unused', FALSE) AS is_unused_commitment

    FROM 'data/bronze/focus_billing.parquet'

    -- Charge periods must be well-formed. A malformed row here would
    -- silently corrupt every daily aggregate downstream, so we exclude it
    -- rather than trusting the source.
    WHERE ChargePeriodEnd > ChargePeriodStart

) TO 'data/silver/billing.parquet' (FORMAT PARQUET, OVERWRITE TRUE);
