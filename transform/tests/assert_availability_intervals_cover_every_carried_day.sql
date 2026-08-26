{#
    Every day a store carried a SKU must be covered by exactly 24 hours of
    availability state - no more, no less.

    The interval representation buys a 24x reduction in rows by asserting that
    the gaps between state changes are implied. That is only true if the states
    tile the day completely. A day summing to 19 hours has lost five hours
    somewhere, and the time-weighted percentage that divides by it will look
    entirely reasonable while being wrong - the denominator shrinks with the
    numerator, so the ratio barely moves.

    Overlaps are caught by the same sum: two states claiming the same hour push
    the total past 24.
#}

select
    store_id,
    sku_id,
    date_day,
    sum(hours_in_state) as hours_covered,
    count(*) as state_runs
from {{ ref('fct_availability_hour') }}
group by store_id, sku_id, date_day
having sum(hours_in_state) <> 24
