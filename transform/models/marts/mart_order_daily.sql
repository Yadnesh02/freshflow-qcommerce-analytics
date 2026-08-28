{#
    Orders per store per day, with the basket rolled up from the header (S4.x
    carry-over). This exists so `aov` has a source at the grain it is defined
    at, and for no other reason.

    **The metric registry has pointed at this table since Sprint 2 and the
    table did not exist**, so `aov` was the one executive metric the resolver
    gate never ran: `test_every_metric_compiles_and_executes` skips a metric
    whose source is missing, on the assumption it belongs to a later sprint.
    No later sprint owned it. A skip that never expires is indistinguishable
    from a pass, which is how a headline tile stayed unverified for two
    sprints.

    **Why not repoint `aov` at `agg_store_sku_day`.** That table has an
    `order_count`, and it is not the number of orders - it counts the orders
    touching each SKU, so a basket of six SKUs is counted six times. Summed
    over the estate it gives 4,247,015 against 1,560,619 real orders, and AOV
    computed from it comes out at Rs 102 instead of Rs 278: a 2.7x error that
    reads as a plausible figure. The order grain has to come from the order
    grain, which is the point the registry's own note was making.

    **Revenue ties by construction, not by coincidence.** `fct_order.gmv` is
    `sum(net_revenue)` over the lines, the same sum `agg_store_sku_day` takes
    over the same lines, so both roll up to Rs 43.4305 Cr exactly.
    `test_order_daily_ties_to_the_sku_aggregate` asserts it to the paisa,
    because two tables in the executive family disagreeing about revenue is
    the failure that makes a dashboard unusable.

    **76,121 orders were served nothing at all, and they decide what AOV
    means.** 4.9% of headers carry a basket the customer built and zero
    fulfilled lines - `requested_units` above zero, `gross_units` at zero, no
    returns, no revenue. That is a total stockout at pick time, the extreme of
    the short-fill this dataset is full of, and it is a real event rather than
    a broken row.

    They are excluded from `orders_count`, because the registry defines aov as
    revenue per *delivered* order and nothing was delivered. The choice is worth
    a paragraph because it moves the tile: including them gives Rs 278.29,
    excluding them Rs 292.56, a 5.1% gap on a headline number. Dividing revenue
    by a denominator that counts baskets nobody received would not be
    conservative, it would be measuring a different thing quietly.

    The count is kept as `unfulfilled_orders`, and every header is still
    counted in `placed_orders`, so the two denominators are both on the table
    and neither can be reconstructed only by guessing. An availability
    collapse now shows up as `unfulfilled_orders` rising rather than as AOV
    drifting for no visible reason.
#}

with orders as (

    select * from {{ ref('fct_order') }}

)

select
    {{ row_hash(['store_id', 'date_day']) }} as store_day_key,
    store_id,
    date_day,

    -- delivered orders: the aov denominator, and the reason it is not count(*)
    count(*) filter (where line_count > 0) as orders_count,
    count(*) as placed_orders,
    count(*) filter (where line_count = 0) as unfulfilled_orders,
    count(distinct customer_id) as customers_count,

    -- gmv is already net of returns and discounts; see fct_order
    sum(gmv) as net_revenue,
    sum(discount_total) as discount_total,
    sum(cogs) as cogs,
    sum(gross_margin) as gross_margin,

    sum(net_units) as net_units,
    sum(gross_units) as gross_units,
    sum(returned_units) as returned_units,
    sum(distinct_skus) as basket_skus,

    -- service quality at the order grain, which is where a customer feels it
    count(*) filter (where is_late) as late_orders,
    count(*) filter (where is_short_filled) as short_filled_orders,
    sum(requested_units) as requested_units,
    sum(unfulfilled_units) as unfulfilled_units,

    count(*) filter (where has_promotion) as promoted_orders,
    count(*) filter (where has_return) as orders_with_return
from orders
group by store_id, date_day
