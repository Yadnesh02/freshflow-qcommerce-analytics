{#
    Order lines, deduplicated on the business row hash (defect 1).

    Faithful to the feed: the rows with negative quantities are left where they
    are. They are one of the two encodings of defect 5, and normalising them
    happens in stg_pos__order_lines - one model deduplicates, the next one
    reconciles the encodings, and keeping those separate means a failure in
    either is attributable.

    **The grain is (order_id, sku_id, batch_id), not (order_id, sku_id).** When
    a line is larger than the batch at the front of the FEFO queue it is split
    across two batches, and both halves are real lines with different expiry
    dates attached. Deduplicating to the SKU would destroy exactly the
    attribution this project is built on.

    **Duplicates cannot be told apart from batch splits by counting alone**,
    which is why the hash covers every business column: two split halves differ
    in batch_id and quantity, a retried delivery differs in nothing.

    `discount_amt` and `unit_cogs` are per unit, not per line. Extending them
    is fct_order_item's job (S2.4); staging stops at making the sign correct.
#}

{%- set business_columns = [
    'order_id',
    'sku_id',
    'batch_id',
    'qty',
    'unit_base_price',
    'unit_realized_price',
    'discount_amt',
    'unit_cogs',
    'dte_at_sale',
    'promo_id',
    'is_substitution',
] -%}

with source as (

    select * from {{ source('pos', 'pos_order_items') }}

),

hashed as (

    select
        order_id,
        sku_id,
        batch_id,
        promo_id,
        qty,
        unit_base_price,
        unit_realized_price,
        discount_amt,
        unit_cogs,
        dte_at_sale,
        is_substitution,
        dt as arrival_date,
        {{ row_hash(business_columns) }} as row_hash
    from source

),

deduplicated as (

    select *
    from hashed
    qualify row_number() over (partition by row_hash order by arrival_date) = 1

)

select
    order_id,
    sku_id,
    batch_id,
    promo_id,
    qty,
    unit_base_price,
    unit_realized_price,
    discount_amt,
    unit_cogs,
    dte_at_sale,
    is_substitution,
    promo_id is not null as is_promoted,

    -- defect 5, encoding A: a return posted back into the sales feed
    qty < 0 as is_return_encoded_as_negative,

    arrival_date,
    row_hash
from deduplicated
