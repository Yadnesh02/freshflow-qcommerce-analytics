{#
    The cohort triangle: what share of each signup cohort was still ordering
    n months later (S3.5).

    `mart_customer_360` carries M1 on the customer row because the metric
    registry asks for it there, but M1 alone cannot show a decay curve and the
    curve is the point. This model is one row per cohort per month index, which
    is the shape a triangle renders from.

    **Order-based, not login-based**, as the plan insists. A customer who opens
    the app and buys nothing has not been retained; counting them would turn a
    retention chart into an engagement chart and quietly inflate every number
    on it.

    **Every cohort is measured against its own full size**, including the
    members who never ordered at all. Dividing by "customers who ordered in M0"
    instead would compute retention over the population that had already
    retained once, which reads far better and answers a question nobody asked.
    That decision is the single largest driver of the level of these numbers.

    **The triangle is ragged at both ends, and both ends are cut.**

    At the recent end: a cohort that signed up two months before the data stops
    has no M6, and carrying it as a zero would drag every late cohort's curve to
    the floor and make retention look like it collapses over time when what
    collapsed is the observation window. `months_observed` bounds how far each
    cohort can be read.

    At the old end, which is the larger cut: signups begin 2024-03-11 and orders
    only on 2025-09-01, so 28,000 customers belong to cohorts whose entire first
    year happened before there is any order to see. Left in, they showed 0.0% at
    every month index - not because they churned but because their orders are not
    in the dataset - and dragged pooled retention from roughly 75% to 32%. A
    cohort whose M0 predates the order feed cannot be put on a triangle at all:
    its denominator is known and its early numerators are unobservable, so the
    curve has no anchor. Only cohorts formed on or after the first month of order
    data appear here.

    **M1 is the peak, not M0, and that is a property of signup cohorts.** These
    are keyed on the month a customer signed up rather than the month they first
    ordered, so someone who signs up on the 28th has two days of chance in M0 and
    a full month in M1. Every cohort therefore rises from M0 to M1 and decays
    from there. Keying on first-order month instead would force M0 to 100% by
    construction and hide how many signups never convert, which for an
    acquisition-quality question is the more interesting number.
#}

with bounds as (

    select
        max(date_day) as as_of_date,
        date_trunc('month', max(date_day)) as last_full_month_start,
        -- the first month any order could be observed in; cohorts older than
        -- this have no measurable M0
        date_trunc('month', min(date_day)) as first_observable_month
    from {{ ref('fct_order') }}

),

cohorts as (

    select
        cohort_month,
        count(*) as cohort_size,
        -- how many month indices this cohort has actually had the chance to
        -- live through, given where the data stops
        cast(
            date_diff(
                'month', cohort_month, (select bounds.last_full_month_start from bounds)
            )
            as integer
        ) as months_observed
    from {{ ref('mart_customer_360') }}
    where cohort_month >= (select bounds.first_observable_month from bounds)
    group by cohort_month

),

activity as (

    select
        customers.cohort_month,
        cast(
            date_diff('month', customers.cohort_month, date_trunc('month', orders.date_day))
            as integer
        ) as month_index,
        count(distinct orders.customer_id) as active_customers
    from {{ ref('fct_order') }} as orders
    inner join {{ ref('mart_customer_360') }} as customers
        on orders.customer_id = customers.customer_id
    group by
        customers.cohort_month,
        cast(
            date_diff('month', customers.cohort_month, date_trunc('month', orders.date_day))
            as integer
        )

),

-- driven off the oldest cohort's observable span rather than a literal: a hard
-- 0..12 silently stops extending the triangle the month the data outgrows it,
-- and a chart that stops growing looks like a flat tail rather than a missing one
indices as (

    select unnest(
        generate_series(0, (select max(cohorts.months_observed) from cohorts))
    ) as month_index

),

-- a dense grid so a month a cohort genuinely went quiet reads as zero rather
-- than as a hole the chart interpolates over
grid as (

    select
        cohorts.cohort_month,
        cohorts.cohort_size,
        cohorts.months_observed,
        cast(indices.month_index as integer) as month_index
    from cohorts
    cross join indices
    where indices.month_index <= cohorts.months_observed

)

select
    grid.cohort_month,
    grid.month_index,
    grid.cohort_size,
    grid.months_observed,
    coalesce(activity.active_customers, 0) as active_customers,
    coalesce(activity.active_customers, 0) / cast(grid.cohort_size as double) as retention_rate
from grid
left join activity
    on
        grid.cohort_month = activity.cohort_month
        and grid.month_index = activity.month_index
