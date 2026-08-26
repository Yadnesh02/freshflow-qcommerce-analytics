{#
    The property that makes dim_product_snapshot safe to join to: on any date
    that has a sale, exactly one version of that SKU is in force.

    Both failure directions are caught by the same count. Zero matches means a
    gap - the sale falls outside the history and an inner join drops the row,
    understating revenue silently. Two or more means the intervals overlap, and
    the join fans out - the same order line counted twice, overstating it just
    as silently. Testing for exactly one is the only version of this test worth
    writing.

    Run against the dates that actually carry sales rather than every date in
    the calendar, because that is where a gap would cost something, and because
    it keeps the test honest about what it proves.
#}

with sold as (

    select distinct
        lines.sku_id,
        orders.order_date_ist as sale_date
    from {{ ref('stg_pos__order_lines') }} as lines
    inner join {{ ref('stg_pos__orders') }} as orders using (order_id)

),

matched as (

    select
        sold.sku_id,
        sold.sale_date,
        count(versions.sku_id) as versions_in_force
    from sold
    left join {{ ref('dim_product_snapshot') }} as versions
        on
            sold.sku_id = versions.sku_id
            and sold.sale_date >= versions.valid_from_date
            and (sold.sale_date < versions.valid_to_date or versions.valid_to_date is null)
    group by sold.sku_id, sold.sale_date

)

select
    sku_id,
    sale_date,
    versions_in_force
from matched
where versions_in_force <> 1
