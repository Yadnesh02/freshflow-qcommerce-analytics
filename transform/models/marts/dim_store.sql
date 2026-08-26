{#
    The 14 dark stores.

    `category_affinity` is unpacked here rather than in staging, because
    deciding which of the nine categories deserve their own column is a
    merchandising judgement and staging's job was only to type and clean. The
    three kept are the ones the expiry problem lives in - the perishable
    categories whose affinity explains why two stores with the same order
    volume write off different amounts. The full map stays available on the
    staging model for anything that needs all nine.

    Capacity arrives here only because building this model found that it did
    not. `stores.yaml` declares network-wide capacity and serviceable radius in
    a `defaults` block and overrides it per store, and nothing merged the two -
    so 12 of 14 stores emitted a null chilled capacity and the radius never
    reached the feed at all. Nothing noticed because the simulator does not read
    those values itself; the first consumer was `chilled_capacity_share` below,
    which came back null for the entire network.

    `total_capacity_units` counts chilled and ambient only. Frozen is a
    separate physical constraint - a frozen SKU cannot be stored in an ambient
    bay - and adding the three into one number would invent headroom that does
    not exist.
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
    serviceable_radius_km,

    chilled_capacity_units,
    ambient_capacity_units,
    frozen_capacity_units,
    chilled_capacity_units + ambient_capacity_units as total_capacity_units,
    chilled_capacity_units / nullif(
        cast(chilled_capacity_units + ambient_capacity_units as double), 0
    ) as chilled_capacity_share,

    category_affinity['Dairy & Eggs'] as affinity_dairy_eggs,
    category_affinity['Fruits & Vegetables'] as affinity_fruits_vegetables,
    category_affinity['Ready to Eat & Frozen'] as affinity_ready_to_eat,

    opened_date
from {{ ref('stg_ref__stores') }}
