{#
    fct_order_item denormalises store_id and date_day off the order header so
    that slicing by store or day does not cost a 1.5M-row join on every read.
    That is a deliberate trade, and it is only sound while the copy cannot
    drift from the source.

    Nothing enforces that but this. A denormalised column that silently
    disagrees with its origin is worse than the join it replaced, because two
    queries answering the same question through different paths return
    different numbers and neither looks wrong.
#}

select
    items.order_item_key,
    items.order_id,
    items.store_id as store_on_the_line,
    orders.store_id as store_on_the_header,
    items.date_day as day_on_the_line,
    orders.date_day as day_on_the_header
from {{ ref('fct_order_item') }} as items
inner join {{ ref('fct_order') }} as orders on items.order_id = orders.order_id
where
    items.store_id is distinct from orders.store_id
    or items.date_day is distinct from orders.date_day
