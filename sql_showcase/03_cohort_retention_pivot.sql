-- question: Of the customers who first ordered in a given month, what share are
--           still ordering one, two and three months later - and is retention
--           improving for cohorts we acquired more recently?
-- technique: PIVOT into a cohort triangle, with the unobservable cells left
--            null rather than filled with zero
-- needs_days: 120
--
-- **The empty half of the triangle is the point.** A cohort acquired in the
-- final month of the window has no month-three cell, because month three has
-- not happened - not because nobody came back. Writing 0 there is the single
-- most common way a cohort chart lies: it drags the recent cohorts' curves to
-- the floor and produces a confident story about retention collapsing that is
-- entirely an artefact of the window ending. `months_observed` carries how far
-- each cohort could possibly be measured, and cells beyond it stay null.
--
-- Read down a column, not across a row: comparing month_index 1 across cohorts
-- is the like-for-like question ("is what we acquired this month better?").
-- Comparing across a row mixes cohort age with calendar time.

with observable as (

    select
        cohort_month,
        month_index,
        cohort_size,
        months_observed,
        -- null, not zero, once the window runs out
        case when month_index <= months_observed then retention_rate end as retention_rate
    from marts.mart_cohort_retention
    where month_index between 1 and 6

),

triangle as (

    pivot observable
    on month_index in (1, 2, 3, 4, 5, 6)
    using first(retention_rate)
    group by cohort_month, cohort_size, months_observed

)

select
    cohort_month,
    cohort_size,
    months_observed,
    round(100 * "1", 1) as m1_pct,
    round(100 * "2", 1) as m2_pct,
    round(100 * "3", 1) as m3_pct,
    round(100 * "4", 1) as m4_pct,
    round(100 * "5", 1) as m5_pct,
    round(100 * "6", 1) as m6_pct
from triangle
order by cohort_month;
