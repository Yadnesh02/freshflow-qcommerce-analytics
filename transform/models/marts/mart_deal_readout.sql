-- The deal rail's findings, served to the three pages of D6.
--
-- Thin on purpose, for the same reason mart_experiment_readout is: the
-- estimators live in `analytics/deal/`, beside the tests that constrain them,
-- and a confidence interval re-implemented in SQL would be a second
-- implementation that disagrees with the first the moment either changes.
--
-- What this model adds is contract and presentation. A stable ordering, so the
-- pages do not reshuffle between builds. A `display_value` formatted in each
-- figure's own unit, so a rate never renders as rupees and a count never gains
-- a decimal. And `is_significant`, computed once here rather than by three
-- pages each deciding what an interval excluding zero means.

with readout as (
    select * from {{ source('deal', 'readout') }}
),

ordered as (
    select
        *,
        case page
            when 'does_it_pay' then 1
            when 'who_should_see_it' then 2
            when 'what_runs_tomorrow' then 3
            else 9
        end as page_order,
        -- An interval that excludes zero. Null where a figure carries no
        -- interval at all, which is NOT the same as "not significant" and must
        -- not render as a cross on the page.
        case
            when ci_low is null or ci_high is null then null
            when ci_low > 0 or ci_high < 0 then true
            else false
        end as is_significant
    from readout
)

select
    page,
    page_order,
    metric,
    label,
    value,
    ci_low,
    ci_high,
    unit,
    is_significant,
    source,
    note,
    case
        when value is null then '--'
        -- Two decimals on small amounts, none on large. A total of Rs 352,659
        -- does not want paise, but a per-customer attach effect of Rs 4.63
        -- rendered as 'Rs 5' loses the number entirely - and the per-customer
        -- figures are the ones this page argues from.
        when unit = 'inr' and abs(value) < 1000
            then concat('Rs ', format('{:,.2f}', value))
        when unit = 'inr' then concat('Rs ', format('{:,.0f}', value))
        -- the sign is spelled out rather than asked of the format spec: DuckDB
        -- rejects '+' and ',' together, and a churn effect without its sign is
        -- the one number on these pages that must never be ambiguous
        when unit = 'rate'
            then concat(
                    case when value >= 0 then '+' else '-' end,
                    format('{:,.2f}', abs(value) * 100), ' pp'
                )
        when unit = 'ratio' then concat(format('{:,.2f}', value), 'x')
        when unit = 'count' then format('{:,.2f}', value)
        -- 4g turned a Qini of 13,095.9 into 1.31e+04, which is unreadable on a
        -- page whose whole job is to make one number legible
        else format('{:,.1f}', value)
    end as display_value
from ordered
order by page_order, metric
