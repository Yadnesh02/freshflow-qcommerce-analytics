{#
    Batch receipts - the grain the whole project is built on.

    A batch is a quantity of one SKU, in one store, with one expiry date. Every
    expiry, freshness and markdown question in this project is answerable only
    because sales carry the batch they consumed, and unanswerable the moment
    inventory is modelled at store-SKU.

    `po_id` is cast to varchar deliberately. Opening-balance batches predate
    any purchase order and carry a null, so a partition made entirely of those
    infers a null column type while the rest of the year infers a string - and
    the union across partitions then resolves to whichever schema was read
    first. Casting pins it, and stops a silent type coercion breaking the join
    to stg_wms__purchase_orders.

    `usable_days` is shelf life as received, not as printed: the supplier
    scorecard compares suppliers on this rather than on cost, which is where
    the SUP-DAIRY-B finding comes from.
#}

select
    batch_id,
    sku_id,
    store_id,
    supplier_id,
    cast(po_id as varchar) as po_id,
    qty_received,
    unit_landed_cost,
    qty_received * unit_landed_cost as landed_cost_value,
    usable_days,
    date_diff('day', received_date, expiry_date) as shelf_life_remaining_days,
    mfg_date,
    received_date,
    expiry_date,
    po_id is null as is_opening_balance,
    dt as arrival_date
from {{ source('wms', 'wms_inventory_batch') }}
