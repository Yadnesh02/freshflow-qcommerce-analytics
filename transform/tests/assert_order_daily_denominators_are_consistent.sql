{#
    The aov denominator excludes orders that were served nothing, so the two
    counts have to stay in a fixed relationship or the exclusion has drifted.

    `placed_orders` is every header. `orders_count` is those with a fulfilled
    line. `unfulfilled_orders` is the difference, and there is no fourth state -
    an order either got something or it did not. Asserting the identity rather
    than each count separately is what catches a filter changing under one of
    them: three columns that each look plausible alone can still stop adding up.

    77,273 unfulfilled headers across the estate is 5.0% of orders, and the gap
    between the two denominators is a similar share of aov. That is large enough
    that a silent change of mind about which one aov divides by would move a
    headline tile without moving anything a test currently watches.
#}

select
    store_id,
    date_day,
    placed_orders,
    orders_count,
    unfulfilled_orders
from {{ ref('mart_order_daily') }}
where
    orders_count + unfulfilled_orders <> placed_orders
    or orders_count > placed_orders
    or unfulfilled_orders < 0
