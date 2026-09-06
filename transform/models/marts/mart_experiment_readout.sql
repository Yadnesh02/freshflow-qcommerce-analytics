-- The policy backtest readout, served to the Executive page (task S5.5).
--
-- Thin on purpose. The statistics are computed in `analytics/experiment/`,
-- where the estimator, its interval and its assumptions live together and are
-- tested against constructed panels; recomputing them in SQL would put a second
-- implementation of a confidence interval in the warehouse, and the two would
-- disagree the first time either changed.
--
-- What this model adds is presentation and contract: a stable ordering so the
-- page does not reorder between builds, and a `display_*` set that formats each
-- metric in its own unit, so a ratio never renders as rupees.
--
-- **`delta` is a difference-in-differences, not `policy_b - policy_a`.** It
-- removes the pre-period gap between the two groups and anything that hit both
-- on the same day. Subtracting the two columns on screen would give a different
-- number, which is why the page shows the delta rather than deriving it.

with readout as (
    select * from {{ source('experiment', 'readout') }}
),

ordered as (
    select
        *,
        -- the north star first, then what it is made of, then the rest
        case metric
            when 'gm_awm_pct' then 1
            when 'wastage_rate_value' then 2
            when 'availability_pct' then 3
            when 'markdown_subsidy_inr' then 4
            else 9
        end as display_order
    from readout
)

select
    metric,
    unit,
    policy_a,
    policy_b,
    delta,
    ci_low,
    ci_high,
    significant,
    seeds,
    not_applicable_reason,
    not_applicable_reason is null as is_measured,
    case
        when unit = 'ratio' then round(100 * policy_a, 2)
        else round(policy_a, 2)
    end as display_policy_a,
    case
        when unit = 'ratio' then round(100 * policy_b, 2)
        else round(policy_b, 2)
    end as display_policy_b,
    case
        when unit = 'ratio' then round(100 * delta, 2)
        else round(delta, 2)
    end as display_delta,
    -- 'n/a' explicitly rather than falling through to 'Rs': an unmeasured row
    -- labelled in rupees reads as a money figure that happens to be missing,
    -- when the point of those rows is that no comparison exists to make.
    case
        when unit = 'ratio' then 'pp'
        when unit = 'money' then 'Rs'
        else 'n/a'
    end as display_unit
from ordered
order by display_order, metric
