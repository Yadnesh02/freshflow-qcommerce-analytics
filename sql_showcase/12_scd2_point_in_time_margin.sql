-- question: What margin did we actually earn on historical sales, using the
--           cost that was in force on the day - and how wrong is the answer if
--           we use today's cost instead?
-- technique: SCD2 point-in-time join on a validity interval, contrasted against
--            the naive join to the current dimension
-- needs_days: 60
--
-- **This is the query that shows why the snapshot exists.** 321 SKUs changed
-- landed cost or base price during the window; a join to `dim_product` gives
-- every one of them today's cost, applied retroactively to a year of sales.
-- The error does not average out - cost changes are mostly increases, so the
-- naive join systematically understates historical margin on exactly the SKUs
-- that moved.
--
-- The point-in-time join is `valid_from <= event_date < valid_to`, with the
-- open interval's null `valid_to` coalesced to a far future date. Half-open is
-- deliberate: `<=` on both ends double-counts the changeover day, and a version
-- boundary that overlaps by one day fans the fact table out silently.
--
-- The naive join is kept in the same result rather than described, because the
-- size of the difference is the argument.

with sales as (

    select
        items.sku_id,
        items.date_day,
        sum(items.units) as units,
        sum(items.net_revenue) as revenue,
        sum(items.cogs) as cogs_as_booked
    from marts.fct_order_item as items
    where items.line_type = 'sale'
    group by items.sku_id, items.date_day

),

point_in_time as (

    select
        sales.sku_id,
        sales.date_day,
        sales.units,
        sales.revenue,
        sales.cogs_as_booked,
        snapshot.landed_cost as cost_in_force,
        snapshot.version_no
    from sales
    inner join marts.dim_product_snapshot as snapshot
        on
            sales.sku_id = snapshot.sku_id
            -- half-open interval: a sale on the changeover day belongs to the
            -- new version, once, and to nothing else
            and sales.date_day >= snapshot.valid_from_date
            and sales.date_day < coalesce(snapshot.valid_to_date, date '2999-12-31')

),

compared as (

    select
        point_in_time.sku_id,
        sum(point_in_time.units) as units,
        sum(point_in_time.revenue) as revenue,
        count(distinct point_in_time.version_no) as versions_used,
        -- margin using the cost that was in force on each day
        sum(point_in_time.revenue - point_in_time.units * point_in_time.cost_in_force)
            as margin_point_in_time,
        -- margin using today's cost for the whole year
        sum(point_in_time.revenue - point_in_time.units * products.landed_cost)
            as margin_current_cost
    from point_in_time
    inner join marts.dim_product as products on point_in_time.sku_id = products.sku_id
    group by point_in_time.sku_id

)

select
    compared.sku_id,
    products.sku_name,
    products.l1_category,
    compared.versions_used,
    compared.units,
    round(compared.revenue, 0) as revenue,
    round(compared.margin_point_in_time, 0) as margin_point_in_time,
    round(compared.margin_current_cost, 0) as margin_current_cost,
    round(compared.margin_current_cost - compared.margin_point_in_time, 0) as error_inr,
    round(
        100 * (compared.margin_current_cost - compared.margin_point_in_time)
        / nullif(compared.margin_point_in_time, 0),
        2
    ) as error_pct
from compared
inner join marts.dim_product as products on compared.sku_id = products.sku_id
-- only the SKUs that actually changed: everywhere else the two joins agree by
-- construction, and including them would dilute the finding to nothing
where compared.versions_used > 1
order by abs(compared.margin_current_cost - compared.margin_point_in_time) desc
limit 40;
