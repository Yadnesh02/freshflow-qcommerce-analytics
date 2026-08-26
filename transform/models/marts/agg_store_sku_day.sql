{{
    config(
        materialized='incremental',
        unique_key='store_sku_day_key',
        incremental_strategy='delete+insert',
    )
}}

{#
    One row per store, SKU and day. The table the metric layer reads, and the
    one gate G2 is measured against.

    **The incremental keys on the event date, not the partition, and that is
    the entire point.** Defect 2 delivers orders up to 48h after they happened,
    so on any given run the newest partition contains rows belonging to three
    different days. An incremental filtered on the arrival partition processes
    those rows into the wrong day; an incremental filtered on the event date
    but with no lookback never revisits the day they belong to and drops 23,232
    orders on the floor. Neither fails. Both produce a table that looks
    complete.

    So the filter is on `date_day` - the IST event date - and it reaches back
    `late_arrival_lookback_days` behind the newest day already built, then
    recomputes those days from scratch. delete+insert on the surrogate key
    replaces them wholesale, which is what makes a late arrival change a day
    that was already written.

    **The trailing average needs more history than the lookback does.** A
    7-day rolling mean computed over only the 3 reprocessed days is a 3-day
    mean wearing a 7-day label. Source rows are therefore read from
    `trailing_window_days` further back than the window being rebuilt, and the
    extra days are dropped at the end - they exist only to give the window
    something to average over.

    **Why the spine is a union rather than the sales table.** A store-SKU-day
    with no sales still matters: it may have received stock, written stock off,
    or spent the day out of stock, and every one of those is a row the wastage
    and availability metrics need. Driving from sales alone would silently
    restrict the table to days something sold.

    **`units_demanded_imputed` is a documented placeholder.** It is what
    `fill_rate` and `lost_sales_units` divide by, so it cannot be null, and the
    real censored-demand correction is Sprint 3's work. The baseline here
    scales observed sales by the inverse of the fraction of the day the SKU was
    actually sellable. That assumes demand is uniform across the day, which it
    is not - the curve peaks at the morning and evening rushes, and stockouts
    happen at those peaks precisely because that is when stock runs out. So
    this baseline *understates* lost sales, systematically, on exactly the days
    it matters most. `demand_imputation_method` records which estimate a row
    got so nothing downstream has to guess, and so Sprint 3's model can be
    compared against it rather than silently replacing it.
#}

{%- set lookback = var('late_arrival_lookback_days') -%}
{%- set trailing = var('trailing_window_days') -%}
{%- set cutoff = var('arrival_cutoff_date') -%}

with bounds as (

    select
        {% if is_incremental() -%}
            (select max(date_day) from {{ this }}) - {{ lookback }}
        {%- else -%}
            cast('1900-01-01' as date)
        {%- endif %} as rebuild_from,
        {% if is_incremental() -%}
            (select max(date_day) from {{ this }}) - {{ lookback + trailing }}
        {%- else -%}
            cast('1900-01-01' as date)
        {%- endif %} as read_from

),

sales as (

    select
        items.store_id,
        items.sku_id,
        items.date_day,

        sum(items.signed_qty) as units_sold,
        sum(items.units) filter (where items.line_type = 'sale') as gross_units,
        sum(items.units) filter (where items.line_type = 'return') as returned_units,
        count(distinct items.order_id) as order_count,

        sum(items.net_revenue) as net_revenue,
        sum(items.gross_revenue) as gross_revenue,
        sum(items.discount_value) as discount_value,
        sum(items.cogs) as cogs,
        sum(items.gross_margin) as gross_margin,

        sum(items.signed_qty) filter (
            where promotions.promo_type = 'markdown'
        ) as markdown_units,
        sum(items.discount_value) filter (
            where promotions.funding_source = 'platform'
        ) as markdown_subsidy_platform,
        sum(items.discount_value) filter (
            where promotions.funding_source = 'brand'
        ) as markdown_subsidy_brand,

        min(items.dte_at_sale) as min_dte_at_sale,
        sum(items.signed_qty * items.dte_at_sale) as dte_weighted_units
    from {{ ref('fct_order_item') }} as items
    left join {{ ref('dim_promotion') }} as promotions
        on items.promo_id = promotions.promo_id
    cross join bounds
    where items.date_day >= bounds.read_from
        {%- if cutoff %}
        -- the header's arrival, not the line's: defect 2 moves headers to a
        -- later partition and leaves lines where they were, so filtering on
        -- the line's own partition makes every late order look like it was
        -- always there and a replay silently reports itself complete
            and items.order_arrival_date <= cast('{{ cutoff }}' as date)
            and items.date_day <= cast('{{ cutoff }}' as date)
        {%- endif %}
    group by items.store_id, items.sku_id, items.date_day

),

stock as (

    select
        movements.store_id,
        movements.sku_id,
        movements.date_day,
        sum(movements.qty_delta) as net_movement,
        sum(movements.qty_delta) filter (
            where movements.event_type in ('inbound', 'opening_balance')
        ) as received_units,
        sum(movements.movement_cost_value) filter (
            where movements.event_type in ('inbound', 'opening_balance')
        ) as received_value,
        -sum(movements.qty_delta) filter (
            where movements.event_type = 'expiry_writeoff'
        ) as writeoff_units,
        -sum(movements.movement_cost_value) filter (
            where movements.event_type = 'expiry_writeoff'
        ) as writeoff_value
    from {{ ref('fct_inventory_movement') }} as movements
    cross join bounds
    where movements.date_day >= bounds.read_from
        {%- if cutoff %}
            and movements.arrival_date <= cast('{{ cutoff }}' as date)
            and movements.date_day <= cast('{{ cutoff }}' as date)
        {%- endif %}
    group by movements.store_id, movements.sku_id, movements.date_day

),

availability as (

    select
        hours.store_id,
        hours.sku_id,
        hours.date_day,
        sum(hours.hours_in_state) as hours_carried,
        sum(hours.hours_in_state) filter (where hours.is_in_stock) as hours_in_stock,
        max(hours.on_hand_units) as opening_units,
        bool_or(not hours.is_in_stock) as is_censored
    from {{ ref('fct_availability_hour') }} as hours
    cross join bounds
    where hours.date_day >= bounds.read_from
        {%- if cutoff %}
            and hours.date_day <= cast('{{ cutoff }}' as date)
        {%- endif %}
    group by hours.store_id, hours.sku_id, hours.date_day

),

browsing as (

    select
        events.store_id,
        events.sku_id,
        events.date_day,
        sum(events.event_count) as browse_events,
        sum(events.censored_event_count) as censored_browse_events
    from {{ ref('fct_clickstream') }} as events
    cross join bounds
    where events.date_day >= bounds.read_from
        {%- if cutoff %}
            and events.date_day <= cast('{{ cutoff }}' as date)
        {%- endif %}
    group by events.store_id, events.sku_id, events.date_day

),

spine as (

    select
        store_id,
        sku_id,
        date_day
    from sales
    union
    select
        store_id,
        sku_id,
        date_day
    from stock
    union
    select
        store_id,
        sku_id,
        date_day
    from availability
    union
    select
        store_id,
        sku_id,
        date_day
    from browsing

),

joined as (

    select
        spine.store_id,
        spine.sku_id,
        spine.date_day,

        coalesce(sales.units_sold, 0) as units_sold,
        coalesce(sales.gross_units, 0) as gross_units,
        coalesce(sales.returned_units, 0) as returned_units,
        coalesce(sales.order_count, 0) as order_count,
        coalesce(sales.net_revenue, 0) as net_revenue,
        coalesce(sales.gross_revenue, 0) as gross_revenue,
        coalesce(sales.discount_value, 0) as discount_value,
        coalesce(sales.cogs, 0) as cogs,
        coalesce(sales.gross_margin, 0) as gross_margin,
        coalesce(sales.markdown_units, 0) as markdown_units,
        coalesce(sales.markdown_subsidy_platform, 0) as markdown_subsidy_platform,
        coalesce(sales.markdown_subsidy_brand, 0) as markdown_subsidy_brand,
        sales.min_dte_at_sale,
        sales.dte_weighted_units,

        coalesce(stock.received_units, 0) as received_units,
        coalesce(stock.received_value, 0) as received_value,
        coalesce(stock.writeoff_units, 0) as writeoff_units,
        coalesce(stock.writeoff_value, 0) as writeoff_value,
        coalesce(stock.net_movement, 0) as net_movement,

        coalesce(availability.hours_carried, 0) as hours_carried,
        coalesce(availability.hours_in_stock, 0) as hours_in_stock,
        coalesce(availability.opening_units, 0) as opening_units,
        coalesce(availability.is_censored, false) as is_censored,

        coalesce(browsing.browse_events, 0) as browse_events,
        coalesce(browsing.censored_browse_events, 0) as censored_browse_events
    from spine
    left join sales
        on
            spine.store_id = sales.store_id
            and spine.sku_id = sales.sku_id
            and spine.date_day = sales.date_day
    left join stock
        on
            spine.store_id = stock.store_id
            and spine.sku_id = stock.sku_id
            and spine.date_day = stock.date_day
    left join availability
        on
            spine.store_id = availability.store_id
            and spine.sku_id = availability.sku_id
            and spine.date_day = availability.date_day
    left join browsing
        on
            spine.store_id = browsing.store_id
            and spine.sku_id = browsing.sku_id
            and spine.date_day = browsing.date_day

),

with_trailing as (

    select
        *,
        avg(units_sold) over (
            partition by store_id, sku_id
            order by date_day
            rows between {{ trailing }} preceding and 1 preceding
        ) as trailing_7d_avg_units
    from joined

)

select
    {{ row_hash(['with_trailing.store_id', 'with_trailing.sku_id', 'with_trailing.date_day']) }}
        as store_sku_day_key,

    with_trailing.store_id,
    with_trailing.sku_id,
    with_trailing.date_day,
    products.is_private_label,
    products.l1_category,

    with_trailing.units_sold,
    with_trailing.gross_units,
    with_trailing.returned_units,
    with_trailing.order_count,

    with_trailing.net_revenue,
    with_trailing.gross_revenue,
    with_trailing.discount_value,
    with_trailing.cogs,
    with_trailing.gross_margin,

    with_trailing.opening_units,
    with_trailing.received_units,
    with_trailing.received_value,
    with_trailing.opening_units + with_trailing.net_movement as closing_units,
    with_trailing.writeoff_units,
    with_trailing.writeoff_value,

    with_trailing.markdown_units,
    with_trailing.markdown_subsidy_platform,
    with_trailing.markdown_subsidy_brand,
    case
        when with_trailing.units_sold <> 0
            then with_trailing.gross_revenue / with_trailing.units_sold
    end as base_price_avg,
    case
        when with_trailing.units_sold <> 0
            then with_trailing.net_revenue / with_trailing.units_sold
    end as realized_price_avg,

    with_trailing.hours_carried,
    with_trailing.hours_in_stock,
    case
        when with_trailing.hours_carried > 0
            then with_trailing.hours_in_stock / cast(with_trailing.hours_carried as double)
    end as in_stock_pct,
    with_trailing.is_censored,

    with_trailing.browse_events,
    with_trailing.censored_browse_events,
    with_trailing.min_dte_at_sale,
    case
        when with_trailing.units_sold <> 0
            then with_trailing.dte_weighted_units / with_trailing.units_sold
    end as avg_dte_at_sale,

    coalesce(with_trailing.trailing_7d_avg_units, 0) as trailing_7d_avg_units,

    -- baseline uncensoring: scale what sold by the share of the day it could
    -- have sold in. Understates lost sales when the stockout lands on a demand
    -- peak, which is when stockouts happen - Sprint 3 replaces this.
    case
        when not with_trailing.is_censored or with_trailing.hours_in_stock = 0
            then with_trailing.units_sold
        else greatest(
                with_trailing.units_sold,
                round(
                    with_trailing.units_sold
                    * with_trailing.hours_carried
                    / cast(with_trailing.hours_in_stock as double)
                )
            )
    end as units_demanded_imputed,
    case
        when not with_trailing.is_censored then 'observed'
        when with_trailing.hours_in_stock = 0 then 'unavailable_all_day'
        else 'availability_scaled'
    end as demand_imputation_method
from with_trailing
cross join bounds
left join {{ ref('dim_product') }} as products
    on with_trailing.sku_id = products.sku_id
where with_trailing.date_day >= bounds.rebuild_from
