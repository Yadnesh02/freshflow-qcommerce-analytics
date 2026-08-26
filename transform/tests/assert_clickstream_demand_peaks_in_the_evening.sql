{#
    Defect 6, and the test docs/known_data_issues.md asks for by name: "a test
    that the hourly demand curve peaks in the evening catches this immediately".

    It works because the two candidate readings of this feed disagree
    violently, not marginally. Conformed to IST, 17:00-22:00 is the busiest
    stretch of a q-commerce day and 00:00-04:00 is dead. Left in UTC and
    labelled as if it were local, those windows land on IST 22:30-03:30 and
    IST 05:30-09:30 - so the dead window fills with the morning grocery peak
    and the busy one empties. The inequality does not narrow, it inverts.

    Asserting on blocks rather than on a single peak hour is deliberate: the
    catalogue mixes a morning curve (dairy, bakery, fruit) with an evening one
    (snacks, beverages), so which single hour wins depends on category mix and
    would make this test a tripwire for merchandising changes rather than for
    a timezone bug.
#}

with hourly as (

    select
        event_hour_ist,
        count(*) as events
    from {{ ref('stg_web__clickstream') }}
    group by event_hour_ist

),

blocks as (

    select
        sum(case when event_hour_ist between 17 and 22 then events else 0 end) as evening,
        sum(case when event_hour_ist between 0 and 4 then events else 0 end) as small_hours
    from hourly

)

select
    evening,
    small_hours
from blocks
where evening <= small_hours
