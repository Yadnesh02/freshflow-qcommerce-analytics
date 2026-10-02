{#
    The deal rail's customer-level holdout, as assigned (task D1).

    One row per customer, not per customer per day: the arm is decided once at
    the start of the window and never revised. That is deliberate and it is what
    makes the retention question in D3 answerable - the effect the rail is meant
    to buy accumulates over months, and an arm that churned weekly would mix
    treated and untreated history inside the same person.

    `assigned_date` is carried rather than assumed so that a later design which
    does stagger assignment does not silently reinterpret this one. Nothing
    downstream should join on `dt`: the feed is emitted once, so the partition
    date says when it was written, not when the assignment applied.
#}

select
    customer_id,
    deal_arm,
    deal_arm = 'exposed' as is_deal_exposed,
    assigned_date,
    holdout_share
from {{ source('crm', 'crm_deal_exposure') }}
