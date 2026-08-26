{#
    The lookback is a constant. This is what keeps it honest against the data.

    An incremental run rebuilds the last `late_arrival_lookback_days` of event
    dates. That is correct only while every late-arriving order lands within
    that many days of the day it happened. One order arriving three days late
    would fall outside the window on every subsequent run - permanently
    missing, never reprocessed, and invisible because the day it belongs to
    still has rows in it.

    Measured on the gap between the event date and the partition the row
    arrived in, per store-SKU-day, which is the shape the incremental actually
    sees. assert_late_arrivals_stay_within_the_lookback checks the same bound
    on individual orders; this one checks it survives aggregation, and states
    it against the var the model reads rather than a number typed twice.
#}

select
    date_day,
    max(arrival_lag_days) as worst_lag_days,
    {{ var('late_arrival_lookback_days') }} as lookback_days,
    count(*) as orders
from {{ ref('fct_order') }}
group by date_day
having max(arrival_lag_days) > {{ var('late_arrival_lookback_days') }}
