-- question: Does every unit that entered a store leave it through a door we can
--           name - sold, written off, or still on the shelf - and if not, where
--           does the identity break?
-- technique: full reconciliation as a single identity, with the residual
--            reported rather than a boolean, and a FULL OUTER JOIN so a batch
--            missing from either side cannot vanish
-- needs_days: 14
--
-- **A reconciliation that returns "true" is worth less than one that returns a
-- residual.** Anything can be made to tie by choosing the right filter; the
-- useful output is how far off it is and on which batches, because that is what
-- distinguishes a rounding artefact from a lost feed.
--
-- The identity is `received = sold + written off + remaining`. It is asserted
-- against two independent sources: `fct_inventory_batch`, which is the batch
-- ledger's own summary, and `fct_inventory_movement`, which is the event stream
-- it was built from. Comparing a table to itself proves nothing, so the
-- movement side is aggregated from scratch here rather than read from the
-- batch's stored columns.
--
-- **FULL OUTER JOIN, not inner.** A batch that exists in the ledger and has no
-- movements - or movements with no batch - is precisely the failure this query
-- is for, and an inner join would drop exactly those rows and report a clean
-- reconciliation. The 49,056 movements with a null `batch_id` that staging
-- repairs are the reason this is not hypothetical.

with from_movements as (

    select
        batch_id,
        sum(units) filter (where event_type in ('inbound', 'opening_balance')) as received_units,
        sum(units) filter (where event_type = 'sale') as sold_units,
        sum(units) filter (where event_type = 'expiry_writeoff') as writeoff_units,
        sum(qty_delta) as net_units
    from marts.fct_inventory_movement
    group by batch_id

),

from_ledger as (

    select
        batch_id,
        store_id,
        sku_id,
        qty_received,
        qty_sold,
        qty_written_off,
        qty_remaining,
        -- the warehouse's own verdict on this batch, carried through so the
        -- residual can be attributed rather than merely reported
        is_reconciled
    from marts.fct_inventory_batch

),

joined as (

    select
        coalesce(from_ledger.batch_id, from_movements.batch_id) as batch_id,
        from_ledger.store_id,
        from_ledger.sku_id,
        from_ledger.batch_id is null as missing_from_ledger,
        from_movements.batch_id is null as missing_from_movements,
        coalesce(from_ledger.is_reconciled, false) as is_reconciled,

        coalesce(from_ledger.qty_received, 0) as ledger_received,
        coalesce(from_ledger.qty_sold, 0) as ledger_sold,
        coalesce(from_ledger.qty_written_off, 0) as ledger_written_off,
        coalesce(from_ledger.qty_remaining, 0) as ledger_remaining,

        coalesce(from_movements.received_units, 0) as movement_received,
        coalesce(from_movements.sold_units, 0) as movement_sold,
        coalesce(from_movements.writeoff_units, 0) as movement_written_off,
        coalesce(from_movements.net_units, 0) as movement_balance
    from from_ledger
    full outer join from_movements on from_ledger.batch_id = from_movements.batch_id

),

residuals as (

    select
        joined.*,
        -- the identity, as a number rather than a verdict
        ledger_received - (ledger_sold + ledger_written_off + ledger_remaining)
            as ledger_residual,
        -- and the same batch measured from the event stream
        movement_received - (movement_sold + movement_written_off) - movement_balance
            as movement_residual,
        ledger_received - movement_received as received_disagreement,
        ledger_sold - movement_sold as sold_disagreement
    from joined

)

select
    -- split by the warehouse's own flag: the whole claim is that the identity
    -- breaks on the batches already marked unreconciled and on no others
    is_reconciled,
    count(*) as batches,
    count(*) filter (where missing_from_ledger) as missing_from_ledger,
    count(*) filter (where missing_from_movements) as missing_from_movements,
    count(*) filter (where ledger_residual <> 0) as ledger_identity_broken,
    count(*) filter (where movement_residual <> 0) as movement_identity_broken,
    count(*) filter (where received_disagreement <> 0) as received_disagrees,
    count(*) filter (where sold_disagreement <> 0) as sold_disagrees,
    coalesce(sum(abs(ledger_residual)), 0) as total_abs_ledger_residual,
    coalesce(max(abs(received_disagreement)), 0) as worst_received_gap,
    coalesce(max(abs(sold_disagreement)), 0) as worst_sold_gap
from residuals
group by is_reconciled
order by is_reconciled desc;
