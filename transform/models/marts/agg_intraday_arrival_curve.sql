{#
    The shape of a day's demand, by category and hour (task S3.1).

    This table exists to answer one question that `agg_store_sku_day` cannot
    answer for itself: when a SKU ran out at 07:00, what fraction of that day's
    demand had already arrived? The baseline it replaces answered "seven
    twenty-fourths", because that is how much of the clock had passed. The real
    answer here is 6.7%, because almost nobody shops between midnight and dawn.
    Scaling by the clock instead of by the demand understates lost sales by a
    factor of four on exactly the mornings a store runs dry.

    **Fitted from browse events, not sales.** Sales cannot describe demand in
    the hours a SKU was unavailable - that is the censoring this whole model
    exists to undo, and fitting the correction on the censored series would
    bake the bias into its own remedy. Page views happen whether or not stock
    does, so they carry the arrival pattern that sales lose.

    Two filters make that true rather than merely plausible:

      - **`pdp_view` only.** The other event type, `notify_me`, is emitted
        exclusively when stock is absent - 100% of its events are flagged
        censored. Including it would pile the curve's mass onto precisely the
        hours stockouts happen, which is the bias, wearing the mask of a fix.

      - **`was_in_stock` only.** An out-of-stock view says when a stockout was
        running, not when demand arrives. Measured over this dataset the two
        distributions are far apart - out-of-stock views put 8.1% of their mass
        at 06:00 against 3.9% for in-stock views - so the filter is load-bearing
        rather than defensive.

    Restricting to in-stock views does introduce a counter-bias: hours that
    stock out often are observed less often, so they are sampled less. The
    correction is to divide each hour's views by how many store-SKU-hours were
    actually in stock in it. That was measured and it moves no hour's share by
    more than 0.25 percentage points, because 91% of store-SKU-hours are in
    stock and the exposure hardly varies. It is left out on those grounds - not
    overlooked, and the number is here so the judgement can be rechecked rather
    than retaken on faith.

    **Why category, and why the curve is then shrunk back toward the global
    one.** Category is the only dimension that moves the curve enough to be
    worth modelling: the worst-fitting category sits 5.9 points of total
    variation from the global curve, against 1.3 for the worst store and 0.8
    for the worst day of week. Fruit and vegetables skew to the morning, snacks
    to the evening, and an expiry project cares about that difference.

    But raw per-category curves would be a trap. The two categories that look
    *most* distinctive - Bakery at 4.3 points and Meat, Fish & Seafood at 4.3 -
    are the two with the least data, and a 24-bin histogram built from 4,248
    events deviates from the global curve by about 2.7 points from sampling
    noise alone. Most of what makes them look distinctive is that they are
    thin. Both are highly perishable, so their numbers are the ones an expiry
    model leans on hardest, and handing them a curve made largely of noise
    would put that noise straight into their lost-sales figures.

    So each category's curve is blended toward the global one by the share of
    its deviation that noise cannot explain:

        weight = 1 - (expected_noise_tvd / observed_tvd)^2

    which is a variance decomposition, not a tuned constant - there is no knob
    here to fit. Snacks, with 180k events and a real signal, keeps 98% of its
    own shape; Health & Wellness, whose deviation is almost entirely sampling
    error, keeps 53%; Meat, Fish & Seafood keeps 61%. Categories earn their
    curve in proportion to the evidence they brought.

    Grain: one row per l1_category per hour of the IST day, 12 x 24 = 288 rows.
    Small enough to ship in the demo slice, which is deliberate - the curve is
    the evidence for every lost-sales number the app displays.
#}

with events as (

    select
        products.l1_category,
        clicks.event_hour_ist,
        sum(clicks.event_count) as events
    from {{ ref('fct_clickstream') }} as clicks
    inner join {{ ref('dim_product') }} as products
        on clicks.sku_id = products.sku_id
    where clicks.event_type = 'pdp_view'
        and clicks.was_in_stock
    group by products.l1_category, clicks.event_hour_ist

),

categories as (

    select distinct l1_category
    from events

),

day_hours as (

    select unnest(generate_series(0, 23)) as hour_ist

),

-- a dense 24-hour spine per category: an hour nobody browsed is a real zero,
-- and leaving it absent would renormalise the rest of the curve upward
spine as (

    select
        categories.l1_category,
        day_hours.hour_ist
    from categories
    cross join day_hours

),

observed as (

    select
        spine.l1_category,
        spine.hour_ist,
        coalesce(events.events, 0) as events
    from spine
    left join events
        on spine.l1_category = events.l1_category
            and spine.hour_ist = events.event_hour_ist

),

global_curve as (

    select
        hour_ist,
        sum(events) / sum(sum(events)) over () as global_share
    from observed
    group by hour_ist

),

category_curve as (

    select
        l1_category,
        hour_ist,
        events,
        sum(events) over (partition by l1_category) as category_events,
        events / nullif(sum(events) over (partition by l1_category), 0) as category_share
    from observed

),

-- how far each category sits from the global curve, against how far pure
-- sampling noise would have carried it. Total variation distance halves the
-- summed absolute difference, so both quantities are on the same scale.
divergence as (

    select
        category_curve.l1_category,
        min(category_curve.category_events) as category_events,
        0.5 * sum(abs(category_curve.category_share - global_curve.global_share))
            as observed_tvd,
        -- mean absolute deviation of a binomial share is sigma * sqrt(2/pi)
        0.5 * sum(
            sqrt(
                global_curve.global_share * (1 - global_curve.global_share)
                / nullif(category_curve.category_events, 0)
            ) * sqrt(2 / pi())
        ) as noise_tvd
    from category_curve
    inner join global_curve
        on category_curve.hour_ist = global_curve.hour_ist
    group by category_curve.l1_category

),

weights as (

    select
        l1_category,
        category_events,
        observed_tvd,
        noise_tvd,
        greatest(
            0.0,
            1.0 - pow(noise_tvd / nullif(observed_tvd, 0), 2)
        ) as signal_weight
    from divergence

),

blended as (

    select
        category_curve.l1_category,
        category_curve.hour_ist,
        category_curve.events,
        weights.category_events,
        weights.signal_weight,
        category_curve.category_share,
        global_curve.global_share,
        weights.signal_weight * category_curve.category_share
        + (1 - weights.signal_weight) * global_curve.global_share as arrival_share
    from category_curve
    inner join global_curve
        on category_curve.hour_ist = global_curve.hour_ist
    inner join weights
        on category_curve.l1_category = weights.l1_category

)

select
    blended.l1_category,
    blended.hour_ist,

    blended.events,
    blended.category_events,

    blended.category_share,
    blended.global_share,

    -- how much of its own shape the category earned, identical across its 24
    -- rows - carried per row so a reader charting the curve can see at a glance
    -- whether they are looking at evidence or at the global fallback
    blended.signal_weight,

    -- the two columns consumers actually use. Renormalised because blending
    -- two distributions leaves a float's worth of drift off 1.0, and a
    -- cumulative share that reaches 0.9999 would quietly inflate every
    -- imputation that divides by it.
    blended.arrival_share / sum(blended.arrival_share) over (
        partition by blended.l1_category
    ) as arrival_share,

    coalesce(
        sum(blended.arrival_share) over (
            partition by blended.l1_category
            order by blended.hour_ist
            rows between unbounded preceding and 1 preceding
        ) / sum(blended.arrival_share) over (partition by blended.l1_category),
        0
    ) as cumulative_share_before
from blended
