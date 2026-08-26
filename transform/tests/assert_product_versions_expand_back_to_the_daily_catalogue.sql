{#
    The compression is lossless: expanding the 1,847 versions back across the
    calendar reproduces all 547,500 daily catalogue rows exactly, and nothing
    else.

    This is the test that makes deriving the dimension defensible instead of
    merely convenient. Collapsing a daily history into intervals is a lossy
    operation done wrong, and every way of getting it wrong is quiet: an
    off-by-one on the boundary shifts a price by a day, a version closed at its
    own last day instead of its successor's first leaves a one-day hole, and a
    version left open on a delisted SKU invents rows for dates the catalogue
    never covered. All three produce a table that looks right.

    Compared in both directions on purpose. Missing rows and invented rows are
    different bugs, and a count that only checks one side passes for whichever
    one you did not think of.
#}

with calendar as (

    select distinct snapshot_date from {{ ref('stg_catalog__products') }}

),

expanded as (

    select
        versions.sku_id,
        calendar.snapshot_date,
        versions.landed_cost,
        versions.base_price
    from {{ ref('dim_product_snapshot') }} as versions
    inner join calendar
        on
            calendar.snapshot_date >= versions.valid_from_date
            and (
                calendar.snapshot_date < versions.valid_to_date
                or versions.valid_to_date is null
            )

),

actual as (

    select
        sku_id,
        snapshot_date,
        landed_cost,
        base_price
    from {{ ref('stg_catalog__products') }}

),

missing as (

    select
        sku_id,
        snapshot_date,
        'day is not covered by any version' as problem
    from (select * from actual except select * from expanded)

),

invented as (

    select
        sku_id,
        snapshot_date,
        'version covers a day the catalogue does not' as problem
    from (select * from expanded except select * from actual)

)

select * from missing

union all by name

select * from invented
