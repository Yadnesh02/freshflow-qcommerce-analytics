{#
    One row per order. The header, with its basket rolled up from the lines.

    **Totals are computed from the lines, not carried from the feed** - so the
    header can never contradict the fact table beneath it.

    **`requested_units` is the second censored-demand signal in this dataset,
    and the better one.** The POS writes `n_units` when the basket is created,
    before FEFO allocation runs, so it records what the customer intended to
    buy rather than what the store managed to serve. The difference is demand
    that existed and was lost: 1.11M units, 20.6% of everything intended, with
    half of all orders short-filled.

    That makes it more useful than the clickstream for uncensoring. The
    clickstream says somebody looked at a SKU while it was out of stock -
    anonymous, and an impression is not an intention. This says a specific
    customer put a specific quantity in a specific basket and the store could
    not fill it. Sprint 3 should train on both and will trust this one more.

    It would have been easy to read `n_units` as a stale unit count and drop
    it, since it disagrees with the lines on half the orders. The disagreement
    is the signal.

    **`delivery_fee` is not here.** The ERD promised it and no feed carries
    one - the simulator never models a delivery charge. A column of zeroes
    would look like a business that does not charge for delivery rather than a
    warehouse that does not know, so the ERD is corrected instead.

    Orders with no surviving lines cannot occur - a G1 invariant asserts every
    line resolves to an order and the relationship holds in both directions -
    but the join is a left join anyway, because a fact table that silently
    drops order headers is the failure this project has already had once.
#}

with orders as (

    select * from {{ ref('stg_pos__orders') }}

),

basket as (

    select
        order_id,
        sum(signed_qty) as net_units,
        sum(units) filter (where line_type = 'sale') as gross_units,
        sum(units) filter (where line_type = 'return') as returned_units,
        count(*) filter (where line_type = 'sale') as line_count,
        count(distinct sku_id) as distinct_skus,
        count(distinct batch_id) as distinct_batches,

        sum(net_revenue) as gmv,
        sum(discount_value) as discount_total,
        sum(cogs) as cogs,
        sum(gross_margin) as gross_margin,

        count(*) filter (where is_promoted) > 0 as has_promotion,
        count(*) filter (where is_substitution) > 0 as has_substitution,
        count(*) filter (where line_type = 'return') > 0 as has_return,
        min(dte_at_sale) as min_dte_at_sale
    from {{ ref('fct_order_item') }}
    group by order_id

)

select
    orders.order_id,
    orders.store_id,
    orders.customer_id,
    orders.order_date_ist as date_day,

    orders.order_ts_ist,
    orders.promised_ts_ist,
    orders.delivered_ts_ist,
    orders.delivery_slack_minutes,
    orders.is_late,
    orders.payment_mode,

    coalesce(basket.net_units, 0) as net_units,
    coalesce(basket.gross_units, 0) as gross_units,
    coalesce(basket.returned_units, 0) as returned_units,
    coalesce(basket.line_count, 0) as line_count,
    coalesce(basket.distinct_skus, 0) as distinct_skus,
    coalesce(basket.distinct_batches, 0) as distinct_batches,

    coalesce(basket.gmv, 0) as gmv,
    coalesce(basket.discount_total, 0) as discount_total,
    coalesce(basket.cogs, 0) as cogs,
    coalesce(basket.gross_margin, 0) as gross_margin,

    coalesce(basket.has_promotion, false) as has_promotion,
    coalesce(basket.has_substitution, false) as has_substitution,
    coalesce(basket.has_return, false) as has_return,
    basket.min_dte_at_sale,

    -- what the customer intended to buy, recorded before allocation ran
    orders.n_units as requested_units,
    orders.n_units - coalesce(basket.gross_units, 0) as unfulfilled_units,
    coalesce(basket.gross_units, 0) / nullif(cast(orders.n_units as double), 0) as fill_rate,
    orders.n_units > coalesce(basket.gross_units, 0) as is_short_filled,

    orders.arrival_date,
    orders.arrival_lag_days
from orders
left join basket on orders.order_id = basket.order_id
