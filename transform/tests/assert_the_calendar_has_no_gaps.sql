{#
    A date dimension with a missing day is the quietest bug in a warehouse.

    Every daily aggregate joins through it, so a hole does not error - the
    affected day just stops appearing. A weekly total silently covers six days,
    a moving average shifts by one, and the series still looks like a series.

    Also asserts the calendar reaches past the last day of data, because the
    forecast horizons in Sprint 3 write predictions for dates that have not
    happened yet, and a spine that stops at the newest fact drops every one of
    them on the join.
#}

with calendar as (

    select
        date_day,
        lead(date_day) over (order by date_day) as next_date_day
    from {{ ref('dim_date') }}

),

gaps as (

    select
        date_day,
        next_date_day,
        'calendar skips a day' as problem
    from calendar
    where next_date_day is not null and next_date_day <> date_day + 1

),

coverage as (

    select
        max(orders.order_date_ist) as date_day,
        (select max(date_day) from {{ ref('dim_date') }}) as next_date_day,
        'calendar does not extend past the newest fact' as problem
    from {{ ref('stg_pos__orders') }} as orders
    having max(orders.order_date_ist) >= (select max(date_day) from {{ ref('dim_date') }})

)

select * from gaps

union all by name

select * from coverage
