{#
    Defect 5, resolved: one signed movement per line, whichever system wrote it.

    Returns exist in two encodings because two systems wrote them. Some are
    negative quantities posted back into the sales feed; others are positive
    rows in a separate returns feed. The defect log is blunt about the
    consequence - "counting either alone gets net units wrong, and counting
    both naively double-counts" - so this model is the single place that
    decides what a line means, and everything downstream reads it instead of
    the two feeds.

    **The convention: `signed_qty` is positive for a sale and negative for a
    return, always.** Net units are then `sum(signed_qty)` and net revenue is
    `sum(signed_qty * unit_realized_price)` with no CASE statement anywhere
    downstream. Encoding A already carries its own sign; encoding B is negated
    here. That is the whole normalisation, and it is worth stating plainly
    because every reconciliation in Sprint 2 rests on it.

    **The returns feed carries no prices**, only a quantity - so it is joined
    back to the sale line it reverses to recover what the unit actually sold
    for. A return valued at today's price rather than the price paid would make
    net revenue drift every time a markdown ran, which on this dataset is daily.

    **The join is a left join on purpose.** An inner join would silently drop
    any return whose sale line cannot be found, which is the same class of
    silent loss defect 7 causes. Unmatched rows are excluded here and routed to
    stg_quarantine under `return_without_matching_sale`; a dbt test asserts
    that the two sides add back up to the raw feed, so nothing leaves without
    being counted somewhere.

    **Encoding A has no return date.** A return posted into the sales feed
    inherits the sale's identity and nothing else, so `return_date` is null for
    those rows and `arrival_date` is the only thing that dates them. Anything
    measuring return latency has to say which encoding it can see.
#}

with items as (

    select * from {{ ref('stg_pos__order_items') }}

),

returned as (

    select * from {{ ref('stg_pos__returns') }}

),

sales as (

    select
        order_id,
        sku_id,
        batch_id,
        promo_id,
        qty as signed_qty,
        unit_base_price,
        unit_realized_price,
        discount_amt,
        unit_cogs,
        dte_at_sale,
        is_substitution,
        is_promoted,
        arrival_date
    from items
    where qty > 0

),

returned_into_sales_feed as (

    -- encoding A: the POS reversed the sale in place
    select
        order_id,
        sku_id,
        batch_id,
        promo_id,
        qty as signed_qty,
        unit_base_price,
        unit_realized_price,
        discount_amt,
        unit_cogs,
        dte_at_sale,
        is_substitution,
        is_promoted,
        cast(null as varchar) as return_reason,
        cast(null as date) as return_date,
        arrival_date
    from items
    where qty < 0

),

returned_into_returns_feed as (

    -- encoding B: a separate system logged it, positive, with no prices
    select
        returned.order_id,
        returned.sku_id,
        returned.batch_id,
        sales.promo_id,
        -returned.returned_qty as signed_qty,
        sales.unit_base_price,
        sales.unit_realized_price,
        sales.discount_amt,
        sales.unit_cogs,
        sales.dte_at_sale,
        sales.is_substitution,
        sales.is_promoted,
        returned.return_reason,
        returned.return_date,
        returned.arrival_date
    from returned
    left join sales
        on
            returned.order_id = sales.order_id
            and returned.sku_id = sales.sku_id
            and returned.batch_id = sales.batch_id
    where sales.order_id is not null

),

unioned as (

    select
        *,
        'sale' as line_type,
        'pos_order_items' as source_feed,
        cast(null as varchar) as return_reason,
        cast(null as date) as return_date
    from sales

    union all by name

    select
        *,
        'return' as line_type,
        'pos_order_items' as source_feed
    from returned_into_sales_feed

    union all by name

    select
        *,
        'return' as line_type,
        'pos_returns' as source_feed
    from returned_into_returns_feed

)

select
    order_id,
    sku_id,
    batch_id,
    promo_id,
    line_type,
    source_feed,
    signed_qty,
    abs(signed_qty) as units,
    unit_base_price,
    unit_realized_price,
    discount_amt,
    unit_cogs,
    dte_at_sale,
    is_substitution,
    is_promoted,
    return_reason,
    return_date,
    arrival_date
from unioned
