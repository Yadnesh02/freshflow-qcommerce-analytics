-- question: Does the stored running balance for each batch agree with a
--           recomputation from the movement ledger, and which batches end the
--           window holding stock they never sold?
-- technique: running total over an ordered window frame, reconciled against a
--            materialised column rather than trusted
-- needs_days: 1
--
-- A running balance is the first thing anybody writes with a window function
-- and the last thing anybody checks. `fct_inventory_movement` already carries
-- one, so recomputing it is only worth doing if the recomputation is compared:
-- an ordering that differs by one row - ties on the same day broken differently
-- - produces a column that looks right on every spot check and is wrong in the
-- middle of every batch. The ledger is ordered by `movement_seq`, which is
-- unique, so the two must agree exactly and any row where they do not is a
-- defect rather than a rounding difference.

with ledger as (

    select
        batch_id,
        store_id,
        sku_id,
        movement_seq,
        date_day,
        event_type,
        qty_delta,
        running_balance as stored_balance,
        sum(qty_delta) over (
            partition by batch_id
            order by movement_seq
            rows between unbounded preceding and current row
        ) as recomputed_balance
    from marts.fct_inventory_movement

),

final_state as (

    select
        batch_id,
        store_id,
        sku_id,
        last(recomputed_balance order by movement_seq) as closing_units,
        count(*) as movements,
        count(*) filter (where event_type = 'expiry_writeoff') as writeoff_events,
        sum(case when stored_balance <> recomputed_balance then 1 else 0 end) as disagreements
    from ledger
    group by batch_id, store_id, sku_id

)

select
    batch_id,
    store_id,
    sku_id,
    movements,
    closing_units,
    writeoff_events,
    disagreements
from final_state
-- stock still sitting at the end of the window is what the expiry engine acts on
where closing_units > 0
order by closing_units desc, batch_id
limit 50;
