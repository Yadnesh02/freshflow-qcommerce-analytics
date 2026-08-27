{#
    Every daily feed must have a row for every day, except the clickstream
    outage we have declared.

    This is the check defect 8 says is the only defence: "missing partitions
    must fail a check, not average to zero". A gap does not raise an error
    anywhere - the rows are simply absent, so every average over the window
    divides by a smaller denominator and returns a smaller, entirely plausible
    number. The collector fell over under load, so the two days it lost are two
    of the busiest of the year, and the bias runs in the worst direction:
    demand looks lower exactly where stockouts were worst.

    The declared tolerance lives in dbt_project.yml as a var rather than as a
    number typed into this file. Widening it is then a deliberate edit to a
    commented constant, visible in a diff, rather than a threshold someone
    nudged until the build went green.

    The clickstream is allowed exactly its documented outage. Every other daily
    feed is allowed nothing.

    Staleness is checked alongside, because a feed that simply stops arriving
    leaves no gap to find - its last partition is just older than everyone
    assumes, and every "latest" figure quietly reports a number from days ago.
#}

with gaps as (

    select
        source_name,
        count(*) as missing_days,
        min(date_day) as first_missing_day,
        max(date_day) as last_missing_day
    from {{ ref('dq_source_coverage') }}
    where is_missing_partition
    group by source_name

)

select
    source_name,
    missing_days,
    first_missing_day,
    last_missing_day,
    case
        when source_name = 'clickstream'
            then {{ var('documented_clickstream_outage_days') }}
        else 0
    end as declared_tolerance_days
from gaps
where
    missing_days > case
        when source_name = 'clickstream'
            then {{ var('documented_clickstream_outage_days') }}
        else 0
    end

union all by name

select
    source_name,
    0 as missing_days,
    max(last_seen_date) as first_missing_day,
    max(last_seen_date) as last_missing_day,
    0 as declared_tolerance_days
from {{ ref('dq_source_coverage') }}
where is_stale
group by source_name
