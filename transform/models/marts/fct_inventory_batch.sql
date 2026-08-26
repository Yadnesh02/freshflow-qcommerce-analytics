{#
    One row per physical batch received into a store.

    **Batch grain, not SKU grain** - the modelling decision the plan calls a
    keystone. Two deliveries of the same SKU on different days are different
    batches carrying different expiry dates and therefore different risk.
    Averaging them into a store-SKU on-hand figure destroys the only thing this
    project is trying to measure.

    Consumption is joined from the movement ledger rather than recomputed from
    sales, because the ledger is the auditable record: `qty_sold`,
    `qty_written_off` and `qty_remaining` all come from the same event stream a
    reconciliation test can replay. Batches whose movements were quarantined
    (defect 3) will not balance, and that is what `is_reconciled` records -
    the gap is bounded by stg_quarantine's impact_units and checked in S2.7.

    `supplier_id` resolves for every row, including the 15,557 opening-balance
    batches, because dim_supplier carries an explicit unknown member for the
    SUP-OPENING sentinel they use.
#}

with batches as (

    select * from {{ ref('stg_wms__inventory_batches') }}

),

ledger as (

    select
        batch_id,
        sum(qty_delta) as net_movement,
        sum(qty_delta) filter (where event_type in ('inbound', 'opening_balance')) as qty_in,
        -sum(qty_delta) filter (where event_type = 'sale') as qty_sold,
        -sum(qty_delta) filter (where event_type = 'expiry_writeoff') as qty_written_off,
        count(*) as movement_count,
        min(event_date) as first_movement_date,
        max(event_date) as last_movement_date
    from {{ ref('stg_wms__inventory_movements') }}
    group by batch_id

)

select
    batches.batch_id,
    batches.sku_id,
    batches.store_id,
    batches.supplier_id,
    batches.po_id,

    batches.mfg_date,
    batches.received_date,
    batches.received_date as date_day,
    batches.expiry_date,
    batches.usable_days,
    batches.shelf_life_remaining_days,

    batches.qty_received,
    batches.unit_landed_cost,
    batches.landed_cost_value,

    coalesce(ledger.qty_sold, 0) as qty_sold,
    coalesce(ledger.qty_written_off, 0) as qty_written_off,
    coalesce(ledger.net_movement, 0) as qty_remaining,
    coalesce(ledger.qty_written_off, 0) * batches.unit_landed_cost as writeoff_value,
    coalesce(ledger.qty_sold, 0) / nullif(cast(batches.qty_received as double), 0)
        as sell_through_rate,

    -- a batch whose inbound movement was quarantined cannot balance; recording
    -- it beats letting the reconciliation in S2.7 discover an unexplained gap
    coalesce(ledger.qty_in, 0) = batches.qty_received as is_reconciled,
    coalesce(ledger.movement_count, 0) as movement_count,
    ledger.first_movement_date,
    ledger.last_movement_date,

    batches.is_opening_balance,
    batches.arrival_date
from batches
left join ledger on batches.batch_id = ledger.batch_id
