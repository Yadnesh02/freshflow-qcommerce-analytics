{#
    The promotion master: what each promo is, who pays for it, and when it ran.

    **Split between a seed and the data, on purpose.** `promo_type` and
    `funding_source` are commercial facts no event feed records - nothing in
    the price ledger says whether a discount came out of the platform's margin
    or the brand's trade budget - so they are maintained in
    `seeds/promotions.csv`, the way a commercial team maintains a promo master.
    Everything else is observed: the window each promo actually ran, and the
    depth it actually landed at, both derived from the price ledger.

    That split is also a check. The seed says PROMO-MD-30 is a 30% markdown;
    the ledger says it averaged 30.04%. `contracted_depth_pct` and
    `observed_depth_pct` sitting next to each other is how a promo that was
    configured one way and executed another becomes visible instead of being
    averaged into a margin variance nobody can explain.

    **Funding source is an assumption, and worth naming as one.** Markdowns are
    platform-funded: the platform is discounting its own ageing stock, and
    GM-after-wastage subtracts exactly that subsidy. The Rs 11 deal slot is
    modelled as brand-funded, the standard q-commerce arrangement where a brand
    buys the placement. Only the first flows into GM-AWM, which is what the
    metric definition in the plan describes. If the commercial reality is that
    the platform funds the deal too, it is one cell in the seed.

    A promo appearing in the ledger but not in the seed would get no attributes
    at all, so a test asserts the seed covers everything observed rather than
    letting a new promo join to nulls.
#}

with promotions as (

    select * from {{ ref('promotions') }}

),

observed as (

    select
        promo_id,
        min(effective_date) as first_seen_date,
        max(effective_date) as last_seen_date,
        count(*) as priced_store_sku_days,
        avg(discount_pct) as observed_depth_pct,
        min(discount_pct) as observed_depth_pct_min,
        max(discount_pct) as observed_depth_pct_max
    from {{ ref('stg_catalog__price_history') }}
    where promo_id is not null
    group by promo_id

)

select
    promotions.promo_id,
    promotions.promo_type,
    promotions.funding_source,
    promotions.contracted_depth_pct,

    round(observed.observed_depth_pct, 4) as observed_depth_pct,
    round(observed.observed_depth_pct_min, 4) as observed_depth_pct_min,
    round(observed.observed_depth_pct_max, 4) as observed_depth_pct_max,
    observed.priced_store_sku_days,

    observed.first_seen_date,
    observed.last_seen_date,
    cast(observed.first_seen_date as timestamp) as start_ts,
    cast(observed.last_seen_date + 1 as timestamp) as end_ts,
    observed.promo_id is not null as was_ever_run
from promotions
left join observed on promotions.promo_id = observed.promo_id
