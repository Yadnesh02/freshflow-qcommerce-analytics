{#
    Gate G2, stated as the plan states it: agg_store_sku_day revenue ties to
    raw order totals exactly.

    This is the end of a chain that has been checked at every hop - staging
    deduplicates to the raw feed, fct_order_item ties to it on count and value,
    and this asserts the aggregate loses nothing on the way through a
    four-source union and a group-by. Checking only the last hop would let an
    error upstream cancel one downstream; checking against the source parquet
    means the whole chain has to be right at once.

    Units are asserted alongside rupees. A group-by that dropped rows and a
    join that duplicated them can offset in value while both being visibly
    wrong in volume, and the two failures need different fixes.

    Exact means exact. The tolerance is a hundredth of a rupee, which exists
    because these are floating-point sums of four million terms and not because
    the numbers are allowed to disagree.
#}

with raw_sales as (

    select
        sum(qty) as units,
        sum(qty * unit_realized_price) as net_revenue
    from (
        select distinct
            order_id,
            sku_id,
            batch_id,
            qty,
            unit_base_price,
            unit_realized_price,
            discount_amt,
            unit_cogs,
            dte_at_sale,
            promo_id,
            is_substitution
        from {{ source('pos', 'pos_order_items') }}
    )

),

raw_returns as (

    select
        coalesce(sum(-returns.qty), 0) as units,
        coalesce(sum(-returns.qty * items.unit_realized_price), 0) as net_revenue
    from {{ source('pos', 'pos_returns') }} as returns
    inner join (
        select distinct
            order_id,
            sku_id,
            batch_id,
            unit_realized_price
        from {{ source('pos', 'pos_order_items') }}
        where qty > 0
    ) as items
        on
            returns.order_id = items.order_id
            and returns.sku_id = items.sku_id
            and returns.batch_id = items.batch_id

),

expected as (

    select
        raw_sales.units + raw_returns.units as units,
        raw_sales.net_revenue + raw_returns.net_revenue as net_revenue
    from raw_sales
    cross join raw_returns

),

actual as (

    select
        sum(units_sold) as units,
        sum(net_revenue) as net_revenue
    from {{ ref('agg_store_sku_day') }}

)

select
    actual.units as actual_units,
    expected.units as expected_units,
    actual.net_revenue as actual_revenue,
    expected.net_revenue as expected_revenue
from actual
cross join expected
where
    actual.units <> expected.units
    or abs(actual.net_revenue - expected.net_revenue) > 0.01
