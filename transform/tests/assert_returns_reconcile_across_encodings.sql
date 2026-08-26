{#
    Defect 5. The two encodings of a return have to add up to the one signed
    model, in rupees, from both directions.

    The left side is computed from the two staged feeds independently: gross
    sales, minus the returns the POS reversed in place, minus the returns the
    separate system logged - those last valued by joining back to the sale line
    they reverse, because the returns feed carries no prices. The right side is
    what stg_pos__order_lines actually produced.

    This is the reconciliation the defect log asks for, and it is the rehearsal
    for gate G2: if net revenue cannot be derived two ways and agree to the
    rupee here, it will not tie to raw order totals in agg_store_sku_day either.
#}

with gross_sales as (

    select coalesce(sum(qty * unit_realized_price), 0) as amount
    from {{ ref('stg_pos__order_items') }}
    where qty > 0

),

reversed_in_sales_feed as (

    -- encoding A: qty is already negative, so negate to get a positive value
    select coalesce(sum(-qty * unit_realized_price), 0) as amount
    from {{ ref('stg_pos__order_items') }}
    where qty < 0

),

reversed_in_returns_feed as (

    -- encoding B: priced from the sale line it reverses
    select coalesce(sum(returned.returned_qty * items.unit_realized_price), 0) as amount
    from {{ ref('stg_pos__returns') }} as returned
    inner join {{ ref('stg_pos__order_items') }} as items
        on
            returned.order_id = items.order_id
            and returned.sku_id = items.sku_id
            and returned.batch_id = items.batch_id
            and items.qty > 0

),

normalised as (

    select coalesce(sum(signed_qty * unit_realized_price), 0) as amount
    from {{ ref('stg_pos__order_lines') }}

)

select
    normalised.amount as normalised_net_revenue,
    gross_sales.amount
    - reversed_in_sales_feed.amount
    - reversed_in_returns_feed.amount as expected_net_revenue
from normalised, gross_sales, reversed_in_sales_feed, reversed_in_returns_feed
where
    abs(
        normalised.amount
        - (gross_sales.amount - reversed_in_sales_feed.amount - reversed_in_returns_feed.amount)
    ) > 0.01
