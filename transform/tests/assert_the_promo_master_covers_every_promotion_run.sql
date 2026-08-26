{#
    dim_promotion gets `promo_type` and `funding_source` from a hand-maintained
    seed, which is the honest place for commercial facts no feed records - and
    also the place that goes stale first.

    A promotion that runs without a master row does not error. It joins to
    nulls, drops out of every funding-source split, and its subsidy silently
    stops counting against GM-after-wastage - so the margin looks better
    precisely because a promo was forgotten.

    Checked from both feeds that carry a promo_id, because a promo can appear
    on an order line without ever having been written to the price ledger.
#}

with run_in_the_price_ledger as (

    select distinct
        promo_id,
        'price_history' as seen_in
    from {{ ref('stg_catalog__price_history') }}
    where promo_id is not null

),

run_on_order_lines as (

    select distinct
        promo_id,
        'order_lines' as seen_in
    from {{ ref('stg_pos__order_lines') }}
    where promo_id is not null

),

observed as (

    select * from run_in_the_price_ledger
    union all
    select * from run_on_order_lines

)

select
    observed.promo_id,
    observed.seen_in
from observed
where not exists (
    select 1
    from {{ ref('dim_promotion') }} as master
    where master.promo_id = observed.promo_id
)
