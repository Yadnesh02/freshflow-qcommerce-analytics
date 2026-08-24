{#
    Shelf-life buckets.

    Days-to-expiry is the dimension the markdown optimiser is built on, because
    price elasticity is not constant across it: freshness aversion means a
    customer will not accept a two-day-old curd at the discount that would move
    a fresh one. Bucketing it here keeps that logic in one place rather than
    scattered as CASE statements across a dozen models.

    Bounds are lower-inclusive, upper-exclusive.
#}

select
    band as dte_band,
    min_days,
    max_days,
    sort_order
from {{ ref('dte_bands') }}
