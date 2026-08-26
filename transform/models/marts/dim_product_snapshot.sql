{#
    Slowly changing dimension, type 2, on landed cost and base price.

    **Why this matters.** Margin on a March order has to use March's cost.
    Joining the live catalogue instead restates history every time
    merchandising renegotiates a supplier price - the March order silently
    reprices at August's cost and the whole margin series shifts under you.
    321 of the 1,500 SKUs change cost during the year, so this is not a
    hypothetical.

    **Why this is a model and not a dbt snapshot**, which is what a warehouse
    of this shape usually reaches for. Two reasons, and both are decisive here:

    1. A dbt snapshot expects the source to be current state - one row per key
       - and captures change by being run repeatedly over time. This source is
       already a full daily history, 365 rows per SKU. Pointed at it, a
       snapshot either errors on the duplicate key or records 365 open
       versions per SKU. Running it once a day for a year would work, and the
       year is already on disk.

    2. A snapshot is stateful: its table is built by accumulation and cannot be
       reconstructed from the source. On a fresh clone, `dbt build
       --full-refresh` would produce exactly one version per SKU and the
       history would be gone - so the repo would no longer reproduce its own
       results, which for a project whose whole claim is reproducibility is not
       a trade worth making.

    Deriving the versions from the history gives the same table, deterministic
    and rebuildable, and keeps dbt's column names so nothing downstream can
    tell the difference. If this ever moves to a real streaming catalogue feed,
    the honest migration is to backfill this table once and hand the
    going-forward job to a snapshot.

    **A version is a maximal run of days on which neither tracked value
    changed.** Not a distinct-value count: if a price moves A - B - A that is
    three versions, not two, and counting distinct values would merge the first
    and third into one interval spanning a period when the price was something
    else. It does not happen in this dataset, and the logic that would notice
    costs nothing.

    **Intervals are half-open, [valid_from, valid_to).** The next version
    begins exactly where the previous one ends, so a point-in-time lookup
    matches exactly one row and never two. A singular test asserts that on
    every date that actually has a sale.
#}

with daily as (

    select
        sku_id,
        snapshot_date,
        landed_cost,
        base_price
    from {{ ref('stg_catalog__products') }}

),

with_previous_day as (

    select
        sku_id,
        snapshot_date,
        landed_cost,
        base_price,
        lag(landed_cost) over (partition by sku_id order by snapshot_date) as prev_landed_cost,
        lag(base_price) over (partition by sku_id order by snapshot_date) as prev_base_price
    from daily

),

flagged as (

    select
        sku_id,
        snapshot_date,
        landed_cost,
        base_price,
        -- a version starts on any day a tracked value differs from the day
        -- before, and on the first day the SKU appears at all, where the lag
        -- is null and `is distinct from` is what keeps that from being unknown
        coalesce(
            landed_cost is distinct from prev_landed_cost
            or base_price is distinct from prev_base_price,
            true
        ) as starts_a_version
    from with_previous_day

),

versioned as (

    select
        sku_id,
        snapshot_date,
        landed_cost,
        base_price,
        sum(case when starts_a_version then 1 else 0 end) over (
            partition by sku_id
            order by snapshot_date
            rows between unbounded preceding and current row
        ) as version_no
    from flagged

),

collapsed as (

    -- both values are constant within a version by construction. min() rather
    -- than any_value() so a rebuild is byte-identical rather than merely
    -- equivalent.
    select
        sku_id,
        version_no,
        min(snapshot_date) as valid_from_date,
        max(snapshot_date) as last_seen_date,
        min(landed_cost) as landed_cost,
        min(base_price) as base_price
    from versioned
    group by sku_id, version_no

),

bounded as (

    select
        sku_id,
        version_no,
        valid_from_date,
        last_seen_date,
        landed_cost,
        base_price,
        lead(valid_from_date) over (
            partition by sku_id order by version_no
        ) as valid_to_date,
        lag(landed_cost) over (partition by sku_id order by version_no) as previous_landed_cost,
        lag(base_price) over (partition by sku_id order by version_no) as previous_base_price
    from collapsed

)

select
    sku_id,
    version_no,
    {{ row_hash(['sku_id', 'valid_from_date']) }} as dbt_scd_id,

    -- dbt's snapshot column names, so a future migration to a real snapshot
    -- changes nothing downstream
    cast(valid_from_date as timestamp) as dbt_valid_from,
    cast(valid_to_date as timestamp) as dbt_valid_to,

    -- the same bounds as dates, because every join downstream is on a date
    -- and casting in five places is five chances to get it wrong
    valid_from_date,
    valid_to_date,
    last_seen_date,
    valid_to_date is null as is_current,
    date_diff('day', valid_from_date, coalesce(valid_to_date, last_seen_date + 1))
        as days_effective,

    landed_cost,
    base_price,
    base_price - landed_cost as unit_margin,
    case
        when base_price > 0 then (base_price - landed_cost) / base_price
    end as gross_margin_pct,

    -- null on a SKU's first version: there is nothing to have changed from
    landed_cost - previous_landed_cost as landed_cost_delta,
    base_price - previous_base_price as base_price_delta
from bounded
