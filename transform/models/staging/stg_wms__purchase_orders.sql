{#
    Replenishment orders and what actually turned up against them.

    The two derived columns are the ones the supplier scorecard is built from:
    `fill_rate` is how much of what was ordered arrived, and `lead_time_days`
    is how long it took. Both are computed here rather than downstream because
    a short delivery and a late delivery are different failures and get
    confused constantly - `is_short` and `is_late` already exist in the feed,
    but neither says by how much.
#}

select
    po_id,
    store_id,
    sku_id,
    supplier_id,
    ordered_qty,
    received_qty,
    case
        when ordered_qty > 0 then received_qty / cast(ordered_qty as double)
    end as fill_rate,
    ordered_qty - received_qty as shortfall_qty,
    inbound_freshness_pct,
    ordered_date,
    expected_date,
    received_date,
    date_diff('day', ordered_date, received_date) as lead_time_days,
    date_diff('day', expected_date, received_date) as days_late,
    is_short,
    is_late,
    dt as arrival_date
from {{ source('wms', 'wms_purchase_orders') }}
