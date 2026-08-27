{#
    Days-to-expiry at the moment of sale cannot exceed the product's own shelf
    life, and cannot be negative.

    The lower bound is the interesting one and is already a G1 invariant:
    negative means expired stock reached a customer. The upper bound catches
    the opposite mistake - a batch dated further out than the product could
    possibly last, which means either the expiry date or the shelf life is
    wrong, and both corrupt every freshness and markdown decision built on them.

    Written as a join rather than as a numeric bound because the ceiling is per
    SKU. The catalogue runs from 1 day to 1,440, so any single global number is
    either so loose it catches nothing or so tight it fails the first time a
    long-life product is ranged. A first attempt at this used 400 and failed on
    824,221 perfectly good rows.
#}

select
    items.order_id,
    items.sku_id,
    items.batch_id,
    items.dte_at_sale,
    products.shelf_life_days
from {{ ref('stg_pos__order_items') }} as items
inner join {{ ref('dim_product') }} as products
    on items.sku_id = products.sku_id
where
    items.dte_at_sale < 0
    or items.dte_at_sale > products.shelf_life_days
