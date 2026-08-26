{#
    The S2.5 acceptance gate, and the reason the table exists.

    A midnight snapshot asks whether stock existed at 00:00. It is the way
    availability is usually measured, because it is the easy way, and on a day
    that ran out at 19:00 it answers yes - recording the day as fully available
    while five hours of the evening peak sold nothing. Time-weighted
    availability divides hours in stock by hours carried and does not.

    If these two ever agreed, one of three things would be true: the hourly
    model collapsed to a daily one, stockouts stopped happening mid-day, or the
    time weighting was not actually being applied. All three make this table
    pointless, and all three are invisible in a number that still looks like a
    percentage. So the test fails when the gap closes rather than when it opens.

    The threshold is deliberately loose. The point is not that the gap is a
    particular size, it is that measuring availability the easy way is
    materially wrong.
#}

with by_day as (

    select
        store_id,
        sku_id,
        date_day,
        sum(hours_in_state) as hours_carried,
        sum(case when is_in_stock then hours_in_state else 0 end) as hours_in_stock,
        -- the snapshot reading: was there stock at midnight?
        max(case when from_hour_ist = 0 and is_in_stock then 1 else 0 end) as in_stock_at_midnight
    from {{ ref('fct_availability_hour') }}
    group by store_id, sku_id, date_day

),

compared as (

    select
        sum(hours_in_stock) / nullif(cast(sum(hours_carried) as double), 0)
            as time_weighted_in_stock_pct,
        avg(cast(in_stock_at_midnight as double)) as midnight_snapshot_in_stock_pct
    from by_day

)

select
    time_weighted_in_stock_pct,
    midnight_snapshot_in_stock_pct,
    midnight_snapshot_in_stock_pct - time_weighted_in_stock_pct as overstatement
from compared
where
    time_weighted_in_stock_pct is null
    or midnight_snapshot_in_stock_pct - time_weighted_in_stock_pct < 0.001
