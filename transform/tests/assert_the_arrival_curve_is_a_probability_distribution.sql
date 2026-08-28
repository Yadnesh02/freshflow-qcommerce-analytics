{#
    The arrival curve is divided by, so its shape has to be a distribution.

    `units_demanded_imputed` computes `units_sold / cumulative_share_before`.
    That division is only meaningful if each category's 24 shares form a proper
    distribution - non-negative, summing to one, over a complete day. None of
    those hold automatically:

      - **Summing to one** survives the blend only because the result is
        renormalised. Mixing a category's curve with the global one at weights
        that add to 1 is exactly right in algebra and lands a few ulps off in
        floating point, and a total of 0.9999 inflates every imputation that
        divides into it. The renormalisation is what makes the sum exact; this
        asserts it stayed exact rather than trusting that it did.

      - **A complete 24 hours** depends on the dense spine. If a category was
        never browsed at 03:00 and the row were simply absent, the surviving
        hours would renormalise upward and the cumulative share would step over
        a gap - overstating how much demand a dawn stockout had already seen,
        which is the exact error this model was built to remove.

      - **Non-negative** is not guaranteed by construction: the shrinkage
        weight is clamped at zero, but a future change to the blend that let it
        go negative would produce negative shares, a cumulative sequence that
        walks backwards, and imputations that shrink demand on the worst days.

    Tolerance is 1e-9, which is float noise on a sum of 24 terms, not a margin
    the shares are allowed to drift within.
#}

with per_category as (

    select
        l1_category,
        count(*) as hours_present,
        count(distinct hour_ist) as distinct_hours,
        min(arrival_share) as min_share,
        sum(arrival_share) as total_share,
        min(cumulative_share_before) as min_cumulative,
        max(cumulative_share_before) as max_cumulative
    from {{ ref('agg_intraday_arrival_curve') }}
    group by l1_category

)

select
    l1_category,
    hours_present,
    total_share,
    min_share,
    case
        when hours_present <> 24 or distinct_hours <> 24 then 'day is not 24 distinct hours'
        when abs(total_share - 1) > 1e-9 then 'shares do not sum to one'
        when min_share < 0 then 'negative share'
        when min_cumulative <> 0 then 'cumulative share does not start at zero'
        else 'cumulative share reaches or exceeds one'
    end as violation
from per_category
where
    hours_present <> 24
    or distinct_hours <> 24
    or abs(total_share - 1) > 1e-9
    or min_share < 0
    or min_cumulative <> 0
    or max_cumulative >= 1
