{#
    What each store was charging for each SKU, as intervals rather than days.

    **This feed is a promotion ledger, not a full price ledger.** Every one of
    its 66,249 rows carries a promo_id and every one has realized below base -
    it records the days a discount was live and says nothing about the days it
    was not. That is worth stating plainly, because "price history" invites the
    assumption that a missing day means a missing price. It does not: a SKU
    with no row on a day was selling at its catalogue base price, which lives
    in dim_product_snapshot.

    **Intervals, per the ERD**, collapsed the same way dim_product_snapshot
    collapses cost history. A new interval starts wherever the base price, the
    realized price or the promotion changes - and also wherever a day is
    skipped, because this feed is sparse and a promo that runs Monday to
    Wednesday and again on Friday is two intervals, not one that quietly
    swallows Thursday. Bounds are half-open, [effective_from, effective_to).

    **Promotion stacking.** 33 store-SKU-days carry two promotions at once: a
    markdown and the Rs 11 deal slot firing on the same ageing SKU. Both rows
    agree on the realized price - the deal price binds - so no price
    information is lost by collapsing them, and a test asserts that agreement
    rather than assuming it. What would be lost is the knowledge that it
    happened, so `is_promo_stacked` and `stacked_promo_ids` are kept.

    That matters more than 33 rows suggests. When Sprint 4 measures markdown
    performance and deal-slot performance separately, these are the units both
    would claim - the same uplift counted twice, in two marts, each looking
    correct on its own. The primary promotion is the deepest one actually
    observed, which is the one that set the price.

    `assert_stacked_promotions_attribute_to_the_price_setter` holds that rule
    open, because until S4.3 it was stated here and enforced nowhere. Note that
    `collapsed` takes `min(promo_id)`, which reads like an alphabetical
    tiebreak and is not one - promo_id is constant inside an interval, since
    intervals break whenever it changes, so the aggregate is a no-op over a
    constant. On this data the two readings even agree, because "PROMO-DEAL11"
    sorts before "PROMO-MD-30"; they would diverge on a rename. The test is
    written against depth rather than ordering so it fails on the real bug -
    confirmed by making the attribution pick by name, which produced 28
    failing rows.
#}

with ledger as (

    select * from {{ ref('stg_catalog__price_history') }}

),

promotions as (

    select
        promo_id,
        observed_depth_pct
    from {{ ref('dim_promotion') }}

),

per_day as (

    select
        ledger.store_id,
        ledger.sku_id,
        ledger.effective_date,
        min(ledger.base_price) as base_price,
        min(ledger.realized_price) as realized_price,
        max(ledger.realized_price) as realized_price_high,
        max_by(ledger.promo_id, promotions.observed_depth_pct) as promo_id,
        list_sort(array_agg(ledger.promo_id)) as stacked_promo_ids,
        count(*) as promo_count
    from ledger
    left join promotions on ledger.promo_id = promotions.promo_id
    group by ledger.store_id, ledger.sku_id, ledger.effective_date

),

with_previous_day as (

    select
        *,
        lag(base_price) over (
            partition by store_id, sku_id order by effective_date
        ) as prev_base_price,
        lag(realized_price) over (
            partition by store_id, sku_id order by effective_date
        ) as prev_realized_price,
        lag(promo_id) over (
            partition by store_id, sku_id order by effective_date
        ) as prev_promo_id,
        lag(effective_date) over (
            partition by store_id, sku_id order by effective_date
        ) as prev_effective_date
    from per_day

),

flagged as (

    select
        *,
        -- a new interval begins where anything priced changes, and also where
        -- a day is skipped: this feed is sparse, so contiguity has to be
        -- checked rather than assumed
        coalesce(
            base_price is distinct from prev_base_price
            or realized_price is distinct from prev_realized_price
            or promo_id is distinct from prev_promo_id
            or effective_date <> prev_effective_date + 1,
            true
        ) as starts_an_interval
    from with_previous_day

),

numbered as (

    select
        *,
        sum(case when starts_an_interval then 1 else 0 end) over (
            partition by store_id, sku_id
            order by effective_date
            rows between unbounded preceding and current row
        ) as interval_no
    from flagged

),

collapsed as (

    select
        store_id,
        sku_id,
        interval_no,
        min(effective_date) as effective_from_date,
        max(effective_date) + 1 as effective_to_date,
        count(*) as duration_days,
        min(base_price) as base_price,
        min(realized_price) as realized_price,
        min(promo_id) as promo_id,
        max(promo_count) as max_promos_active,
        bool_or(promo_count > 1) as is_promo_stacked,
        list_sort(list_distinct(flatten(array_agg(stacked_promo_ids)))) as stacked_promo_ids
    from numbered
    group by store_id, sku_id, interval_no

),

window_end as (

    select max(effective_date) as last_ledger_date from ledger

)

select
    {{ row_hash([
        'collapsed.store_id',
        'collapsed.sku_id',
        'collapsed.effective_from_date',
    ]) }} as price_interval_key,

    collapsed.store_id,
    collapsed.sku_id,
    collapsed.interval_no,

    collapsed.effective_from_date,
    collapsed.effective_to_date,
    cast(collapsed.effective_from_date as timestamp) as effective_from,
    cast(collapsed.effective_to_date as timestamp) as effective_to,
    collapsed.duration_days,
    -- the observed window ends before this interval did; the close date is
    -- where the data stops, not where the promotion did
    collapsed.effective_to_date > window_end.last_ledger_date as is_open_at_window_end,

    collapsed.base_price,
    collapsed.realized_price,
    collapsed.base_price - collapsed.realized_price as discount_amt,
    case
        when collapsed.base_price > 0
            then (collapsed.base_price - collapsed.realized_price) / collapsed.base_price
    end as discount_pct,

    collapsed.promo_id,
    collapsed.is_promo_stacked,
    collapsed.max_promos_active,
    collapsed.stacked_promo_ids
from collapsed
cross join window_end
