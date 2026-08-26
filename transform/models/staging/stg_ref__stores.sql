{#
    The 14 dark stores. Reference data, emitted once, no defects to repair.

    `category_affinity` stays a struct rather than being unpivoted here. It is
    a 9-key map of how much each store over- or under-indexes on a category,
    and flattening it to 9 columns in staging would push a merchandising
    decision into a layer whose job is to type and clean. dim_store (S2.3)
    decides what shape the BI layer wants.
#}

select
    store_id,
    store_name,
    locality,
    pincode,
    catchment_tier,
    lat,
    lon,
    demand_index,
    chilled_capacity_units,
    ambient_capacity_units,
    category_affinity,
    opened_date,
    dt as arrival_date
from {{ source('ref', 'ref_stores') }}
