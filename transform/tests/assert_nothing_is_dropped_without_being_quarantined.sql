{#
    The contract that makes stg_quarantine worth having: every row staging
    refuses is either kept or counted, never neither.

    Stated per feed, because a total that balances by accident is exactly the
    reassuring number this table exists to prevent. The movement ledger is the
    one that matters - 49k rows lose their batch reference to defect 3 - but
    the returns feed is checked the same way so that the day an unmatched
    return does appear, the arithmetic notices rather than the analyst.

    Reading this test in reverse is also the fastest way to understand the
    staging layer: kept + quarantined = raw, for every feed that filters.
#}

with movements as (

    select
        'wms_inventory_movement' as source_name,
        (select count(*) from {{ source('wms', 'wms_inventory_movement') }}) as raw_rows,
        (select count(*) from {{ ref('stg_wms__inventory_movements') }}) as kept_rows,
        (
            select count(*) from {{ ref('stg_quarantine') }}
            where source_name = 'wms_inventory_movement'
        ) as quarantined_rows

),

returned as (

    select
        'pos_returns' as source_name,
        (select count(*) from {{ source('pos', 'pos_returns') }}) as raw_rows,
        (
            select count(*) from {{ ref('stg_pos__order_lines') }}
            where source_feed = 'pos_returns'
        ) as kept_rows,
        (
            select count(*) from {{ ref('stg_quarantine') }}
            where source_name = 'pos_returns'
        ) as quarantined_rows

),

audited as (

    select * from movements
    union all
    select * from returned

)

select
    source_name,
    raw_rows,
    kept_rows,
    quarantined_rows,
    raw_rows - kept_rows - quarantined_rows as unaccounted_rows
from audited
where raw_rows <> kept_rows + quarantined_rows
