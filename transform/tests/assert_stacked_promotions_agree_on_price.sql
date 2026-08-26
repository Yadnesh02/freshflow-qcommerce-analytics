{#
    33 store-SKU-days carry two promotions at once - a markdown and the Rs 11
    deal slot landing on the same ageing SKU. fct_price_history collapses them
    to one priced interval, which is only safe while both rows agree on what
    the customer paid.

    They do today: the deal price binds and both rows read Rs 11. If they ever
    disagree the collapse would be silently choosing one price over another,
    and the model would need a rule rather than an aggregate - so this fails
    the build instead of picking a winner.
#}

select
    store_id,
    sku_id,
    effective_date,
    count(*) as promos_active,
    count(distinct realized_price) as distinct_prices,
    min(realized_price) as lowest_price,
    max(realized_price) as highest_price
from {{ ref('stg_catalog__price_history') }}
group by store_id, sku_id, effective_date
having count(*) > 1 and count(distinct realized_price) > 1
