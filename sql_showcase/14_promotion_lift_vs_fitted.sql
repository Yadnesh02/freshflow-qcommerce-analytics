-- question: How much more do we sell on discounted days - and how much of that
--           apparent lift survives once you ask whether the discount was
--           allocated at random?
-- technique: conditional aggregation to build a naive before/after contrast,
--            joined to the fitted coefficient that corrects it
-- needs_days: 60
-- needs_tables: marts.mart_price_elasticity
--
-- **This query exists to be wrong in an instructive way.** The naive lift is
-- what a promo readout usually reports: mean units on discounted days over mean
-- units on undiscounted days, for the same store and SKU. It will look
-- impressive. It is also not a price effect, because discounts here are not
-- allocated at random - they land on stock that is close to expiry and on SKUs
-- that are already moving, so the comparison contains the reason the discount
-- was applied as well as its effect.
--
-- `mart_price_elasticity` carries the fitted coefficient for the same category
-- and freshness band, along with `is_identified` - whether the interval
-- excludes zero. Setting the two side by side is the point: the naive lift is
-- large and positive nearly everywhere, and the fitted elasticity fails to
-- clear identification in a majority of cells. Where a cell is not identified,
-- the naive number is not a smaller version of the truth, it is a different
-- quantity.
--
-- The `dte_band` join is what makes the two comparable at all - elasticity is
-- fitted per category and days-to-expiry band, so aggregating the naive lift to
-- category alone would compare it against a coefficient it does not correspond
-- to.

with daily as (

    select
        agg.store_id,
        agg.sku_id,
        agg.date_day,
        products.l1_category,
        bands.dte_band,
        agg.units_sold,
        agg.base_price_avg,
        agg.realized_price_avg,
        -- a day counts as discounted when the realised price actually sat below
        -- the base price, not when a promotion merely existed
        agg.realized_price_avg < agg.base_price_avg * 0.98 as is_discounted
    from marts.agg_store_sku_day as agg
    inner join marts.dim_product as products on agg.sku_id = products.sku_id
    -- half-open, because the bands share their endpoints: 0-1d and 1-2d both
    -- contain 1, so `between` would match a one-day-old batch to two bands and
    -- silently double every count in this query
    inner join marts.dim_dte_band as bands
        on
            agg.min_dte_at_sale >= bands.min_days
            and agg.min_dte_at_sale < bands.max_days
    where agg.units_sold > 0 and agg.base_price_avg > 0

),

naive as (

    select
        l1_category,
        dte_band,
        count(*) filter (where is_discounted) as discounted_days,
        count(*) filter (where not is_discounted) as regular_days,
        avg(units_sold) filter (where is_discounted) as units_discounted,
        avg(units_sold) filter (where not is_discounted) as units_regular,
        avg(
            realized_price_avg / nullif(base_price_avg, 0)
        ) filter (where is_discounted) as price_ratio_discounted
    from daily
    group by l1_category, dte_band
    having
        count(*) filter (where is_discounted) >= 30
        and count(*) filter (where not is_discounted) >= 30

)

select
    naive.l1_category,
    naive.dte_band,
    naive.discounted_days,
    naive.regular_days,
    round(naive.units_regular, 2) as units_regular,
    round(naive.units_discounted, 2) as units_discounted,
    -- what a promo readout would report
    round(
        100 * (naive.units_discounted - naive.units_regular) / nullif(naive.units_regular, 0), 1
    ) as naive_lift_pct,
    round(naive.price_ratio_discounted, 3) as avg_price_ratio,
    -- what the fitted model says, and whether it is even identified
    round(elasticity.elasticity, 4) as fitted_elasticity,
    elasticity.is_identified,
    elasticity.elasticity_basis,
    elasticity.observations
from naive
left join marts.mart_price_elasticity as elasticity
    on
        naive.l1_category = elasticity.l1_category
        and naive.dte_band = elasticity.dte_band
order by naive_lift_pct desc;
