{#
    The stock ledger, restricted to movements that can be attributed to a batch
    (defect 3).

    **What this model excludes and why that is not a drop.** About 1% of
    movements - 49k rows - arrive with no batch reference. Without one the
    movement cannot be tied to an expiry date, so it can neither be replayed
    into a running balance nor attributed to a write-off, and including it
    would corrupt every balance it touched. Excluding it quietly would be
    worse: the reconciliation test in S2.7 would then show a gap with no
    explanation. Both are avoided by routing those rows to stg_quarantine with
    the reason recorded, so the gap has a row count, a reason code and a test
    that ties the two sides back to the raw feed.

    This is the difference between a pipeline that is clean and one that is
    honest. The reconciliation is allowed not to balance; it is not allowed to
    not balance for unknown reasons.

    **`movement_seq` is the replay order.** An event log sorted by date alone
    has no defined order within a day, and a running balance computed over an
    undefined order is arbitrary. fct_availability_hour (S2.5) windows on this
    column, not on event_date.
#}

select
    movement_seq,
    batch_id,
    event_type,
    qty_delta,
    abs(qty_delta) as units,
    sign(qty_delta) as direction,
    event_date,
    dt as arrival_date
from {{ source('wms', 'wms_inventory_movement') }}
where batch_id is not null
