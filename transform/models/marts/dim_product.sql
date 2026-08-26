{#
    The product dimension, at current state - one row per SKU.

    Current state, deliberately. History lives in dim_product_snapshot, and the
    division of labour is the point: anything asking "what is this product"
    reads here, anything asking "what did it cost in March" reads there. A
    single table trying to do both ends up either restating history or making
    every lookup a range join.

    Attributes are taken from the SKU's latest catalogue snapshot rather than
    from any particular day, which is what "current" has to mean for a feed
    that arrives daily.

    `abc_class` and `xyz_class` are inferred from the sales the SKU actually
    produced - see int_product_sales_profile for how, and why the pair matters
    more than either alone. They are the only attributes here that depend on
    facts, which makes this dimension depend on the order feed; that is fine
    and one-directional, because facts carry sku_id and never read back.
#}

with latest_snapshot as (

    select *
    from {{ ref('stg_catalog__products') }}
    qualify row_number() over (partition by sku_id order by snapshot_date desc) = 1

),

profile as (

    select * from {{ ref('int_product_sales_profile') }}

)

select
    latest_snapshot.sku_id,
    latest_snapshot.sku_name,
    latest_snapshot.brand,
    latest_snapshot.is_private_label,
    latest_snapshot.l1_category,
    latest_snapshot.l2_subcategory,
    latest_snapshot.temp_zone,

    latest_snapshot.uom,
    latest_snapshot.base_uom,
    latest_snapshot.pack_qty,
    latest_snapshot.pack_size_base_units,
    latest_snapshot.shelf_life_days,

    latest_snapshot.mrp,
    latest_snapshot.base_price,
    latest_snapshot.landed_cost,
    latest_snapshot.unit_margin,
    latest_snapshot.gross_margin_pct,
    latest_snapshot.gst_rate,
    latest_snapshot.deal_eligible as deal_eligible_flag,

    -- inferred from sales, not carried by the catalogue
    profile.abc_class,
    profile.xyz_class,
    profile.abc_class || profile.xyz_class as abc_xyz_class,
    profile.has_sales,
    profile.net_revenue as net_revenue_ltd,
    profile.net_units as net_units_ltd,
    profile.selling_days,
    profile.demand_cv,

    latest_snapshot.snapshot_date as attributes_as_of_date
from latest_snapshot
left join profile on latest_snapshot.sku_id = profile.sku_id
