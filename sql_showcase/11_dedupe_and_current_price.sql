-- question: What is each store-SKU's price right now, given the price feed
--           arrives as overlapping intervals - and can we prove the order book
--           carries no duplicate lines after the staging repair?
-- technique: ROW_NUMBER over a natural key for pick-one-per-group, and the same
--            mechanic inverted to return duplicates rather than hide them
-- needs_days: 7
--
-- Two uses of one window function, and the second is the one worth having.
--
-- **Pick-latest** is the everyday form: partition by the natural key, order by
-- recency, keep row 1. The trap is that `row_number` will happily pick a winner
-- from a tie, so if two intervals share an `effective_from` the "current" price
-- is whichever the engine happened to emit first. The tie-break on
-- `interval_no` makes it deterministic; without it this query returns a
-- different price for the same SKU on different runs.
--
-- **Duplicate detection** is the same window with the predicate flipped:
-- anything with `rn > 1` over a key that is supposed to be unique is a defect.
-- The order book had 22,878 duplicated rows injected into it - a retried
-- webhook - and staging deduplicates on the full row hash rather than on
-- `order_id`, because a genuine order has many item lines and deduplicating on
-- the header would delete the basket. This half of the query is the assertion
-- that the repair held: it should return nothing, and a row here is a
-- regression rather than a curiosity.

with ranked_prices as (

    select
        store_id,
        sku_id,
        interval_no,
        effective_from_date,
        effective_to_date,
        base_price,
        realized_price,
        discount_pct,
        promo_id,
        is_open_at_window_end,
        row_number() over (
            partition by store_id, sku_id
            -- recency first, then a unique tie-break so the winner is stable
            order by effective_from_date desc, interval_no desc
        ) as recency_rank
    from marts.fct_price_history

),

current_price as (

    select
        store_id,
        sku_id,
        effective_from_date,
        base_price,
        realized_price,
        discount_pct,
        promo_id,
        is_open_at_window_end
    from ranked_prices
    where recency_rank = 1

),

duplicate_order_lines as (

    select
        order_item_key,
        count(*) as copies
    from marts.fct_order_item
    group by order_item_key
    having count(*) > 1

)

select
    current_price.store_id,
    current_price.sku_id,
    products.sku_name,
    current_price.effective_from_date,
    current_price.base_price,
    current_price.realized_price,
    round(current_price.discount_pct, 3) as discount_pct,
    current_price.promo_id is not null as on_promotion,
    current_price.is_open_at_window_end,
    -- carried through so the assertion is visible in the same result set:
    -- anything other than zero here means the dedupe regressed
    (select count(*) from duplicate_order_lines) as duplicate_order_lines
from current_price
inner join marts.dim_product as products on current_price.sku_id = products.sku_id
where current_price.promo_id is not null
order by current_price.discount_pct desc, current_price.store_id, current_price.sku_id
limit 50;
