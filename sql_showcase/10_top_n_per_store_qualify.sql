-- question: What are the three biggest revenue lines in each store, and does
--           every store really sell the same things - or does the estate have
--           genuinely different demand by locality?
-- technique: QUALIFY to filter on a window function without a wrapping
--            subquery, with a deterministic tie-break
-- needs_days: 30
--
-- QUALIFY is to window functions what HAVING is to aggregates: it filters on
-- something that can only be computed after the rows are grouped and ordered.
-- Without it this is a subquery whose only purpose is to expose `rn` so the
-- outer query can compare it to 3, which is a lot of scaffolding for one
-- predicate. DuckDB, Snowflake, BigQuery and Databricks all support it;
-- Postgres does not, and the subquery form is the portable fallback.
--
-- **The tie-break is not cosmetic.** `row_number` over a non-unique ordering
-- returns a different set of rows between runs, so a "top 3" that ties on
-- revenue is silently non-deterministic - a report that changes when nothing
-- changed. Adding `sku_id` to the ORDER BY makes it total.
--
-- The comparison against the estate-wide rank is what turns a top-N list into a
-- finding: a SKU ranked first in one store and fortieth overall is a local
-- assortment fact, not a bestseller.

with store_sku as (

    select
        agg.store_id,
        agg.sku_id,
        sum(agg.net_revenue) as revenue,
        sum(agg.units_sold) as units
    from marts.agg_store_sku_day as agg
    group by agg.store_id, agg.sku_id

),

estate_rank as (

    select
        sku_id,
        row_number() over (order by sum(revenue) desc, sku_id) as estate_rank
    from store_sku
    group by sku_id

)

select
    stores.store_name,
    stores.locality,
    stores.catchment_tier,
    store_sku.sku_id,
    products.sku_name,
    products.l1_category,
    round(store_sku.revenue, 0) as revenue,
    store_sku.units,
    row_number() over (
        partition by store_sku.store_id order by store_sku.revenue desc, store_sku.sku_id
    ) as rank_in_store,
    estate_rank.estate_rank,
    -- a local hero: top three here, outside the estate top twenty
    estate_rank.estate_rank > 20 as local_only
from store_sku
inner join marts.dim_store as stores on store_sku.store_id = stores.store_id
inner join marts.dim_product as products on store_sku.sku_id = products.sku_id
inner join estate_rank on store_sku.sku_id = estate_rank.sku_id
qualify
    row_number() over (
        partition by store_sku.store_id order by store_sku.revenue desc, store_sku.sku_id
    )
    <= 3
order by stores.store_name, rank_in_store;
