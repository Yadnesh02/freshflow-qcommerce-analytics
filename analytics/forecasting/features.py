"""Feature construction for the demand model (task S3.3).

Every feature here has to satisfy one rule: it must be knowable at the origin.
A forecast made on day O for day O+h may read anything up to and including O,
the calendar of O+h, and prices already scheduled for O+h - and nothing else.
Most of the work below is arranging for that to be true by construction rather
than checked afterwards, because a leaked feature does not fail, it flatters.

**Why `lag_1` is not in the feature set the plan asked for.** At horizon 1 the
previous day is the origin and lag_1 is legitimate; at horizon 7 it is six days
into the future. Lags of 7, 14 and 28 are safe at every horizon this model
serves, because the shortest of them equals the longest horizon. lag_1 is
replaced by `demand_at_origin` plus `horizon_days`, which carries the same
information - the most recent observation, and how stale it is - without ever
reaching past the origin. The alternative, forecasting recursively and feeding
each day's prediction in as the next day's lag, compounds its own error across
a week and makes the horizon-7 number a function of six earlier guesses.

**Gap-safe by date, not by row.** 1.6% of series have holes in them, and the
shortest covers 24 days. `lag(units, 7) over (order by date_day)` returns the
seventh *row* back, which in a series with a hole is the eighth or ninth day -
silently, on 286 series, in a feature named lag_7. Lags are therefore explicit
joins on date arithmetic and rolling windows are RANGE frames over intervals,
both of which mean what they say on a panel with holes in it.

**The price columns on `agg_store_sku_day` are unusable here, and dangerously
so.** `base_price_avg` and `realized_price_avg` are computed from what sold, so
they are null exactly when nothing sold - populated on 29.5% of cells, which is
precisely the 29.5% where `units_sold > 0`. Their presence is a perfect
predictor of a non-zero target. A model given them would score beautifully and
know nothing. Price comes from `fct_price_history` instead, which records
intervals whether or not anything sold; it covers 221 SKUs because it is a
promotional price log, and a SKU with no interval on a day was at its regular
price, which is what a zero discount means.

**Calendar features are read from the target day on purpose.** That a Saturday
in the monsoon is coming is known well before it arrives, and so is a promotion
that has already been scheduled. These are the only features taken from after
the origin, and they are the ones a planner genuinely has.
"""

from __future__ import annotations

# Lags safe at every horizon: the shortest equals MAX_HORIZON, so the day being
# read is never past the origin.
TARGET_RELATIVE_LAGS = (7, 14, 28)

NUMERIC_FEATURES = [
    "horizon_days",
    "demand_at_origin",
    "roll_mean_7",
    "roll_mean_28",
    "roll_std_7",
    "zero_share_28",
    "days_since_last_sale",
    *[f"lag_{n}" for n in TARGET_RELATIVE_LAGS],
    "discount_pct",
    "day_of_week",
]

BOOLEAN_FEATURES = [
    "is_weekend",
    "is_salary_week",
    "is_monsoon",
    "is_festival",
    "is_ipl_window",
    "is_month_end",
    "is_on_promo",
]

CATEGORICAL_FEATURES = ["store_id", "l1_category", "abc_class", "xyz_class"]

ALL_FEATURES = NUMERIC_FEATURES + BOOLEAN_FEATURES + CATEGORICAL_FEATURES


def feature_sql(origins_table: str, target_table: str, max_horizon: int) -> str:
    """SQL that materialises one feature row per series, origin and horizon.

    `origins_table` supplies a single `origin_date` column, which is what lets
    the same construction serve the dense training grid and the twelve
    evaluation origins without the two drifting apart.
    """
    lag_selects = ",\n        ".join(
        f"coalesce(lag_{n}.units_demanded_imputed, 0) as lag_{n}" for n in TARGET_RELATIVE_LAGS
    )
    lag_joins = "\n".join(
        f"""left join marts.agg_store_sku_day as lag_{n}
    on
        lag_{n}.store_id = anchor.store_id
        and lag_{n}.sku_id = anchor.sku_id
        and lag_{n}.date_day = pairs.target_day - {n}"""
        for n in TARGET_RELATIVE_LAGS
    )

    return f"""
create or replace table {target_table} as

with anchored as (

    -- rolling state as at each day, for every series. RANGE frames rather than
    -- ROWS: on a series with a hole, "6 rows back" is not "6 days back", and
    -- 286 series have holes.
    select
        store_id,
        sku_id,
        date_day,
        units_demanded_imputed as demand,
        avg(units_demanded_imputed) over w7 as roll_mean_7,
        avg(units_demanded_imputed) over w28 as roll_mean_28,
        coalesce(stddev_samp(units_demanded_imputed) over w7, 0) as roll_std_7,
        avg(case when units_demanded_imputed = 0 then 1.0 else 0.0 end)
            over w28 as zero_share_28,
        -- how long this SKU has been dead here. Intermittency is the dominant
        -- feature of this panel: the C/Z tail sells on 5% of days.
        date_day - max(case when units_demanded_imputed > 0 then date_day end)
            over w_all as days_since_last_sale
    from marts.agg_store_sku_day
    window
        w7 as (
            partition by store_id, sku_id order by date_day
            range between interval 6 days preceding and current row
        ),
        w28 as (
            partition by store_id, sku_id order by date_day
            range between interval 27 days preceding and current row
        ),
        w_all as (
            partition by store_id, sku_id order by date_day
            range between unbounded preceding and current row
        )

),

pairs as (

    select
        origins.origin_date,
        horizons.horizon_days,
        origins.origin_date + cast(horizons.horizon_days as integer) as target_day
    from {origins_table} as origins
    cross join (select unnest(generate_series(1, {max_horizon})) as horizon_days) as horizons

),

-- Promotions stack: 18,969 store-SKU-days are covered by two intervals at
-- once. Joining the interval table directly would emit a duplicate feature row
-- for each of them - inflating the training set and double-counting those
-- cells in the evaluation - so the day is collapsed to one row first. The
-- deepest applicable discount is the one a shopper sees.
price_by_day as (

    select
        price.store_id,
        price.sku_id,
        days.target_day,
        max(price.discount_pct) as discount_pct,
        bool_or(price.promo_id is not null) as is_on_promo
    from (select distinct target_day from pairs) as days
    inner join marts.fct_price_history as price
        on days.target_day between price.effective_from_date and price.effective_to_date
    group by price.store_id, price.sku_id, days.target_day

)

select
    anchor.store_id,
    anchor.sku_id,
    pairs.target_day as date_day,
    pairs.origin_date,
    pairs.horizon_days,

    -- everything on this line is as at the origin
    anchor.demand as demand_at_origin,
    anchor.roll_mean_7,
    anchor.roll_mean_28,
    anchor.roll_std_7,
    anchor.zero_share_28,
    coalesce(anchor.days_since_last_sale, 999) as days_since_last_sale,

    {lag_selects},

    -- the target day's calendar, which a planner knows a year ahead
    calendar.day_of_week,
    calendar.is_weekend,
    calendar.is_salary_week,
    calendar.is_monsoon,
    calendar.is_festival,
    calendar.is_ipl_window,
    calendar.is_month_end,

    -- a scheduled price is known before it takes effect; no interval covering
    -- the day means the SKU was at its regular price
    coalesce(price.discount_pct, 0) as discount_pct,
    coalesce(price.is_on_promo, false) as is_on_promo,

    products.l1_category,
    products.abc_class,
    products.xyz_class,
    products.abc_xyz_class,

    -- the label. Imputed, not sold: a model trained on sales learns the
    -- stockout instead of the demand that caused it.
    actuals.units_demanded_imputed as target_demand,
    actuals.units_sold,
    actuals.is_censored
from pairs
inner join anchored as anchor
    on anchor.date_day = pairs.origin_date
inner join marts.agg_store_sku_day as actuals
    on
        actuals.store_id = anchor.store_id
        and actuals.sku_id = anchor.sku_id
        and actuals.date_day = pairs.target_day
inner join marts.dim_date as calendar
    on calendar.date_day = pairs.target_day
inner join marts.dim_product as products
    on products.sku_id = anchor.sku_id
{lag_joins}
left join price_by_day as price
    on
        price.store_id = anchor.store_id
        and price.sku_id = anchor.sku_id
        and price.target_day = pairs.target_day
"""
