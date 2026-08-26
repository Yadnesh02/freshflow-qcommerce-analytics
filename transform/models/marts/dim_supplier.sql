{#
    Suppliers, plus the unknown member that the batch feed needs and the source
    system does not provide.

    **Why the unknown member exists.** 15,557 batches - the stock that existed
    on day one, before any purchase order - carry `SUP-OPENING` in
    `supplier_id`. It is a sentinel, not a supplier, and `ref_suppliers` has
    never heard of it. Surfaced by the relationships test in S2.1, and it is
    the textbook case for an explicit unknown member: without one, every
    supplier rollup is an inner join that silently drops day-one stock, and
    opening inventory is not a rounding error in a business whose whole
    question is what happens to stock as it ages.

    An outer join everywhere downstream would also work and would be worse -
    it pushes the same decision into every consumer, and one of them will
    forget. One row here fixes it once, and `is_unknown_member` lets any
    analysis that genuinely wants only real suppliers exclude it deliberately
    rather than by accident.

    **Inbound freshness is summarised, not averaged away.** The mean is what
    contracts are written on; the minimum is what actually causes a write-off.
    Keeping both is what lets the supplier scorecard say SUP-DAIRY-B is cheaper
    per unit and lands materially less usable shelf life - a finding the mean
    alone states weakly and the distribution states plainly.

    `private_label_only` arrives null rather than false for every supplier that
    is not the private-label plant. Coalesced here rather than in staging,
    which is meant to stay faithful to what the feed said.
#}

with suppliers as (

    select
        supplier_id,
        supplier_name,
        categories,
        lead_time_mean_days,
        lead_time_sd_days,
        otif_rate,
        monsoon_otif_penalty,
        cost_index,
        coalesce(private_label_only, false) as is_private_label_only,
        list_avg(inbound_freshness_pct) as inbound_freshness_pct_mean,
        list_min(inbound_freshness_pct) as inbound_freshness_pct_min,
        list_max(inbound_freshness_pct) as inbound_freshness_pct_max,
        false as is_unknown_member
    from {{ ref('stg_ref__suppliers') }}

),

unknown_member as (

    select
        'SUP-OPENING' as supplier_id,
        'Opening balance (no supplier)' as supplier_name,
        cast(null as varchar) as categories,
        cast(null as double) as lead_time_mean_days,
        cast(null as double) as lead_time_sd_days,
        cast(null as double) as otif_rate,
        cast(null as double) as monsoon_otif_penalty,
        cast(null as double) as cost_index,
        false as is_private_label_only,
        cast(null as double) as inbound_freshness_pct_mean,
        cast(null as double) as inbound_freshness_pct_min,
        cast(null as double) as inbound_freshness_pct_max,
        true as is_unknown_member

)

select * from suppliers

union all by name

select * from unknown_member
