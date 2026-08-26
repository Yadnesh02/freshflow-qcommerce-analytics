{#
    One row per store, SKU and hour with no sellable stock.

    The counterpart to the clickstream's `was_in_stock`: this feed says the
    shelf was empty, the clickstream says somebody looked anyway. Sprint 3
    joins the two to recover the demand that was never allowed to become a sale.

    Hours are IST - the WMS runs on store-local time, like the POS. Only the
    clickstream disagreed, and only because nobody told it (defect 6).
#}

select
    store_id,
    sku_id,
    event_date as stockout_date,
    hour_out as stockout_hour_ist,
    cast(event_date as timestamp) + to_hours(hour_out) as stockout_hour_ts_ist,
    dt as arrival_date
from {{ source('wms', 'wms_stockout_interval') }}
