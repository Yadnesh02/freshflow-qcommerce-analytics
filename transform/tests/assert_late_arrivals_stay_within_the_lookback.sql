{#
    Defect 2. Orders land in a partition up to 48h after they happened, and the
    48h lookback in agg_store_sku_day (S2.6) is sized against exactly that.

    This test is what stops the lookback and the data drifting apart. If the
    feed ever delivers something three days late, the incremental will silently
    miss it - the model would still build, still reconcile against itself, and
    still be wrong. Better to fail here, where the number that needs changing
    is one constant away.

    A negative lag is a different failure: it means a row arrived in a
    partition before the event happened, which is not lateness but a clock
    problem, and it is caught by the same bound.
#}

select
    order_id,
    order_date_ist,
    arrival_date,
    arrival_lag_days
from {{ ref('stg_pos__orders') }}
where arrival_lag_days < 0 or arrival_lag_days > 2
