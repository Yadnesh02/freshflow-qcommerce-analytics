{#
    Defect 4. A pack measured in grams or millilitres cannot weigh less than
    one of them, and a pack measured in kilograms or litres is not a hundred of
    them. Both bounds sit in gaps two orders of magnitude wide, so this is a
    plausibility check rather than a tuned threshold.

    It runs on the repaired column, so it is asserting that the fix worked -
    not that the raw feed was clean. Pointed at the source it would fail on all
    5,110 drifted rows, which is the point: the same statement is a defect
    report before staging and a guarantee after it.
#}

select
    sku_id,
    snapshot_date,
    uom,
    pack_qty,
    pack_size_base_units
from {{ ref('stg_catalog__products') }}
where
    pack_qty <= 0
    or (uom in ('g', 'ml') and pack_qty < 1)
    or (lower(uom) in ('kg', 'l') and pack_qty > 100)
