{#
    The daily product catalogue, with the unit drift of defect 4 repaired.

    **The repair.** 14 SKUs report their pack size in kilograms while `uom`
    still says grams, so a 500 g pack arrives as `pack_qty = 0.5`. The rule is
    a plausibility bound, not a hardcoded SKU list: a pack measured in grams or
    millilitres cannot weigh less than one of them, so anything below 1 is a
    unit that slipped three orders of magnitude and is rescaled. The smallest
    genuine gram pack in this catalogue is 30 g and the largest drifted value
    is 0.75, so the threshold sits in a gap two orders of magnitude wide - it
    does not need to be tuned, and a new SKU cannot creep past it.

    The alternative - pinning the 14 known ids - would pass today's test and
    silently miss the fifteenth SKU the day merchandising adds one.

    **`pack_qty_was_repaired` is kept.** A repair that leaves no trace is
    indistinguishable from data that was always clean, and the number of rows
    it fires on is what the S2.1 contract test asserts against the defect log.

    **`pack_size_base_units` is why any of this matters.** Every per-kilo price
    and every weight rollup divides by it; unrepaired, those come out 1000x
    wrong for the drifted SKUs and look merely odd rather than obviously broken.

    One row per SKU per snapshot day - not deduplicated to current state,
    because the SCD2 snapshot in S2.2 needs the full history to detect the
    landed-cost and base-price changes it tracks.
#}

with source as (

    select * from {{ source('catalog', 'catalog_snapshot') }}

),

repaired as (

    select
        sku_id,
        sku_name,
        brand,
        is_private_label,
        l1_category,
        l2_subcategory,
        temp_zone,
        uom,
        shelf_life_days,
        mrp,
        base_price,
        landed_cost,
        gst_rate,
        deal_eligible,
        snapshot_date,
        dt as arrival_date,

        -- defect 4: a gram or millilitre pack below 1 arrived in kg or L
        uom in ('g', 'ml') and pack_qty < 1 as pack_qty_was_repaired,
        case
            when uom in ('g', 'ml') and pack_qty < 1 then pack_qty * 1000
            else pack_qty
        end as pack_qty

    from source

)

select
    *,
    -- lower() because the catalogue writes litres as 'L' and grams as 'g'
    case lower(uom)
        when 'kg' then 'g'
        when 'l' then 'ml'
        else lower(uom)
    end as base_uom,
    case
        when lower(uom) in ('kg', 'l') then pack_qty * 1000
        else pack_qty
    end as pack_size_base_units,
    base_price - landed_cost as unit_margin,
    case
        when base_price > 0 then (base_price - landed_cost) / base_price
    end as gross_margin_pct
from repaired
