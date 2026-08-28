{#
    One row per customer: RFM, cohort, discount dependency and contribution (S3.5).

    Problem P7 in the plan is that retention is measured and not managed, and
    that nobody knows whether clearance deals build habit or train discount
    seekers. This table is where that question becomes answerable, because it
    puts a customer's value and their discount dependency on the same row. A
    cohort with strong GMV and a high DDI is not a good cohort; it is a
    subsidised one, and the two are indistinguishable until you divide.

    **Everything trails a fixed as-of date rather than "today".** The warehouse
    ends on a known day and a customer's recency has to be measured from it,
    not from whenever the model happens to run - otherwise every rebuild moves
    every customer's segment and no two runs of the dashboard agree. `as_of_date`
    is published on the row so a reader can see what "90 days" was measured
    against.

    **The two 90-day windows are adjacent, not overlapping.** `active_curr_90d`
    covers the last 90 days and `active_prev_90d` the 90 before that, because
    `retention_90d` divides one by the other. Overlapping them would count the
    same order on both sides and produce a retention figure that cannot fall
    below the share of customers who ordered once in the middle.

    **Frequency is measured over the customer's lifetime, not the 90-day
    window, and the segmentation only works because of it.** Recency and a
    90-day order count are two readings of the same window: their correlation
    here is -0.55, so a customer cannot have a low recency score and a high
    frequency score at once. That combination is precisely the definition of
    `at_risk` - someone who used to buy often and has stopped - so scoring F on
    the recent window makes the most actionable segment in the grid unreachable
    by construction. Lifetime frequency correlates -0.37 with recency instead,
    which leaves room for the two to disagree, and disagreement is the whole
    information content of an RF grid.

    **Scores use `cume_dist`, not `ntile`, and the column they are computed on
    has to have no tie block.** `ntile` divides by row count and is blind to
    ties, so it would split a block of identical customers across score bands by
    sort position alone and put two identical customers in different segments.
    `cume_dist` gives tied rows one value - but that only helps if no single
    value dominates. Measured here, `orders_90d` is exactly zero for 41.3% of the
    scored base, which puts that entire block at `ceil(5 x 0.413) = 3` and makes
    frequency scores of 1 and 2 unreachable: every lapsed customer scored 3 out
    of 5 for frequency, and `hibernating` could never fire. `orders_lifetime`
    has no such block - its largest single value, six orders, holds 3.65% - so
    the quintiles it produces are real ones. That is the second reason F is a
    lifetime figure.

    Monetary correlates 0.89 with frequency, as it does in every RFM cut ever
    made, so it is published for slicing but deliberately not used to assign
    segments; those come off the RF grid alone.

    The cost of a lifetime count is that it carries tenure inside it: a customer
    who joined a month ago cannot out-order one who joined a year ago even at
    twice the rate, so recent cohorts sit lower on F than their behaviour
    deserves. Normalising to orders-per-month-active would remove that and
    introduce a worse problem - a single-order customer who signed up last week
    scores infinitely well - so the confound is kept and named rather than
    traded for one that is harder to see. Read F as "how much has this customer
    bought from us", which is what it is, rather than as a rate.

    **`delivery_cost_90d` is an assumption, it is the only one in this table,
    and the headline conclusion turns on it.** The event stream has no
    fulfilment cost in it, so `assumed_delivery_cost_per_order` in
    dbt_project.yml supplies a flat per-order figure, currently 42 rupees.

    That number is not a detail. Contribution by discount-dependency band, which
    is the answer P7 exists to produce, reorders itself around it:

        delivery Rs/order    low-DDI   medium-DDI   high-DDI   best
                        0       1073          775        486   low
                       20        747          657        436   low
                       30        584          598        412   medium
                       42        388          527        382   medium
                       60         94          421        338   medium
                       70        -69          362        313   medium

    Low-DDI customers order 16.3 times a quarter against 2.5 for the high-DDI
    band, so a per-order cost falls on them hardest, and the two curves cross at
    28.6 rupees. The assumption in use sits 47% above that crossover - close
    enough that "our least discount-dependent customers are the most valuable"
    is a statement about the delivery cost, not about the customers. At 70
    rupees the low-DDI band goes negative outright.

    The honest reading is therefore narrower than the P7 hypothesis expects:
    high-DDI customers here are not value-destroying, they are simply small -
    2.5 orders and 2,022 rupees of GMV against 16.3 and 4,152. What the data
    supports is that discount dependency tracks *low engagement*, not negative
    contribution. Saying otherwise would require a delivery cost this dataset
    cannot supply.

    Dropping delivery entirely is worse than a labelled assumption - it would
    put low-DDI customers on top by a wide margin and make that look like a
    finding. `test_the_ddi_conclusion_is_disclosed_as_assumption_sensitive`
    fails if the crossover ever leaves the plausible range without this note
    being revisited.
#}

{%- set delivery_cost = var('assumed_delivery_cost_per_order') -%}

with bounds as (

    select
        max(date_day) as as_of_date,
        max(date_day) - 89 as curr_window_start,
        max(date_day) - 179 as prev_window_start,
        date_trunc('month', min(date_day)) as first_order_month,
        date_trunc('month', max(date_day)) as last_order_month
    from {{ ref('fct_order') }}

),

orders_scoped as (

    select
        orders.customer_id,
        orders.store_id,
        orders.order_id,
        orders.date_day,
        orders.gmv,
        orders.gross_margin,
        orders.discount_total,
        orders.has_promotion,
        bounds.as_of_date,
        orders.date_day >= bounds.curr_window_start as in_curr_window,
        orders.date_day between bounds.prev_window_start and bounds.curr_window_start - 1
            as in_prev_window
    from {{ ref('fct_order') }} as orders
    cross join bounds

),

per_customer as (

    select
        customer_id,
        min(as_of_date) as as_of_date,

        count(*) as orders_lifetime,
        sum(gmv) as gmv_lifetime,
        min(date_day) as first_order_date,
        max(date_day) as last_order_date,
        min(as_of_date) - max(date_day) as recency_days,

        count(*) filter (where in_curr_window) as orders_90d,
        count(*) filter (where in_curr_window and has_promotion) as promo_orders_90d,
        coalesce(sum(gmv) filter (where in_curr_window), 0) as gmv_90d,
        coalesce(sum(gross_margin) filter (where in_curr_window), 0) as gross_margin_90d,
        coalesce(sum(discount_total) filter (where in_curr_window), 0) as subsidy_90d,

        count(*) filter (where in_prev_window) as orders_prev_90d
    from orders_scoped
    group by customer_id

),

-- the whole customer base, including people who have never ordered: a signup
-- that never converted is a retention fact, and dropping it would compute
-- cohort retention over the subset that already retained
base as (

    select
        customers.customer_id,
        customers.home_store_id as store_id,
        customers.signup_date,
        customers.signup_cohort_month as cohort_month,
        customers.acquisition_channel,
        customers.is_member,

        coalesce(per_customer.as_of_date, bounds.as_of_date) as as_of_date,
        coalesce(per_customer.orders_lifetime, 0) as orders_lifetime,
        coalesce(per_customer.gmv_lifetime, 0) as gmv_lifetime,
        per_customer.first_order_date,
        per_customer.last_order_date,
        per_customer.recency_days,

        coalesce(per_customer.orders_90d, 0) as orders_90d,
        coalesce(per_customer.promo_orders_90d, 0) as promo_orders_90d,
        coalesce(per_customer.gmv_90d, 0) as gmv_90d,
        coalesce(per_customer.gross_margin_90d, 0) as gross_margin_90d,
        coalesce(per_customer.subsidy_90d, 0) as subsidy_90d,
        coalesce(per_customer.orders_prev_90d, 0) as orders_prev_90d,

        -- Whether this customer's first full month after signup falls inside
        -- the order feed at all. Signups begin 2024-03 and orders only
        -- 2025-09, so for 18 of 30 cohorts M1 happened where there is nothing
        -- to see - and `ordered_m1` is false for every one of them, which makes
        -- retention_m1 read a clean 0.0000 and pass its own [0,1] range test
        -- while describing the dataset's start date rather than any customer.
        customers.signup_cohort_month >= bounds.first_order_month
        and customers.signup_cohort_month + interval 1 month <= bounds.last_order_month
            as m1_observable
    from {{ ref('dim_customer') }} as customers
    left join per_customer
        on customers.customer_id = per_customer.customer_id
    cross join bounds

),

-- M1 is the first *full* calendar month after signup, so a customer who signs
-- up on the 30th is not judged on two days of opportunity
first_month as (

    select
        orders_scoped.customer_id,
        bool_or(
            date_trunc('month', orders_scoped.date_day)
            = date_trunc('month', base.signup_date) + interval 1 month
        ) as ordered_m1
    from orders_scoped
    inner join base
        on orders_scoped.customer_id = base.customer_id
    group by orders_scoped.customer_id

),

scored as (

    select
        base.*,
        coalesce(first_month.ordered_m1, false) as ordered_m1,

        base.orders_90d > 0 as active_curr_90d,
        base.orders_prev_90d > 0 as active_prev_90d,

        -- Subsidy is deliberately NOT subtracted here, and the temptation to is
        -- strong because the metric registry originally said to. gross_margin
        -- is built in fct_order_item as qty * (unit_realized_price - unit_cogs),
        -- and unit_realized_price is the price after discount - measured over
        -- this warehouse, base - realized - discount is exactly 0.00. The
        -- discount is therefore already inside the margin, and subtracting
        -- subsidy again charged it twice: it understated total contribution by
        -- 11.7%, and unevenly, hitting the discount-heavy customers hardest -
        -- which is precisely the population the P7 comparison is about.
        base.gross_margin_90d - (base.orders_90d * {{ delivery_cost }}) as contribution_90d,

        case
            when base.orders_90d > 0
                then base.promo_orders_90d / cast(base.orders_90d as double)
        end as discount_dependency_index,

        -- Quintiles over the ordering base only. Scoring never-ordered
        -- customers alongside them would put a block of zeroes at the bottom of
        -- every scale and make it describe signups rather than value; they are
        -- given no score at all and a segment of their own.
        --
        -- `order by recency_days desc` so that the most recent customer, whose
        -- recency is smallest, sorts last and takes the highest cume_dist.
        case
            when base.orders_lifetime > 0
                then cast(ceil(5 * cume_dist() over (
                    partition by base.orders_lifetime > 0 order by base.recency_days desc
                )) as integer)
        end as r_score,
        case
            when base.orders_lifetime > 0
                then cast(ceil(5 * cume_dist() over (
                    partition by base.orders_lifetime > 0 order by base.orders_lifetime
                )) as integer)
        end as f_score,
        case
            when base.orders_lifetime > 0
                then cast(ceil(5 * cume_dist() over (
                    partition by base.orders_lifetime > 0 order by base.gmv_lifetime
                )) as integer)
        end as m_score
    from base
    left join first_month
        on base.customer_id = first_month.customer_id

)

select
    customer_id,
    store_id,
    cohort_month,
    as_of_date,
    acquisition_channel,
    is_member,

    signup_date,
    first_order_date,
    last_order_date,
    recency_days,

    orders_lifetime,
    gmv_lifetime,

    orders_90d,
    orders_prev_90d,
    promo_orders_90d,
    gmv_90d,
    gross_margin_90d,
    subsidy_90d,

    -- flat per-order assumption; see the header and dbt_project.yml
    orders_90d * {{ delivery_cost }} as delivery_cost_90d,
    contribution_90d,

    active_curr_90d,
    active_prev_90d,
    ordered_m1,

    r_score,
    f_score,
    m_score,
    -- the raw grid position, which dimensions.yml declares as rfm_segment.
    -- Distinct from customer_segment: this is the coordinate, that is the name
    -- given to the region it falls in.
    case
        when r_score is not null
            then cast(r_score as varchar) || cast(f_score as varchar) || cast(m_score as varchar)
    end as rfm_segment,
    m1_observable,
    discount_dependency_index,

    case
        when discount_dependency_index is null then 'no_orders'
        when discount_dependency_index >= 0.6 then 'high'
        when discount_dependency_index >= 0.25 then 'medium'
        else 'low'
    end as ddi_band,

    -- Segments are named for the action they imply, not for the scores that
    -- produced them. A reader deciding what to do with a cohort should not have
    -- to hold the RFM cube in their head.
    case
        when orders_lifetime = 0 then 'never_ordered'

        -- Absence from the scoring window is checked before the quintiles, not
        -- after. r_score is a *relative* position in the recency distribution,
        -- so a customer 92 days out lands mid-band and used to come out
        -- 'loyal' on a row that also read orders_90d = 0. 797 customers sat in
        -- that gap, between the 90-day window edge and the third quintile
        -- boundary at 93 days. A label that contradicts the columns beside it
        -- is worse than a coarse one.
        when not active_curr_90d and f_score >= 3 then 'at_risk'
        when not active_curr_90d then 'hibernating'

        when r_score >= 4 and f_score >= 4 then 'champion'
        when r_score >= 4 and f_score <= 2 then 'promising'
        when r_score >= 3 and f_score >= 3 then 'loyal'
        when r_score <= 2 and f_score >= 3 then 'at_risk'
        when r_score <= 2 and f_score <= 2 then 'hibernating'
        else 'needs_attention'
    end as customer_segment
from scored
