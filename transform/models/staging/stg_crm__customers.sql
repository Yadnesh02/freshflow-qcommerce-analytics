{#
    The customer master, snapshotted monthly rather than as a current-state
    table.

    Keeping every snapshot is what makes churn measurable. `churn_date` is
    populated only once the hazard model has fired, so the December snapshot
    knows things the September one did not, and a single current-state extract
    would let that knowledge leak backwards into any cohort analysis built on
    it. dim_customer (S2.3) picks the latest snapshot; anything reasoning about
    a point in time reads this.
#}

select
    customer_id,
    home_store_id,
    acquisition_channel,
    device,
    is_member,
    signup_date,
    churn_date,
    dt as snapshot_date
from {{ source('crm', 'customer_snapshot') }}
