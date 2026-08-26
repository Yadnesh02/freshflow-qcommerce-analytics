{#
    fct_clickstream collapses 3.07M raw rows into 1.72M counted ones. That is
    only lossless while the counts add back up, and this is what says they do.

    The claim being tested is specific: identical rows carry no information
    distinguishing them, so replacing a group of them with a count describes
    them completely. If `sum(event_count)` ever came out below the raw row
    count, the grouping would have swallowed rows that were not identical -
    which is the difference between aggregating and losing data, and it is
    invisible in the output either way.

    The censored subtotal is checked separately, because it is the number the
    demand model in Sprint 3 is built on and a rollup that is right in total
    can still be wrong in its split.
#}

with raw_feed as (

    select
        count(*) as event_count,
        count(*) filter (where not was_in_stock) as censored_event_count
    from {{ source('web', 'clickstream') }}

),

fact as (

    select
        sum(event_count) as event_count,
        sum(censored_event_count) as censored_event_count
    from {{ ref('fct_clickstream') }}

)

select
    fact.event_count as fact_events,
    raw_feed.event_count as raw_events,
    fact.censored_event_count as fact_censored,
    raw_feed.censored_event_count as raw_censored
from fact
cross join raw_feed
where
    fact.event_count <> raw_feed.event_count
    or fact.censored_event_count <> raw_feed.censored_event_count
