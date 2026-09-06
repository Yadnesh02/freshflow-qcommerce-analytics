-- question: Where does demand leak away - between being browsed and being
--           ordered, or between being ordered and being served - and does the
--           answer differ by category?
-- technique: conditional aggregation to fold several stages into one row per
--            group, so the stages can be divided by each other
-- needs_days: 30
--
-- **The funnel stops at "served", because the feed has no cart.**
-- `fct_clickstream` carries `pdp_view` and `notify_me` and nothing between them
-- and an order, so a view -> cart -> checkout -> purchase funnel cannot be
-- built from this warehouse. What exists is browse -> ordered -> fulfilled, and
-- the second step is the one this business actually loses money on: a
-- short-filled order is demand that converted and then evaporated at the pick
-- face.
--
-- `notify_me` is the most interesting stage precisely because it only fires
-- when the shelf is empty - it is a customer telling you they wanted something
-- you did not have, which is the signal every other stage of this funnel is
-- censored by.
--
-- Conditional aggregation rather than four joins: one pass over each source,
-- with `filter` doing the branching. Joining four subqueries on store x sku x
-- day would produce the same numbers and fan out the moment one side is missing
-- a row.

with browse as (

    select
        store_id,
        sku_id,
        date_day,
        sum(event_count) filter (where event_type = 'pdp_view') as pdp_views,
        sum(event_count) filter (where event_type = 'notify_me') as notify_me_events,
        -- the event_type filter belongs here too. Without it this counts
        -- notify_me events in the numerator against a pdp_view denominator and
        -- reports 125% of views hitting an empty shelf - a share above 100 that
        -- reads as a modelling subtlety and is an unmatched filter.
        sum(event_count) filter (
            where event_type = 'pdp_view' and not was_in_stock
        ) as views_against_empty_shelf
    from marts.fct_clickstream
    group by store_id, sku_id, date_day

),

sales as (

    select
        store_id,
        sku_id,
        date_day,
        sum(order_count) as orders,
        sum(units_sold) as units_served,
        sum(units_demanded_imputed) as units_demanded,
        avg(in_stock_pct) as in_stock_pct
    from marts.agg_store_sku_day
    group by store_id, sku_id, date_day

)

select
    products.l1_category,
    sum(browse.pdp_views) as pdp_views,
    sum(browse.notify_me_events) as notify_me_events,
    sum(browse.views_against_empty_shelf) as views_against_empty_shelf,
    sum(sales.orders) as orders,
    sum(sales.units_served) as units_served,
    round(sum(sales.units_demanded), 0) as units_demanded_imputed,

    -- stage one: did a browse turn into an order line
    round(100.0 * sum(sales.orders) / nullif(sum(browse.pdp_views), 0), 1) as browse_to_order_pct,

    -- stage two: of what was wanted, how much was actually handed over
    round(
        100.0 * sum(sales.units_served) / nullif(sum(sales.units_demanded), 0), 1
    ) as demand_served_pct,

    -- the leak the store controls, as a share of everything browsed
    round(
        100.0 * sum(browse.views_against_empty_shelf) / nullif(sum(browse.pdp_views), 0), 1
    ) as browsed_an_empty_shelf_pct,
    round(100 * avg(sales.in_stock_pct), 1) as avg_in_stock_pct
from browse
inner join sales
    on
        browse.store_id = sales.store_id
        and browse.sku_id = sales.sku_id
        and browse.date_day = sales.date_day
inner join marts.dim_product as products on browse.sku_id = products.sku_id
group by products.l1_category
order by browsed_an_empty_shelf_pct desc;
