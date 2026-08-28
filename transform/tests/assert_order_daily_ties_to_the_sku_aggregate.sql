{#
    The two tables in the executive family that carry revenue have to agree.

    `agg_store_sku_day` is the store-SKU-day grain and ties to the raw feed
    exactly (gate G2). `mart_order_daily` is the store-day grain and is the
    source of `aov`. Both sum net revenue over the same order lines, so they
    are two roll-ups of one number and any gap between them is a join that
    duplicated or a group-by that dropped.

    This matters more than the usual tie-out because the two tables land next
    to each other on the executive page. A dashboard whose revenue tile and
    AOV tile imply different revenues is not a dashboard anybody uses twice.

    Orders are deliberately *not* compared: agg_store_sku_day.order_count
    counts orders per SKU and sums to 4,247,015 against 1,560,619 real orders.
    That is not a defect in either table, it is the reason aov cannot be
    sourced from the SKU grain, and asserting equality here would be asserting
    the bug.

    Tolerance is a hundredth of a rupee for the same reason as G2's - these are
    floating-point sums of millions of terms.
#}

with by_order as (

    select
        sum(net_revenue) as net_revenue,
        sum(cogs) as cogs,
        sum(gross_margin) as gross_margin
    from {{ ref('mart_order_daily') }}

),

by_sku as (

    select
        sum(net_revenue) as net_revenue,
        sum(cogs) as cogs,
        sum(gross_margin) as gross_margin
    from {{ ref('agg_store_sku_day') }}

)

select
    by_order.net_revenue as order_grain_revenue,
    by_sku.net_revenue as sku_grain_revenue,
    by_order.gross_margin as order_grain_margin,
    by_sku.gross_margin as sku_grain_margin
from by_order
cross join by_sku
where
    abs(by_order.net_revenue - by_sku.net_revenue) > 0.01
    or abs(by_order.cogs - by_sku.cogs) > 0.01
    or abs(by_order.gross_margin - by_sku.gross_margin) > 0.01
