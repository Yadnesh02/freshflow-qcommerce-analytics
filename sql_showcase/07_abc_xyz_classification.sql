-- question: Which SKUs carry the revenue, which ones are predictable enough to
--           automate, and which combination deserves a human looking at it
--           every morning?
-- technique: cumulative share with a window frame for the ABC cut, coefficient
--            of variation for XYZ, recomputed rather than read from the
--            dimension so the two can be compared
-- needs_days: 60
--
-- ABC ranks by contribution; XYZ ranks by predictability. Neither is useful
-- alone - an A item nobody can forecast (AZ) and a C item that arrives like
-- clockwork (CX) need opposite treatment, and a single ranking cannot say that.
-- The pairing is what makes the classification actionable: AX automates, AZ
-- gets the safety stock and the attention, CZ is a delisting conversation.
--
-- **Recomputed here on purpose.** `dim_product` already carries `abc_class`,
-- `xyz_class` and `demand_cv`, and this query derives them again from the daily
-- aggregate so the two can be set side by side. A classification that the
-- warehouse and an ad-hoc query disagree about is worth knowing about before it
-- is used to decide what to stop stocking.
--
-- The 80/95 cut is a convention, not a measurement, and it is written where it
-- can be seen rather than buried in a case expression.

with daily as (

    select
        sku_id,
        date_day,
        sum(units_sold) as units,
        sum(net_revenue) as revenue
    from marts.agg_store_sku_day
    group by sku_id, date_day

),

per_sku as (

    select
        sku_id,
        sum(revenue) as revenue,
        avg(units) as mean_units,
        stddev_samp(units) as sd_units,
        count(*) as selling_days
    from daily
    group by sku_id
    having sum(revenue) > 0

),

ranked as (

    select
        per_sku.*,
        sum(revenue) over () as total_revenue,
        sum(revenue) over (
            order by revenue desc
            rows between unbounded preceding and current row
        ) as running_revenue
    from per_sku

),

classified as (

    select
        ranked.*,
        running_revenue / nullif(total_revenue, 0) as cumulative_share,
        case
            when running_revenue / nullif(total_revenue, 0) <= 0.80 then 'A'
            when running_revenue / nullif(total_revenue, 0) <= 0.95 then 'B'
            else 'C'
        end as abc_recomputed,
        -- coefficient of variation: dispersion relative to level, so a big
        -- steady seller is not punished for being big
        sd_units / nullif(mean_units, 0) as demand_cv
    from ranked

)

select
    classified.sku_id,
    products.sku_name,
    products.l1_category,
    round(classified.revenue, 0) as revenue,
    round(100 * classified.cumulative_share, 2) as cumulative_share_pct,
    classified.abc_recomputed,
    case
        when classified.demand_cv <= 0.5 then 'X'
        when classified.demand_cv <= 1.0 then 'Y'
        else 'Z'
    end as xyz_recomputed,
    round(classified.demand_cv, 3) as demand_cv,
    products.abc_xyz_class as class_in_dimension,
    classified.abc_recomputed
    || case
        when classified.demand_cv <= 0.5 then 'X'
        when classified.demand_cv <= 1.0 then 'Y'
        else 'Z'
    end
    <> products.abc_xyz_class as disagrees_with_dimension
from classified
inner join marts.dim_product as products on classified.sku_id = products.sku_id
order by classified.revenue desc
limit 50;
