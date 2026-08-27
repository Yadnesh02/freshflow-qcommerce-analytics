{#
    The reconciliation test. Two systems that never spoke to each other have to
    agree about how much stock left the building.

    The POS records what it sold. The WMS records what left each batch. Nothing
    in the pipeline forces those to match - they come from different feeds, are
    keyed differently, and are joined nowhere before this - so agreement here is
    evidence the warehouse is right rather than merely self-consistent. It is
    the one test in the suite that could fail because the *data* is wrong rather
    than because a model is.

    **It is allowed not to balance, by exactly one documented amount.** Defect 3
    strips the batch reference off ~1% of movements, and a movement with no
    batch cannot be attributed to a store or a SKU - so those sales exist in the
    POS and are missing from the ledger. stg_quarantine holds them with the
    quantity they removed, and that quantity is the entire permitted gap:

        POS gross sale units - WMS sale movements = quarantined sale units

    Exactly, to the unit. A residual larger than the quarantine means something
    else is broken too, which is the failure a plain "does it reconcile?" check
    hides by reporting a number that is merely close.

    **Gross, not net.** Returns are the trap here. The POS posts them as
    negative lines, but the ledger has no event type that puts a unit back -
    `assert_returns_never_re_enter_the_stock_ledger` documents that separately.
    Reconciling net sales against the ledger would therefore be off by the
    returned quantity and would look like a reconciliation failure, when it is
    a modelling gap in a different place entirely.

    **Value is bounded rather than equated, and that is not a weaker claim - it
    is the honest one.** A quarantined movement has no batch, so it has no unit
    cost: the rupee value of the gap is genuinely unknowable and any test
    asserting it exactly would be asserting a number it invented. What can be
    checked is that the implied cost per missing unit falls inside the range of
    unit costs the catalogue actually contains, which is what catches a gap of
    the right size made of the wrong things.
#}

with pos_sales as (

    select
        sum(units) as units,
        sum(cogs) as cogs
    from {{ ref('fct_order_item') }}
    where line_type = 'sale'

),

wms_sales as (

    select
        -sum(qty_delta) as units,
        -sum(movement_cost_value) as cogs
    from {{ ref('fct_inventory_movement') }}
    where event_type = 'sale'

),

quarantined as (

    select coalesce(sum(impact_units), 0) as units
    from {{ ref('stg_quarantine') }}
    where
        reason_code = 'missing_batch_reference'
        and json_extract_string(payload, 'event_type') = 'sale'

),

cost_range as (

    select
        min(unit_landed_cost) as lowest_unit_cost,
        max(unit_landed_cost) as highest_unit_cost
    from {{ ref('fct_inventory_batch') }}

),

reconciliation as (

    select
        pos_sales.units as pos_units,
        wms_sales.units as wms_units,
        quarantined.units as quarantined_units,
        pos_sales.units - wms_sales.units as unit_gap,
        pos_sales.cogs - wms_sales.cogs as value_gap,
        case
            when quarantined.units > 0
                then (pos_sales.cogs - wms_sales.cogs) / quarantined.units
        end as implied_cost_per_missing_unit,
        cost_range.lowest_unit_cost,
        cost_range.highest_unit_cost
    from pos_sales
    cross join wms_sales
    cross join quarantined
    cross join cost_range

)

select *
from reconciliation
where
    -- units must agree exactly once the quarantine is accounted for
    unit_gap <> quarantined_units
    -- and the value of what went missing must be made of real products
    or implied_cost_per_missing_unit is null
    or implied_cost_per_missing_unit < lowest_unit_cost
    or implied_cost_per_missing_unit > highest_unit_cost
