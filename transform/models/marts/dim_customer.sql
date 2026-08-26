{#
    One row per customer, from the latest monthly snapshot.

    **Latest, not first.** `churn_date` is only populated once the hazard has
    fired, so the September snapshot does not yet know a customer churned in
    March. Reading current state means reading the most recent snapshot; the
    full history stays on stg_crm__customers for anything reasoning about what
    was knowable at a point in time - which cohort and survival analysis in
    Sprint 4 must do, or it will leak the future into its own training data.

    Behavioural attributes - RFM, discount dependency, segment - are not here.
    They belong to mart_customer_360, which is built from order history rather
    than from the CRM record, and the split is deliberate: this table is what
    the source system asserts, that one is what the behaviour shows.

    `signup_cohort_month` is the one derived column, because every retention
    chart starts by grouping on it and computing it in each of them is how two
    charts end up disagreeing about the same cohort.
#}

with latest_snapshot as (

    select *
    from {{ ref('stg_crm__customers') }}
    qualify row_number() over (partition by customer_id order by snapshot_date desc) = 1

)

select
    customer_id,
    home_store_id,
    acquisition_channel,
    device,
    is_member,

    signup_date,
    cast(date_trunc('month', signup_date) as date) as signup_cohort_month,

    churn_date,
    churn_date is not null as is_churned,
    date_diff('day', signup_date, churn_date) as days_to_churn,

    snapshot_date as attributes_as_of_date
from latest_snapshot
