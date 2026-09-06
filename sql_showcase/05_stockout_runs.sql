-- question: How long does a stockout actually last once it starts, and which
--           store-SKU pairs are out of stock in long unbroken stretches rather
--           than in brief repeated dips?
-- technique: gaps and islands over adjacent time intervals, merged with a
--            running sum of "this row starts a new run"
-- needs_days: 14
--
-- **Counting out-of-stock hours and counting stockouts are different questions
-- with different answers.** Two hundred hours spread over a hundred separate
-- one-hour dips is a replenishment cadence problem; the same two hundred hours
-- in four fifty-hour stretches is an assortment or supply problem, and the fix
-- is not the same. `fct_availability_hour` stores state intervals, so a stretch
-- that crosses midnight arrives as two rows and has to be stitched.
--
-- The merge is the classic form: flag a row as starting a new run when the
-- previous interval for that store-SKU did not end exactly where this one
-- begins, or when the previous state was different, then take a running sum of
-- that flag as the run's identity. This is safer here than the row-number
-- difference used in query 04, because these intervals are variable-length -
-- there is no fixed step to difference against.

with out_of_stock as (

    select
        store_id,
        sku_id,
        hour_ts_ist as starts_at,
        valid_to_hour_ts_ist as ends_at,
        hours_in_state
    from marts.fct_availability_hour
    where not is_in_stock

),

flagged as (

    select
        out_of_stock.*,
        case
            when
                lag(ends_at) over (
                    partition by store_id, sku_id order by starts_at
                ) = starts_at
                then 0
            else 1
        end as starts_new_run
    from out_of_stock

),

runs as (

    select
        flagged.*,
        sum(starts_new_run) over (
            partition by store_id, sku_id
            order by starts_at
            rows between unbounded preceding and current row
        ) as run_id
    from flagged

),

merged as (

    select
        store_id,
        sku_id,
        run_id,
        min(starts_at) as run_started,
        max(ends_at) as run_ended,
        sum(hours_in_state) as hours_out,
        count(*) as intervals_stitched
    from runs
    group by store_id, sku_id, run_id

)

select
    merged.store_id,
    merged.sku_id,
    products.sku_name,
    products.l1_category,
    count(*) as stockout_runs,
    sum(merged.hours_out) as total_hours_out,
    -- the diagnostic: many short runs vs few long ones
    round(avg(merged.hours_out), 1) as avg_hours_per_run,
    max(merged.hours_out) as longest_run_hours,
    round(max(merged.hours_out) / nullif(avg(merged.hours_out), 0), 1) as worst_vs_typical
from merged
inner join marts.dim_product as products on merged.sku_id = products.sku_id
group by merged.store_id, merged.sku_id, products.sku_name, products.l1_category
having sum(merged.hours_out) > 0
order by longest_run_hours desc, total_hours_out desc
limit 50;
