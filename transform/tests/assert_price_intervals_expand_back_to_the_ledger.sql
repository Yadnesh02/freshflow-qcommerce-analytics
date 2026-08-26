{#
    The same lossless-expansion check dim_product_snapshot gets, for the same
    reason: collapsing days into intervals is lossy done wrong, and every way
    of doing it wrong is quiet.

    Expanding the intervals back across the calendar must reproduce exactly the
    store-SKU-days the promotion ledger recorded, at the prices it recorded -
    no day invented, none dropped. The failure this specifically catches is a
    gap swallowed: this feed is sparse, so a promo running Monday to Wednesday
    and again on Friday must be two intervals. Collapse it into one and
    Thursday silently acquires a discount that never ran.

    Compared in both directions, because a missing day and an invented day are
    different bugs and a single count would pass for whichever one you did not
    think of.
#}

with calendar as (

    select date_day from {{ ref('dim_date') }}

),

expanded as (

    select
        intervals.store_id,
        intervals.sku_id,
        calendar.date_day as effective_date,
        intervals.base_price,
        intervals.realized_price
    from {{ ref('fct_price_history') }} as intervals
    inner join calendar
        on
            calendar.date_day >= intervals.effective_from_date
            and calendar.date_day < intervals.effective_to_date

),

actual as (

    select distinct
        store_id,
        sku_id,
        effective_date,
        base_price,
        realized_price
    from {{ ref('stg_catalog__price_history') }}

),

missing as (

    select
        store_id,
        sku_id,
        effective_date,
        'priced day is not covered by any interval' as problem
    from (select * from actual except select * from expanded)

),

invented as (

    select
        store_id,
        sku_id,
        effective_date,
        'interval covers a day the ledger never priced' as problem
    from (select * from expanded except select * from actual)

)

select * from missing

union all by name

select * from invented
