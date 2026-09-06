-- question: Which categories are growing month on month, and is the growth
--           real or is it a month with more days and a festival in it?
-- technique: LAG over an ordered partition for the period-on-period delta, with
--            a per-day normalisation so calendar length cannot masquerade as
--            growth
-- needs_days: 90
--
-- **This is month-over-month and not year-over-year, and that is a data
-- constraint rather than a preference.** The simulated world runs from
-- 2025-09-01 to 2026-08-31 - exactly twelve months - so every SKU's
-- year-earlier comparison is null and a YoY query would return a column of
-- blanks. Writing one anyway and letting it come back empty is how a portfolio
-- query gets quietly copied into somewhere it matters.
--
-- The same LAG mechanic answers YoY the moment a second year exists: change the
-- offset to 12 and the partition stays as it is. What does *not* transfer is
-- the seasonality problem - a month-on-month comparison in this business is
-- contaminated by monsoon and festival demand in a way a YoY comparison would
-- not be, which is why `revenue_per_day` and the festival flag are both here.

with monthly as (

    select
        products.l1_category,
        date_trunc('month', agg.date_day) as month_start,
        sum(agg.net_revenue) as revenue,
        sum(agg.units_sold) as units,
        count(distinct agg.date_day) as days_in_month
    from marts.agg_store_sku_day as agg
    inner join marts.dim_product as products on agg.sku_id = products.sku_id
    group by products.l1_category, date_trunc('month', agg.date_day)

),

festival_days as (

    select
        date_trunc('month', date_day) as month_start,
        count(*) filter (where is_festival) as festival_days
    from marts.dim_date
    group by date_trunc('month', date_day)

),

with_lag as (

    select
        monthly.*,
        coalesce(festival_days.festival_days, 0) as festival_days,
        monthly.revenue / nullif(monthly.days_in_month, 0) as revenue_per_day,
        lag(monthly.revenue) over (
            partition by monthly.l1_category order by monthly.month_start
        ) as prev_revenue,
        lag(monthly.revenue / nullif(monthly.days_in_month, 0)) over (
            partition by monthly.l1_category order by monthly.month_start
        ) as prev_revenue_per_day
    from monthly
    left join festival_days on monthly.month_start = festival_days.month_start

)

select
    l1_category,
    month_start,
    days_in_month,
    festival_days,
    round(revenue, 0) as revenue,
    round(revenue_per_day, 0) as revenue_per_day,
    round(100 * (revenue - prev_revenue) / nullif(prev_revenue, 0), 1) as mom_pct,
    -- the honest one: growth that survives the calendar
    round(
        100 * (revenue_per_day - prev_revenue_per_day) / nullif(prev_revenue_per_day, 0), 1
    ) as mom_per_day_pct,
    -- when these two disagree in sign, the headline number is a calendar artefact
    sign(revenue - prev_revenue) <> sign(revenue_per_day - prev_revenue_per_day)
        as calendar_flipped_the_sign
from with_lag
where prev_revenue is not null
order by l1_category, month_start;
