-- question: Which product pairs are bought together far more often than their
--           individual popularity would predict, and are any of them pairs we
--           could protect from stocking out together?
-- technique: self-join on the order grain with an ordered pair guard, lift
--            computed against independent baselines
-- needs_days: 30
--
-- **Co-occurrence counts on their own rank the bestsellers.** Milk appears with
-- everything because milk appears with everything, so a raw pair count is a
-- popularity list wearing a recommendation's clothes. Lift divides the observed
-- joint rate by the rate the two would show if they were independent, which is
-- what makes a genuinely associated pair beat a merely frequent one.
--
-- The `a.sku_id < b.sku_id` guard is doing two jobs: it removes the self-pair
-- and it keeps each unordered pair once rather than twice. Without it the
-- output doubles and every lift is computed on a duplicated denominator.
--
-- The support floor is not decoration. Lift is a ratio of small numbers at the
-- tail, so a pair seen four times can post a spectacular lift that means
-- nothing; requiring a floor is what separates a finding from an artefact.

with basket as (

    -- distinct, because a basket with two lines of the same SKU is one
    -- occurrence of that SKU, not two
    select distinct
        order_id,
        sku_id
    from marts.fct_order_item
    where line_type = 'sale'

),

order_count as (

    select count(distinct order_id) as orders from basket

),

sku_support as (

    select
        sku_id,
        count(*) as orders_with_sku
    from basket
    group by sku_id

),

pairs as (

    select
        a.sku_id as sku_a,
        b.sku_id as sku_b,
        count(*) as orders_with_both
    from basket as a
    inner join basket as b
        on
            a.order_id = b.order_id
            -- one row per unordered pair, and no sku paired with itself
            and a.sku_id < b.sku_id
    group by a.sku_id, b.sku_id
    having count(*) >= 200

)

select
    pairs.sku_a,
    product_a.sku_name as name_a,
    pairs.sku_b,
    product_b.sku_name as name_b,
    pairs.orders_with_both,
    round(100.0 * pairs.orders_with_both / order_count.orders, 3) as support_pct,
    round(
        (1.0 * pairs.orders_with_both / order_count.orders)
        / nullif(
            (1.0 * support_a.orders_with_sku / order_count.orders)
            * (1.0 * support_b.orders_with_sku / order_count.orders),
            0
        ),
        2
    ) as lift,
    -- a pair with high lift where one side stocks out often is a basket that
    -- breaks: the association is real and the shelf cannot honour it
    product_a.l1_category as category_a,
    product_b.l1_category as category_b
from pairs
cross join order_count
inner join sku_support as support_a on pairs.sku_a = support_a.sku_id
inner join sku_support as support_b on pairs.sku_b = support_b.sku_id
inner join marts.dim_product as product_a on pairs.sku_a = product_a.sku_id
inner join marts.dim_product as product_b on pairs.sku_b = product_b.sku_id
order by lift desc, pairs.orders_with_both desc
limit 30;
