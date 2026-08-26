{#
    The stock ledger: every event that moved a unit into or out of a batch.

    **Movements, not snapshots.** On-hand is derived by a running sum over this
    table, so every balance in the warehouse is auditable back to the events
    that produced it, and the reconciliation test in S2.7 can assert the
    derived balance equals the ledger exactly. A nightly on-hand snapshot would
    be smaller and would make that assertion impossible.

    **`movement_seq` is the replay order, and it is not decoration.** The feed
    is daily-grained - it records the date an event happened, not the time - so
    ordering by `event_date` leaves every intra-day sequence undefined, and a
    running balance over an undefined order is arbitrary rather than wrong in a
    way anyone would notice. Every window function over this table orders by
    `movement_seq`. The ERD said `event_ts`; the feed has no time component, so
    the ERD is corrected rather than a midnight timestamp invented.

    `running_balance` is computed here rather than in each consumer because it
    is the one column everything downstream wants and the one most likely to be
    computed with a subtly different frame. Ordered by movement_seq, framed to
    the current row.

    Rows with no batch reference are not here - they are in stg_quarantine,
    counted, with the quantity they removed from the balance recorded so the
    reconciliation can bound its own gap.
#}

with movements as (

    select * from {{ ref('stg_wms__inventory_movements') }}

),

batches as (

    select
        batch_id,
        sku_id,
        store_id,
        supplier_id,
        expiry_date,
        unit_landed_cost
    from {{ ref('stg_wms__inventory_batches') }}

)

select
    movements.movement_seq,
    movements.batch_id,

    -- denormalised so the ledger can be sliced by store, SKU or supplier
    -- without a 650k-row join on every read
    batches.sku_id,
    batches.store_id,
    batches.supplier_id,

    movements.event_type,
    movements.qty_delta,
    movements.units,
    movements.direction,
    movements.qty_delta * batches.unit_landed_cost as movement_cost_value,

    movements.event_date as date_day,
    batches.expiry_date,
    date_diff('day', movements.event_date, batches.expiry_date) as days_to_expiry,

    sum(movements.qty_delta) over (
        partition by movements.batch_id
        order by movements.movement_seq
        rows between unbounded preceding and current row
    ) as running_balance,

    movements.arrival_date
from movements
inner join batches on movements.batch_id = batches.batch_id
