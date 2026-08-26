{#
    The standalone returns feed - the second of the two encodings in defect 5.

    Quantities here are positive: the feed records "3 units came back", not
    "-3 units were sold". stg_pos__order_lines is where that gets a sign; this
    model stays faithful to what the source system wrote, so the two encodings
    can be counted separately when they disagree.

    **Not deduplicated, deliberately.** Defect 1 duplicates the order feeds,
    not this one, and a return is not identified by its content: two units of
    the same SKU coming back from the same order on the same day for the same
    reason is a plausible pair of real events, and a row hash cannot tell that
    apart from a redelivery. Instead the natural key carries a `unique` test in
    _staging__models.yml. If a duplicate ever does appear, the build fails and
    someone reads this comment - which is the outcome a silent distinct() would
    have denied them.
#}

select
    order_id,
    sku_id,
    batch_id,
    qty as returned_qty,
    reason as return_reason,
    return_date,
    dt as arrival_date
from {{ source('pos', 'pos_returns') }}
