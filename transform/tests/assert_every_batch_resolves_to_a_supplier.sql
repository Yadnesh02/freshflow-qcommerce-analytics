{#
    The S2.1 finding, closed.

    15,557 batches carry `SUP-OPENING`, a sentinel the supplier reference feed
    has never heard of. dim_supplier's unknown member is what makes them
    joinable; this test is what stops that row being deleted by someone tidying
    up a dimension with a null-heavy row in it.

    The failure it guards against is not an error. It is an inner join that
    returns slightly fewer rows, so opening stock quietly stops appearing in
    every supplier rollup and every wastage-by-supplier chart is short by a
    day-one cohort - in a business whose whole question is what happens to
    stock as it ages, which is not a rounding error.
#}

select
    batches.batch_id,
    batches.supplier_id,
    batches.is_opening_balance
from {{ ref('stg_wms__inventory_batches') }} as batches
where not exists (
    select 1
    from {{ ref('dim_supplier') }} as suppliers
    where suppliers.supplier_id = batches.supplier_id
)
