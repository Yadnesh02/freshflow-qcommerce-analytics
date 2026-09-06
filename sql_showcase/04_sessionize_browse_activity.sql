-- question: When a product starts attracting attention, how long does the burst
--           last - and are the longest bursts the ones where the shelf was
--           empty the whole time?
-- technique: gaps and islands, by differencing a row number against an hour
--            index so that consecutive hours collapse into one run
-- needs_days: 7
--
-- **This sessionises store-SKU attention, not users, because the feed has no
-- user on it.** `fct_clickstream` is delivered pre-aggregated to store x sku x
-- hour, so the textbook "30 minutes of inactivity ends a session" cannot be
-- written here - there is nobody to be inactive. Saying so is better than
-- inventing a `customer_id` join that would silently fan out.
--
-- The island trick: number the rows per store-SKU in time order, convert each
-- timestamp to an integer hour, and subtract. Consecutive hours advance both
-- counters in step, so the difference is constant inside a run and jumps at
-- every gap. Group by that difference and each group is one unbroken burst.

with hourly as (

    select
        store_id,
        sku_id,
        hour_ts_ist,
        sum(event_count) as events,
        -- a burst that happened entirely against an empty shelf is demand the
        -- store could not serve, which is the interesting kind
        bool_and(not was_in_stock) as never_in_stock,
        sum(censored_event_count) as censored_events
    from marts.fct_clickstream
    where event_type = 'pdp_view'
    group by store_id, sku_id, hour_ts_ist

),

indexed as (

    select
        hourly.*,
        -- hours since epoch: consecutive hours differ by exactly 1
        cast(epoch(hour_ts_ist) / 3600 as bigint) as hour_index,
        row_number() over (
            partition by store_id, sku_id order by hour_ts_ist
        ) as seq
    from hourly

),

islands as (

    select
        indexed.*,
        hour_index - seq as island_key
    from indexed

)

select
    store_id,
    sku_id,
    min(hour_ts_ist) as burst_started,
    max(hour_ts_ist) as burst_ended,
    count(*) as hours_in_burst,
    sum(events) as events,
    sum(censored_events) as censored_events,
    bool_and(never_in_stock) as empty_shelf_throughout
from islands
group by store_id, sku_id, island_key
having count(*) >= 3
order by hours_in_burst desc, events desc
limit 50;
