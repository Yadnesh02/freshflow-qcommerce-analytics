{#
    Browsing demand, at the grain the feed actually has.

    **The column that justifies the table**: `was_in_stock`. It is the only
    record of demand that existed while stock was zero, and sales data cannot
    tell "nobody wanted it" from "we had none". A forecast trained on sales
    alone under-orders the fast movers permanently, which is why the
    censored-demand correction in Sprint 3 reads this fact and not the order
    feed.

    **Why this is one row per store, SKU, hour, event type and stock state -
    and not one row per event.** The collector timestamps to the hour, so 3.07M
    raw rows resolve to 1.72M distinct combinations: 44% of the feed is rows
    that are byte-identical to another row and carry no information
    distinguishing them. Emitting them individually would mean inventing a
    surrogate key to tell apart records that differ in nothing, and publishing
    a per-event grain the source cannot support. Counting them says the same
    thing, honestly, in half the rows - and `event_count` is exactly the
    quantity every consumer wants, because unserved demand is a count of
    signals, not a list of them.

    Nothing is lost. Identical rows are indistinguishable by construction, so
    the count is a complete description of them, and a test asserts the counts
    sum back to the raw feed exactly.

    **The feed is anonymous** - no session, no customer, and none invented.
    Synthesising a customer would fabricate evidence for journey analysis the
    data cannot support. What it does support is how much demand went unserved
    for a SKU at a store in an hour, which needs no identity at all.

    **The two missing days stay missing.** Defect 8 removed them and no join
    invents them back. The collector fell over under load, so they are two of
    the busiest days of the year, and filling them biases the censoring signal
    downward exactly where stockouts were worst.
#}

select
    store_id,
    sku_id,
    event_type,
    was_in_stock,

    cast(date_trunc('hour', event_ts_ist) as timestamp) as hour_ts_ist,
    event_date_ist as date_day,
    event_hour_ist,

    count(*) as event_count,

    -- demand that existed and could not be served: the uncensoring signal,
    -- pre-split so no consumer has to remember which way the flag points
    count(*) filter (where is_censored_demand) as censored_event_count,
    bool_or(is_censored_demand) as is_censored_demand,

    min(event_ts_utc) as event_ts_utc,
    bool_or(sku_id_was_conformed) as sku_id_was_conformed,
    min(arrival_date) as arrival_date
from {{ ref('stg_web__clickstream') }}
group by
    store_id,
    sku_id,
    event_type,
    was_in_stock,
    event_date_ist,
    event_hour_ist,
    cast(date_trunc('hour', event_ts_ist) as timestamp)
