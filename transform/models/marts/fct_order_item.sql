{#
    The keystone fact. One row per order line, carrying the FEFO-allocated
    batch it was served from.

    **Why batch_id on the sale is the decision this whole project rests on.**
    Without it a sale is attributable to a SKU and a store and nothing else -
    so "how much did we sell on the last day of shelf life" and "which supplier
    is behind this write-off" are both unanswerable, and the expiry, freshness
    and markdown analysis has nowhere to stand. Model inventory at store-SKU
    and the problem averages away entirely.

    **The grain is one signed line movement**: (order_id, sku_id, batch_id,
    line_type, source_feed). A line split across two batches by FEFO is two
    real rows with different expiry dates, and a return is a third row with a
    negative quantity - so `sum(signed_qty)` is net units and
    `sum(net_revenue)` is net revenue, with no CASE statement anywhere
    downstream. That convention is set in stg_pos__order_lines and this model
    inherits it rather than reinventing it.

    **Per-unit prices are extended here**, because this is the first layer
    entitled to. `discount_amt` and `unit_cogs` are per unit in the feed;
    multiplying by the signed quantity is the step that makes them additive,
    and doing it once here is what stops five marts each deciding whether the
    discount was per line or per unit.

    **store_id and date_day are denormalised from the header on purpose.**
    Nearly every query slices by store and day, and forcing each one through a
    1.5M-row join to fct_order to discover which store a sale happened in is a
    cost paid on every read to save space once. The order header stays the
    source of truth and a test asserts the two never disagree.

    Row count and revenue both tie to the raw feed exactly - that is the S2.4
    acceptance gate, and assert_order_items_tie_to_the_raw_feed states it.
#}

with lines as (

    select * from {{ ref('stg_pos__order_lines') }}

),

orders as (

    select
        order_id,
        store_id,
        customer_id,
        order_date_ist,
        order_ts_ist,
        arrival_date as order_arrival_date,
        arrival_lag_days
    from {{ ref('stg_pos__orders') }}

)

select
    -- surrogate key over the natural grain, so the fact has a single column to
    -- test for uniqueness and for a later incremental to merge on
    {{ row_hash([
        'lines.order_id',
        'lines.sku_id',
        'lines.batch_id',
        'lines.line_type',
        'lines.source_feed',
    ]) }} as order_item_key,

    lines.order_id,
    lines.sku_id,
    lines.batch_id,
    lines.promo_id,

    -- denormalised from the header; a test asserts they never disagree
    orders.store_id,
    orders.order_date_ist as date_day,
    orders.customer_id,
    orders.order_ts_ist,

    lines.line_type,
    lines.source_feed,
    lines.signed_qty,
    lines.units,

    lines.unit_base_price,
    lines.unit_realized_price,
    lines.discount_amt,
    lines.unit_cogs,

    -- extended once, here, so nothing downstream has to decide whether the
    -- per-unit columns were per unit
    lines.signed_qty * lines.unit_realized_price as net_revenue,
    lines.signed_qty * lines.unit_base_price as gross_revenue,
    lines.signed_qty * lines.discount_amt as discount_value,
    lines.signed_qty * lines.unit_cogs as cogs,
    lines.signed_qty * (lines.unit_realized_price - lines.unit_cogs) as gross_margin,

    lines.dte_at_sale,
    lines.is_substitution,
    lines.is_promoted,
    lines.return_reason,
    lines.return_date,

    -- Two arrival dates, and the difference matters. `arrival_date` is the
    -- partition this line landed in; `order_arrival_date` is when its header
    -- did. Defect 2 moves headers to a later partition and leaves the lines
    -- where they were, so the two disagree on 62,900 rows.
    --
    -- `order_arrival_date` is the one that means "when did this line become
    -- usable", because a line without its header has no store and no date and
    -- this model inner-joins on exactly that. Anything replaying history or
    -- filtering on what had arrived by a point in time has to use it - filter
    -- on the line's own partition and every late order looks like it was
    -- always there.
    lines.arrival_date,
    orders.order_arrival_date,
    orders.arrival_lag_days as order_arrival_lag_days
from lines
inner join orders on lines.order_id = orders.order_id
