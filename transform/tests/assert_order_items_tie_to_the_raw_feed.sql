{#
    The S2.4 acceptance gate, and the rehearsal for G2.

    `fct_order_item` must tie to the raw feed on both count and value. Not
    approximately, and not on one of the two: a model can hold the right number
    of rows and the wrong money if a join fanned out and a filter cut it back,
    or hold the right money and the wrong rows if a return landed twice with
    opposite signs.

    "Raw" means the deduplicated feed - the duplicates of defect 1 are damage,
    not revenue, and the tie is to what the source actually recorded once. Both
    return encodings are included, because a return is a line the source wrote
    and dropping it would tie to a number nobody asked for.

    Expressed against the source parquet rather than against staging, so this
    checks the whole chain from Bronze to the fact and not just the last hop.
#}

with raw_lines as (

    select
        count(*) as row_count,
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

    -- the separate returns feed carries no price, so it is valued from the
    -- sale line it reverses - the same way stg_pos__order_lines values it
    select
        count(*) as row_count,
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
        raw_lines.row_count + raw_returns.row_count as row_count,
        raw_lines.net_revenue + raw_returns.net_revenue as net_revenue
    from raw_lines
    cross join raw_returns

),

actual as (

    select
        count(*) as row_count,
        sum(net_revenue) as net_revenue
    from {{ ref('fct_order_item') }}

)

select
    actual.row_count as actual_rows,
    expected.row_count as expected_rows,
    actual.net_revenue as actual_revenue,
    expected.net_revenue as expected_revenue
from actual
cross join expected
where
    actual.row_count <> expected.row_count
    or abs(actual.net_revenue - expected.net_revenue) > 0.01
