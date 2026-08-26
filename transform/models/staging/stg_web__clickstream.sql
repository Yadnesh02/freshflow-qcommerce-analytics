{#
    Browsing events, conformed to IST (defect 6) and to the catalogue's SKU
    format (defect 7).

    This feed carries the most valuable column in the dataset and three of the
    eight defects at once, which is not a coincidence: event collectors are
    where schema discipline is weakest.

    **Why it is worth repairing rather than dropping.** `was_in_stock = false`
    is the only record of demand that existed while stock was zero. Sales data
    cannot distinguish "nobody wanted it" from "we had none", so a forecast
    trained on sales alone under-orders the fast movers forever. The
    censored-demand correction in Sprint 3 reads this model.

    **Timezone (defect 6).** The collector writes UTC, the POS writes IST, and
    nothing in either feed says so. Both `event_ts_utc` and `event_ts_ist` are
    published, suffixed, so no downstream model has to guess - and the raw
    `event_date_utc` is kept alongside `event_date_ist` because the gap between
    them is the bug: every event before 05:30 IST is filed under the previous
    day, which drags the evening demand peak into the afternoon.

    **Identifier migration (defect 7).** Handled by the normalised_sku_id
    macro. `sku_id_was_conformed` records where it fired, so the count can be
    checked against the defect log rather than taken on trust.

    **The outage (defect 8) is not repaired here, because it cannot be.** Two
    partitions are missing entirely and no amount of staging invents them. What
    staging can do is refuse to hide it: this model never fills gaps, and the
    per-day row-count check that fails the build on a missing partition lands
    in S2.7. Interpolating would be worse than useless - the collector fell
    over under load, so the missing days are two of the busiest of the year and
    any average would be biased low exactly where stockouts were worst.

    **Not deduplicated.** The feed has no event id, so two genuine impressions
    of the same SKU in the same hour are legitimately identical rows. Defect 1
    duplicates the order feeds, not this one; applying a row hash here would
    silently delete real events to fix a problem that does not exist.
#}

with source as (

    select * from {{ source('web', 'clickstream') }}

),

conformed as (

    select
        store_id,
        event_type,
        was_in_stock,
        event_ts_utc,
        event_date as event_date_utc,
        dt as arrival_date,

        -- defect 7: SKU_42 and SKU-00042 are the same product
        sku_id as sku_id_raw,
        {{ normalised_sku_id('sku_id') }} as sku_id,

        -- defect 6: the collector writes UTC, everything else writes IST
        {{ to_ist('event_ts_utc') }} as event_ts_ist

    from source

)

select
    store_id,
    sku_id,
    event_type,
    was_in_stock,

    -- demand that existed and could not be served: the uncensoring signal
    not was_in_stock as is_censored_demand,

    event_ts_ist,
    cast(event_ts_ist as date) as event_date_ist,
    extract(hour from event_ts_ist) as event_hour_ist,

    event_ts_utc,
    event_date_utc,
    sku_id <> sku_id_raw as sku_id_was_conformed,
    arrival_date
from conformed
