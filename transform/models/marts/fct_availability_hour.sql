{#
    Availability at hourly resolution, as state intervals rather than a dense
    grid.

    **Why intervals and not one row per hour.** 17,785 store-SKU pairs across
    a year of open hours is 170 million rows - a table nothing on a laptop can
    build, nothing under 80 MB can ship, and which would be 99% repetition:
    stock state changes at most once a day, so all but a handful of those rows
    would say exactly what the row above them said. One row per contiguous run
    of a state carries the identical information in 7 million, and every
    question asked of the dense version is asked of this one with a duration
    weight instead of a count.

    Keyed on (store_id, sku_id, hour_ts_ist) all the same, so the grain the ERD
    declares still holds - the hours are simply the ones where something
    changed.

    **What the WMS actually gives us.** The stockout feed is one row per
    store-SKU-day carrying the hour stock ran out - not a full out-of-stock
    series. Its hour distribution tracks the demand curve, peaking at the
    morning and evening rushes, because that is when depletion happens. So a
    day reads as: in stock from opening until hour H, out of stock from H until
    replenishment arrives the next day. A day with no row was never short.

    **Why this matters more than it sounds.** A midnight snapshot - the way
    availability is usually measured, because it is the easy way - asks whether
    stock existed at 00:00. On a day that ran out at 19:00 the answer is yes,
    and the day is recorded as fully available while five hours of the evening
    peak sold nothing. Time-weighted availability divides hours in stock by
    hours carried and gets it right. The gap between those two numbers is
    exactly the demand this project is trying to stop losing, and
    assert_time_weighted_availability_differs_from_a_midnight_snapshot fails
    the build if it ever closes.

    **On-hand is a day-start figure, and named to say so.** The movement ledger
    is daily-grained - it records the date an event happened, not the time - so
    there is no honest way to carry a unit count across the hours within a day.
    `on_hand_units_at_day_start` is the running balance from the ledger, replayed
    in movement_seq order, as it stood when the day opened. Anything wanting
    true hourly depletion has to model it from order timestamps, which is
    Sprint 3's job and not a column to fake here.
#}

with assortment as (

    -- A store carries a SKU from its first receipt to the end of the window.
    --
    -- Not to its last batch's expiry, which was the first thing tried and is
    -- wrong in the most important direction: a SKU the store still lists but
    -- has stopped replenishing is out of stock, not out of assortment, and
    -- ending the window at the last expiry deletes exactly those days. It cost
    -- 358,885 stockout days - 63% of every stockout in the feed - and every
    -- one of them was a SKU sitting dead on the shelf, which is the worst
    -- availability failure there is and the one a store most needs to see.
    select
        store_id,
        sku_id,
        min(received_date) as first_carried_date
    from {{ ref('fct_inventory_batch') }}
    group by store_id, sku_id

),

window_end as (

    select max(snapshot_date) as last_observed_date from {{ ref('stg_catalog__products') }}

),

spine as (

    select
        assortment.store_id,
        assortment.sku_id,
        calendar.date_day
    from assortment
    cross join window_end
    inner join {{ ref('dim_date') }} as calendar
        on
            assortment.first_carried_date <= calendar.date_day
            and window_end.last_observed_date >= calendar.date_day

),

daily_delta as (

    select
        store_id,
        sku_id,
        date_day,
        sum(qty_delta) as delta
    from {{ ref('fct_inventory_movement') }}
    group by store_id, sku_id, date_day

),

balances as (

    select
        spine.store_id,
        spine.sku_id,
        spine.date_day,
        coalesce(daily_delta.delta, 0) as delta,
        sum(coalesce(daily_delta.delta, 0)) over (
            partition by spine.store_id, spine.sku_id
            order by spine.date_day
            rows between unbounded preceding and current row
        ) as on_hand_close
    from spine
    left join daily_delta
        on
            spine.store_id = daily_delta.store_id
            and spine.sku_id = daily_delta.sku_id
            and spine.date_day = daily_delta.date_day

),

with_open as (

    select
        *,
        lag(on_hand_close) over (
            partition by store_id, sku_id order by date_day
        ) as previous_close
    from balances

),

days as (

    select
        with_open.store_id,
        with_open.sku_id,
        with_open.date_day,
        coalesce(with_open.previous_close, 0) as on_hand_open,
        with_open.on_hand_close,
        stockouts.stockout_hour_ist as ran_out_at_hour
    from with_open
    left join {{ ref('stg_wms__stockout_intervals') }} as stockouts
        on
            with_open.store_id = stockouts.store_id
            and with_open.sku_id = stockouts.sku_id
            and with_open.date_day = stockouts.stockout_date

),

in_stock_runs as (

    select
        store_id,
        sku_id,
        date_day,
        0 as from_hour,
        coalesce(ran_out_at_hour, 24) as to_hour,
        true as is_in_stock,
        on_hand_open as on_hand_units_at_day_start,
        ran_out_at_hour
    from days
    -- a day that ran out at midnight was never in stock, so it contributes no
    -- in-stock run at all
    where coalesce(ran_out_at_hour, 24) > 0

),

out_of_stock_runs as (

    select
        store_id,
        sku_id,
        date_day,
        ran_out_at_hour as from_hour,
        24 as to_hour,
        false as is_in_stock,
        0 as on_hand_units_at_day_start,
        ran_out_at_hour
    from days
    where ran_out_at_hour is not null

),

runs as (

    select * from in_stock_runs
    union all
    select * from out_of_stock_runs

)

select
    store_id,
    sku_id,
    cast(date_day as timestamp) + to_hours(from_hour) as hour_ts_ist,
    cast(date_day as timestamp) + to_hours(to_hour) as valid_to_hour_ts_ist,
    date_day,
    from_hour as from_hour_ist,
    to_hour as to_hour_ist,
    to_hour - from_hour as hours_in_state,
    is_in_stock,
    on_hand_units_at_day_start as on_hand_units,

    -- every row in this table is a day the store carried the SKU; the column
    -- is kept because the ERD declares it and because a future assortment feed
    -- would make it vary
    true as in_assortment,
    ran_out_at_hour
from runs
