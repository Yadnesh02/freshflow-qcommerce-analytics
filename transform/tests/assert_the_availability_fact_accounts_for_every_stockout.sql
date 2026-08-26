{#
    Every stockout the WMS recorded has to appear in the availability fact, or
    be explainable as a day the store was not yet carrying the SKU.

    This exists because the first version of fct_availability_hour silently
    dropped 63% of them. Its assortment window ran from first receipt to last
    batch expiry, which sounds right and deletes precisely the days that matter
    most: a SKU the store still lists but has stopped replenishing sits out of
    stock indefinitely, and every one of those days falls after its last batch
    expired. Availability read 98.4% instead of the truth.

    Nothing failed. The table built, the tests passed, and the number looked
    plausible - which is exactly why the count is asserted against the source
    feed rather than left to inspection.

    The only stockouts allowed to be absent are those on a store-SKU before its
    first receipt, or one the store never received at all: there is no
    assortment timeline to place them on, and inventing a start date to hold
    them would be worse than counting them here.
#}

with recorded as (

    select
        stockouts.store_id,
        stockouts.sku_id,
        stockouts.stockout_date
    from {{ ref('stg_wms__stockout_intervals') }} as stockouts

),

carried_from as (

    select
        store_id,
        sku_id,
        min(received_date) as first_carried_date
    from {{ ref('fct_inventory_batch') }}
    group by store_id, sku_id

),

placeable as (

    -- stockouts that sit on a day the store was already carrying the SKU
    select recorded.*
    from recorded
    inner join carried_from
        on
            recorded.store_id = carried_from.store_id
            and recorded.sku_id = carried_from.sku_id
    where recorded.stockout_date >= carried_from.first_carried_date

),

in_the_fact as (

    select distinct
        store_id,
        sku_id,
        date_day as stockout_date
    from {{ ref('fct_availability_hour') }}
    where not is_in_stock

)

select
    placeable.store_id,
    placeable.sku_id,
    placeable.stockout_date,
    'stockout recorded by the WMS but absent from the availability fact' as problem
from placeable
where not exists (
    select 1
    from in_the_fact
    where
        in_the_fact.store_id = placeable.store_id
        and in_the_fact.sku_id = placeable.sku_id
        and in_the_fact.stockout_date = placeable.stockout_date
)
