{#
    S3.1's acceptance gate, stated as the plan states it: imputed demand
    exceeds observed sales only on censored days.

    Two ways an uncensoring model goes wrong, and this catches both.

    **It can remove sales.** If `units_demanded_imputed` ever lands below
    `units_sold`, the correction has decided that fewer units were wanted than
    were actually bought, which is not a modelling error but a contradiction -
    the sales happened. Every downstream metric divides by this column, so a
    single such row makes `fill_rate` exceed 1 and `lost_sales_units` go
    negative. The model-level pair test in `_facts__models.yml` guards the same
    boundary from the other side; this states it as a gate rather than as a
    column property.

    **It can invent censoring where there was none.** A day the SKU never ran
    out has nothing to uncensor: demand is what sold, exactly, and any uplift
    is the model hallucinating lost sales on a day the shelf was full. That is
    the failure that would quietly inflate every wastage-versus-lost-sales
    tradeoff the decision engine is going to make in Sprint 4, and it is
    invisible in aggregate because it moves the total in the direction people
    expect.

    Both directions are checked on the same pass so a fix for one cannot
    silently open the other.
#}

select
    store_sku_day_key,
    store_id,
    sku_id,
    date_day,
    is_censored,
    units_sold,
    units_demanded_imputed,
    demand_imputation_method,
    case
        when units_demanded_imputed < units_sold then 'imputed below observed'
        else 'uplift on an uncensored day'
    end as violation
from {{ ref('agg_store_sku_day') }}
where
    units_demanded_imputed < units_sold
    or (not is_censored and units_demanded_imputed <> units_sold)
