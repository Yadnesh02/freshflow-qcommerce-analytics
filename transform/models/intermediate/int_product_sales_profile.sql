{#
    ABC and XYZ classification, the two attributes on dim_product that are
    inferred rather than carried.

    **ABC is about size.** SKUs ranked by net revenue, then split on cumulative
    share: A is the head that produces the first 80%, B the next 15%, C the
    long tail. It decides where attention goes - service-level targets, review
    frequency, whether a stockout is worth an alert.

    **XYZ is about predictability**, and it is the one people skip. Measured as
    the coefficient of variation of daily units: X below 0.5 is stable enough to
    forecast tightly, Y up to 1.0 needs buffer, Z above 1.0 is erratic and no
    amount of model tuning fixes it. The pair is what makes the classification
    useful - an AZ SKU is high-revenue and unforecastable, which is a different
    operational problem from a CX SKU and gets a different safety stock.

    **Zero-sellers.** 185 of the 1,500 SKUs never sold. They are classified C
    and Z rather than given a fourth class, because the dimension registry
    declares three values each and inventing a fourth would break every
    consumer that trusts it. `has_sales` is published alongside so nothing has
    to infer "C because tail" versus "C because never sold".

    Variation is measured across days the SKU actually sold on, not across the
    whole calendar. Padding the gaps with zeroes would make an intermittent SKU
    look stable when its problem is precisely that it is not - the zeroes would
    dominate the mean and shrink the ratio.

    Ephemeral: this exists to keep dim_product readable, not to be queried.
#}

with lines as (

    select
        item.sku_id,
        orders.order_date_ist as sale_date,
        item.signed_qty,
        item.signed_qty * item.unit_realized_price as line_revenue
    from {{ ref('stg_pos__order_lines') }} as item
    inner join {{ ref('stg_pos__orders') }} as orders
        on item.order_id = orders.order_id

),

daily as (

    select
        sku_id,
        sale_date,
        sum(signed_qty) as units,
        sum(line_revenue) as revenue
    from lines
    group by sku_id, sale_date

),

per_sku as (

    select
        sku_id,
        sum(revenue) as net_revenue,
        sum(units) as net_units,
        count(*) as selling_days,
        avg(units) as mean_daily_units,
        stddev_samp(units) as sd_daily_units
    from daily
    group by sku_id

),

catalogue as (

    select distinct sku_id from {{ ref('stg_catalog__products') }}

),

joined as (

    select
        catalogue.sku_id,
        coalesce(per_sku.net_revenue, 0) as net_revenue,
        coalesce(per_sku.net_units, 0) as net_units,
        coalesce(per_sku.selling_days, 0) as selling_days,
        per_sku.mean_daily_units,
        per_sku.sd_daily_units,
        per_sku.sku_id is not null as has_sales,
        case
            when per_sku.mean_daily_units > 0
                then per_sku.sd_daily_units / per_sku.mean_daily_units
        end as demand_cv
    from catalogue
    left join per_sku on catalogue.sku_id = per_sku.sku_id

),

ranked as (

    select
        *,
        -- ties broken by sku_id so the cumulative share, and therefore the
        -- class boundary, is identical on every rebuild
        sum(net_revenue) over (
            order by net_revenue desc, sku_id asc
            rows between unbounded preceding and current row
        ) / nullif(sum(net_revenue) over (), 0) as cumulative_revenue_share
    from joined

)

select
    sku_id,
    net_revenue,
    net_units,
    selling_days,
    has_sales,
    demand_cv,
    round(cumulative_revenue_share, 6) as cumulative_revenue_share,
    case
        when not has_sales then 'C'
        when cumulative_revenue_share <= 0.80 then 'A'
        when cumulative_revenue_share <= 0.95 then 'B'
        else 'C'
    end as abc_class,
    case
        when demand_cv is null then 'Z'
        when demand_cv < 0.5 then 'X'
        when demand_cv < 1.0 then 'Y'
        else 'Z'
    end as xyz_class
from ranked
