{{ config(materialized='table') }}

{#
    Rows the staging layer refused, with the reason recorded.

    **The rule this table exists to enforce: nothing is dropped silently.** A
    staging model that filters bad rows out with a WHERE clause and says
    nothing produces a warehouse that reconciles beautifully to a number that
    is wrong. Every exclusion in staging has a matching insert here, and S2.7
    tests that the two sides add back up to the raw feed - so the pipeline can
    say not just "this is what we kept" but "this is what we did not, how much
    of it there was, and why".

    Materialised as a table rather than a view because it is a record, not a
    derivation: the data-quality mart reads it, the dashboard surfaces the
    count, and a view would recompute the exclusions on every read.

    `impact_units` is the quantity the exclusion removed from a balance. It is
    what makes the reconciliation test in S2.7 quantitative: the stock ledger
    is allowed not to balance, but only by exactly this much and no more. A
    residual larger than the quarantined quantity means something else is
    broken too, which is the failure a plain "does it reconcile?" check hides.

    `payload` keeps the row itself. The reason a rejected row is worth storing
    is that somebody eventually asks whether it was really unrecoverable, and
    the answer is only available if the row survived the question.
#}

with movements_without_a_batch as (

    -- defect 3: the WMS emits ~1% of movements with no batch reference
    select
        'wms_inventory_movement' as source_name,
        'missing_batch_reference' as reason_code,
        'Stock movement with no batch reference. It cannot be attributed to an '
        || 'expiry date or replayed into a batch balance, so it is held here '
        || 'rather than distorting one.' as reason,
        cast(movement_seq as varchar) as record_key,
        event_date,
        dt as arrival_date,
        cast(abs(qty_delta) as double) as impact_units,
        to_json({
            'movement_seq': movement_seq,
            'batch_id': batch_id,
            'event_type': event_type,
            'qty_delta': qty_delta,
            'event_date': event_date
        }) as payload
    from {{ source('wms', 'wms_inventory_movement') }}
    where batch_id is null

),

returns_without_a_sale as (

    -- defect 5, the failure mode: a return whose sale line cannot be found.
    -- Expected to be empty. It is queried anyway because stg_pos__order_lines
    -- excludes these rows, and an exclusion with no counterpart here is
    -- exactly the silent loss this table exists to prevent.
    select
        'pos_returns' as source_name,
        'return_without_matching_sale' as reason_code,
        'Return references an order line that does not exist in the sales '
        || 'feed, so the unit price it should be valued at is unknown.' as reason,
        returned.order_id || '|' || returned.sku_id || '|'
        || coalesce(returned.batch_id, '') as record_key,
        returned.return_date as event_date,
        returned.arrival_date,
        cast(returned.returned_qty as double) as impact_units,
        to_json({
            'order_id': returned.order_id,
            'sku_id': returned.sku_id,
            'batch_id': returned.batch_id,
            'returned_qty': returned.returned_qty,
            'return_reason': returned.return_reason,
            'return_date': returned.return_date
        }) as payload
    from {{ ref('stg_pos__returns') }} as returned
    left join {{ ref('stg_pos__order_items') }} as items
        on
            returned.order_id = items.order_id
            and returned.sku_id = items.sku_id
            and returned.batch_id = items.batch_id
            and items.qty > 0
    where items.order_id is null

)

select * from movements_without_a_batch

union all by name

select * from returns_without_a_sale
