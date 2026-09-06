-- question: Under first-expiry-first-out picking, which open batches will
--           actually be sold before they expire, and how many units are
--           already committed to being written off?
-- technique: cumulative sum as a queue position, interval arithmetic against a
--            demand rate, left join to keep the zero-demand case visible
-- needs_days: 14
--
-- FEFO is the allocation rule this whole warehouse assumes, and it is what
-- makes perishable inventory different: a unit's fate is decided by how much
-- stock sits *ahead* of it in the queue, not by how much stock exists. Two
-- stores with identical on-hand and identical demand can have completely
-- different write-off exposure if one of them holds it in a single long-dated
-- batch and the other in four short-dated ones.
--
-- The queue position is a cumulative sum over batches ordered by expiry. A unit
-- clears only if the demand arriving before its expiry date exceeds everything
-- ahead of it. Demand comes from the trailing seven-day average on the last day
-- the warehouse covers, which is a rate rather than a forecast - deliberately,
-- because the point here is the allocation logic and a forecast would put a
-- model in the middle of it.

with window_end as (

    select max(date_day) as as_of from marts.agg_store_sku_day

),

demand_rate as (

    -- the trailing average as it stood on the final day, one row per store-sku
    select
        agg.store_id,
        agg.sku_id,
        agg.trailing_7d_avg_units as daily_units
    from marts.agg_store_sku_day as agg
    inner join window_end on agg.date_day = window_end.as_of

),

open_batches as (

    select
        batches.batch_id,
        batches.store_id,
        batches.sku_id,
        batches.expiry_date,
        batches.qty_remaining,
        window_end.as_of,
        date_diff('day', window_end.as_of, batches.expiry_date) as days_left
    from marts.fct_inventory_batch as batches
    cross join window_end
    where
        batches.qty_remaining > 0
        and batches.expiry_date >= window_end.as_of

),

queued as (

    select
        open_batches.*,
        -- units that must sell before this batch is touched at all
        coalesce(
            sum(open_batches.qty_remaining) over (
                partition by open_batches.store_id, open_batches.sku_id
                order by open_batches.expiry_date, open_batches.batch_id
                rows between unbounded preceding and 1 preceding
            ),
            0
        ) as units_ahead_in_queue
    from open_batches

)

select
    queued.store_id,
    queued.sku_id,
    queued.batch_id,
    queued.expiry_date,
    queued.days_left,
    queued.qty_remaining,
    queued.units_ahead_in_queue,
    round(coalesce(demand_rate.daily_units, 0), 2) as daily_units,
    -- what the queue can clear before this batch dies
    round(coalesce(demand_rate.daily_units, 0) * queued.days_left, 1) as demand_before_expiry,
    greatest(
        0,
        queued.qty_remaining
        - greatest(
            0,
            coalesce(demand_rate.daily_units, 0) * queued.days_left - queued.units_ahead_in_queue
        )
    ) as units_expected_to_expire
from queued
-- a left join, not inner: a batch whose store-sku sold nothing on the final day
-- has no trailing average, and dropping it would hide the worst cases
left join demand_rate
    on
        queued.store_id = demand_rate.store_id
        and queued.sku_id = demand_rate.sku_id
order by units_expected_to_expire desc, queued.expiry_date
limit 50;
