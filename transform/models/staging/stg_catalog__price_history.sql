{#
    The price ledger: what each store was asking for each SKU on each day, and
    which promotion - if any - explains the gap between the two.

    `discount_pct` is derived here rather than downstream because every model
    that touches markdown needs it and computing it in five places invites five
    slightly different denominators. It is expressed against base price, not
    MRP: the elasticity work asks "how much did we cut our own price", and MRP
    is a printed number the store never charged.
#}

select
    store_id,
    sku_id,
    effective_date,
    base_price,
    realized_price,
    base_price - realized_price as discount_amt,
    case
        when base_price > 0 then (base_price - realized_price) / base_price
    end as discount_pct,
    promo_id,
    promo_id is not null as is_promoted,
    dt as arrival_date
from {{ source('catalog', 'price_history') }}
