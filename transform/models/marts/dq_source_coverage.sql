{#
    One row per source feed per day, with how many rows arrived.

    **This is the fix defect 8 asked for and S2.1 deferred to here**: "a
    freshness and row-count check per source per day. Missing partitions must
    fail a check, not average to zero."

    The clickstream lost two partitions entirely. Nothing downstream errors
    when a day is missing - the rows simply are not there, so every average
    over the window quietly divides by a smaller denominator and comes back
    lower. It is the most dangerous class of data-quality failure precisely
    because the pipeline stays green, and it is why a row-count check per
    source per day has to exist as a first-class artefact rather than as an
    assumption.

    Materialising the coverage rather than only testing it means the Sprint 5
    data-quality page has something to read, and means an analyst can see which
    days are thin rather than only being told that some are.

    **Cadence is declared per source because "missing" is not universal.**
    pos_returns only exists on days a return happened and customer_snapshot is
    monthly; treating either as a daily feed would report hundreds of false
    gaps and train everyone to ignore this table. Only feeds that genuinely
    arrive every day are held to that standard.

    **A gap is measured inside a feed's own active life, not against the
    calendar.** The first version compared every daily source to the full
    catalogue window and immediately reported wms_purchase_orders as missing
    day one - which it is, and correctly so: no replenishment order exists
    before the first day's demand has been seen. A feed that starts late has
    not lost a partition, it has a start date, and conflating the two produces
    exactly the kind of permanent amber warning that teaches people to stop
    reading a data-quality table.

    A feed that *stops* arriving is the real freshness failure and is flagged
    separately as `is_stale`, because a source whose last partition is three
    days old fails silently in a way no gap-in-the-middle check would catch.
#}

with observed_window as (

    select
        min(products.snapshot_date) as first_day,
        max(products.snapshot_date) as last_day
    from {{ ref('stg_catalog__products') }} as products

),

calendar as (

    select dates.date_day
    from {{ ref('dim_date') }} as dates
    cross join observed_window
    where dates.date_day between observed_window.first_day and observed_window.last_day

),

{#
    Written out rather than generated from a list in a Jinja loop. The loop
    version is four lines shorter and renders SQL whose indentation the
    linter cannot follow, which trades a readable model for an unreadable
    diff every time it changes. This list changes when a source feed is
    added, which is roughly never.
#}

arrivals as (

    select
        'pos_orders' as source_name,
        arrival_date,
        count(*) as row_count
    from {{ ref('stg_pos__orders') }}
    group by arrival_date

    union all

    select
        'pos_order_items' as source_name,
        arrival_date,
        count(*) as row_count
    from {{ ref('stg_pos__order_items') }}
    group by arrival_date

    union all

    select
        'pos_returns' as source_name,
        arrival_date,
        count(*) as row_count
    from {{ ref('stg_pos__returns') }}
    group by arrival_date

    union all

    select
        'clickstream' as source_name,
        arrival_date,
        count(*) as row_count
    from {{ ref('stg_web__clickstream') }}
    group by arrival_date

    union all

    select
        'catalog_snapshot' as source_name,
        arrival_date,
        count(*) as row_count
    from {{ ref('stg_catalog__products') }}
    group by arrival_date

    union all

    select
        'price_history' as source_name,
        arrival_date,
        count(*) as row_count
    from {{ ref('stg_catalog__price_history') }}
    group by arrival_date

    union all

    select
        'wms_inventory_batch' as source_name,
        arrival_date,
        count(*) as row_count
    from {{ ref('stg_wms__inventory_batches') }}
    group by arrival_date

    union all

    select
        'wms_inventory_movement' as source_name,
        arrival_date,
        count(*) as row_count
    from {{ ref('stg_wms__inventory_movements') }}
    group by arrival_date

    union all

    select
        'wms_purchase_orders' as source_name,
        arrival_date,
        count(*) as row_count
    from {{ ref('stg_wms__purchase_orders') }}
    group by arrival_date

    union all

    select
        'wms_stockout_interval' as source_name,
        arrival_date,
        count(*) as row_count
    from {{ ref('stg_wms__stockout_intervals') }}
    group by arrival_date

    union all

    select
        'customer_snapshot' as source_name,
        snapshot_date as arrival_date,
        count(*) as row_count
    from {{ ref('stg_crm__customers') }}
    group by snapshot_date

),

sources as (

    select
        source_name,
        min(arrival_date) as first_seen_date,
        max(arrival_date) as last_seen_date,
        -- only feeds that genuinely arrive daily are held to daily coverage
        source_name not in ('pos_returns', 'customer_snapshot') as expects_daily
    from arrivals
    group by source_name

),

window_end as (

    select max(date_day) as last_expected_date from calendar

),

expected as (

    select
        sources.source_name,
        sources.expects_daily,
        sources.first_seen_date,
        sources.last_seen_date,
        calendar.date_day
    from sources
    inner join calendar
        on
            sources.first_seen_date <= calendar.date_day
            and sources.last_seen_date >= calendar.date_day

)

select
    expected.source_name,
    expected.date_day,
    expected.expects_daily,
    expected.first_seen_date,
    expected.last_seen_date,
    coalesce(arrivals.row_count, 0) as row_count,
    arrivals.arrival_date is not null as is_present,

    -- a hole inside the feed's own active life
    expected.expects_daily and arrivals.arrival_date is null as is_missing_partition,

    -- the feed stopped arriving before the window closed. Checked separately
    -- because a source that goes quiet fails in a way no gap check would see.
    expected.expects_daily
    and expected.last_seen_date < window_end.last_expected_date as is_stale
from expected
cross join window_end
left join arrivals
    on
        expected.source_name = arrivals.source_name
        and expected.date_day = arrivals.arrival_date
