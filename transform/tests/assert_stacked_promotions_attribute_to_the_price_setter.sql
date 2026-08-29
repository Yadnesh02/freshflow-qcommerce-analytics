{#
    When two promotions run on the same SKU on the same day, exactly one of
    them owns the units, and it has to be the one that set the price.

    33 store-SKU-days carry a markdown and the Rs 11 deal at once. On those
    days a shopper paid Rs 11 because the deal slot said so - the markdown was
    subsumed and changed nothing about what was charged - so the deal generated
    whatever response was observed and the markdown did not. Sprint 4 measures
    markdown performance and deal-slot performance in separate marts, and
    without a rule both would claim the same 132 units: the same effect counted
    twice, in two tables, each defensible on its own.

    `fct_price_history` already implements the rule - `per_day` picks the
    promotion with the greatest observed depth, which is the one that bound -
    and the model header states it. Nothing asserted it, which is the gap this
    closes. An attribution that is correct by convention rather than by
    construction is one refactor away from being wrong silently, and the
    collapse step's `min(promo_id)` reads exactly like an alphabetical
    tiebreak. It is not one - promo_id is constant inside an interval, so the
    aggregate is a no-op over a constant - but the next person to read it
    cannot know that, and if intervals ever stop breaking on promo_id the
    alphabetical reading becomes the real one. On this data it would even keep
    passing: "PROMO-DEAL11" happens to sort before "PROMO-MD-30", so the wrong
    rule and the right rule agree here and would diverge on a rename.

    The test is therefore about depth, not about ordering: on every stacked
    interval, no promotion sharing that interval may be deeper than the one
    attributed to it.

    The units at stake are small - 132 units, Rs 1,452 of revenue, and a gross
    margin of minus Rs 6,099, since this is Rs 95 stock going out at Rs 11.
    The reason to pin it anyway is that the sign is negative: double-counting a
    loss overstates how badly each lever performs on its own, and both marts
    exist precisely to judge those levers.
#}

with attributed as (

    select
        price.store_id,
        price.sku_id,
        price.interval_no,
        price.promo_id as attributed_promo_id,
        price.stacked_promo_ids,
        winner.observed_depth_pct as attributed_depth
    from {{ ref('fct_price_history') }} as price
    left join {{ ref('dim_promotion') }} as winner
        on price.promo_id = winner.promo_id
    where price.is_promo_stacked

),

-- every promotion that shared the interval, with the depth it was observed at
contenders as (

    select
        attributed.store_id,
        attributed.sku_id,
        attributed.interval_no,
        attributed.attributed_promo_id,
        attributed.attributed_depth,
        contender.promo_id as contender_promo_id,
        promotions.observed_depth_pct as contender_depth
    from attributed
    cross join unnest(attributed.stacked_promo_ids) as contender (promo_id)
    inner join {{ ref('dim_promotion') }} as promotions
        on contender.promo_id = promotions.promo_id

)

select
    store_id,
    sku_id,
    interval_no,
    attributed_promo_id,
    attributed_depth,
    contender_promo_id,
    contender_depth
from contenders
where contender_depth > attributed_depth
