"""Rolling-origin backtest of the baseline forecasts (task S3.2).

The harness matters more than the two baselines it currently runs. S3.3 swaps
LightGBM into the `forecast_units` column and changes nothing else, and the
number that decides whether that model ships - forecast value add against the
naive rule - is only meaningful if both were scored the exact same way. So the
evaluation protocol lives here, once, rather than inside each model.

**Rolling origin, not a single holdout.** A single train/test split reports the
accuracy of one arbitrary fortnight. Retail demand has festivals, salary weeks
and a monsoon in it, and a split that lands beside one of them measures the
calendar rather than the model. Twelve origins a week apart, each forecasting
seven days forward, spread that luck across a quarter.

**Nothing after the origin is visible at the origin.** Both baselines are
computed strictly from days at or before their origin, which is the whole point
of the exercise and the easiest thing in forecasting to get wrong. Seasonal
naive for origin+h reads day origin+h-7, which is at or before the origin for
every horizon up to 7 - the reason horizons stop at 7 rather than 14.

**Trained on imputed demand, scored on uncensored days.** These are different
columns for a reason that is easy to state and expensive to discover: a
forecast trained on `units_sold` learns that a SKU which reliably empties by
09:00 sells twenty a day, forecasts twenty, orders for twenty, and empties by
09:00 again. Training reads `units_demanded_imputed`, which S3.1 corrects for
exactly that. Scoring then excludes censored days entirely, because on those
days the "actual" is itself an estimate - grading a model against another
model's output measures agreement, not accuracy. 91% of cells survive that
filter, so it costs little.

**What the numbers will look like, and why that is not a bug.** At store x SKU
x day this demand is intermittent: the best class averages 2.15 units a day
with 36% zero days, and the C/Z tail averages 0.17 with 95% zero days. WAPE on
series like that is large for real reasons. A model cannot place a fractional
unit on the right day when the actual is a Bernoulli draw, and no amount of
tuning changes it - the honest response is to report per ABC-XYZ class so the
head and the tail are visible separately, and to expect the tail to be told by
a rule rather than a model.

**Bias reads positive on every class, and the protocol is why.** Stockouts
happen on busy days: mean demand on censored days is 0.750 against 0.705 on
uncensored ones. Training sees all of them and evaluation sees only the
uncensored subset, so the model is asked to predict the full mean and then
scored against a below-average sample of it. The gap is a property of
"evaluate on uncensored days only" rather than a model that over-forecasts,
and it will not disappear when LightGBM arrives in S3.3. Read `forecast_bias`
as a number to watch for *changes* in, not one whose sign is meaningful on its
own.

    python tasks.py backtest
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

# Twelve origins covers a quarter - long enough to contain a festival and a
# monsoon week, short enough that the oldest origin still resembles the present.
# Horizons stop at 7 because seasonal naive at horizon 8 would have to read a
# day the origin cannot see.
ORIGIN_COUNT = 12
# Five days, not seven, and the difference is not cosmetic. Weekly origins put
# every origin on the same weekday, which makes horizon h land on the same
# weekday every time - horizon and day-of-week become the same variable, and a
# WAPE-by-horizon table then reports neither. Measured on this data before the
# step was changed: h=5 was always Saturday (0.87 units/day) and h=1 always
# Tuesday (0.61), so h=5 posted the "best" WAPE purely by having the largest
# denominator. Any step coprime with 7 breaks the alignment;
# test_horizons_are_not_locked_to_a_weekday fails if this returns to 7.
ORIGIN_STEP_DAYS = 5
MAX_HORIZON = 7
SEASONAL_LAG_DAYS = 7
MA_WINDOW_DAYS = 7

TARGET_TABLE = "marts.mart_forecast_accuracy"


BACKTEST_SQL = f"""
create or replace table {TARGET_TABLE} as

with span as (

    select max(date_day) as last_day
    from marts.agg_store_sku_day

),

-- one origin a week, walking backwards from the last day that still leaves a
-- full horizon of actuals to score against
origins as (

    select span.last_day - {MAX_HORIZON}
        - cast(step.n * {ORIGIN_STEP_DAYS} as integer) as origin_date
    from span
    cross join (select unnest(generate_series(0, {ORIGIN_COUNT - 1})) as n) as step

),

targets as (

    select
        origins.origin_date,
        horizons.horizon_days,
        origins.origin_date + cast(horizons.horizon_days as integer) as date_day
    from origins
    cross join (select unnest(generate_series(1, {MAX_HORIZON})) as horizon_days) as horizons

),

-- the trailing mean over the {MA_WINDOW_DAYS} days ending at the origin. One
-- value per series per origin, reused across all {MAX_HORIZON} horizons: a
-- flat forecast is what a moving average *is*, and pretending it decays with
-- horizon would be inventing a model that was not fitted.
moving_average as (

    select
        origins.origin_date,
        history.store_id,
        history.sku_id,
        avg(history.units_demanded_imputed) as forecast_units
    from origins
    inner join marts.agg_store_sku_day as history
        on history.date_day between
            origins.origin_date - {MA_WINDOW_DAYS - 1} and origins.origin_date
    group by origins.origin_date, history.store_id, history.sku_id

)

select
    actuals.store_id,
    actuals.sku_id,
    actuals.date_day,
    targets.origin_date,
    targets.horizon_days,

    products.abc_class,
    products.xyz_class,
    products.abc_xyz_class,

    -- on an uncensored day units_sold and units_demanded_imputed are the same
    -- number by construction; units_sold is used because it is the one that
    -- actually happened
    actuals.units_sold as actual_units,

    coalesce(moving_average.forecast_units, 0) as forecast_units,

    -- the reference every model is judged against, including this one
    coalesce(seasonal.units_demanded_imputed, 0) as naive_units,

    'moving_average_{MA_WINDOW_DAYS}d' as model_name
from targets
inner join marts.agg_store_sku_day as actuals
    on actuals.date_day = targets.date_day
inner join marts.dim_product as products
    on actuals.sku_id = products.sku_id
left join moving_average
    on
        moving_average.origin_date = targets.origin_date
        and moving_average.store_id = actuals.store_id
        and moving_average.sku_id = actuals.sku_id
left join marts.agg_store_sku_day as seasonal
    on
        seasonal.store_id = actuals.store_id
        and seasonal.sku_id = actuals.sku_id
        and seasonal.date_day = targets.date_day - {SEASONAL_LAG_DAYS}
-- censored days are excluded from scoring, not from training: on those the
-- actual is an imputation, and a model graded against it is being measured
-- for agreement with S3.1 rather than for accuracy
where not actuals.is_censored
"""


REPORT_SQL = """
select
    abc_xyz_class as class,
    count(*) as cells,
    round(sum(actual_units)) as actual_units,
    round(sum(abs(actual_units - forecast_units)) / nullif(sum(actual_units), 0), 3) as wape,
    round(sum(abs(actual_units - naive_units)) / nullif(sum(actual_units), 0), 3) as naive_wape,
    round(sum(forecast_units - actual_units) / nullif(sum(actual_units), 0), 3) as bias,
    round(
        (sum(abs(actual_units - naive_units)) - sum(abs(actual_units - forecast_units)))
        / nullif(sum(actual_units), 0), 3
    ) as fva
from {table}
group by abc_xyz_class
order by abc_xyz_class
"""

HORIZON_SQL = """
select
    horizon_days,
    round(sum(abs(actual_units - forecast_units)) / nullif(sum(actual_units), 0), 3) as wape,
    round(sum(abs(actual_units - naive_units)) / nullif(sum(actual_units), 0), 3) as naive_wape
from {table}
group by horizon_days
order by horizon_days
"""


def run(con: duckdb.DuckDBPyConnection) -> None:
    """Build the accuracy table from scratch."""
    con.execute(BACKTEST_SQL)


def report(con: duckdb.DuckDBPyConnection) -> int:
    """Print the S3.2 acceptance gate: WAPE by ABC-XYZ class."""
    rows, origins, span = con.execute(
        f"""
        select count(*), count(distinct origin_date),
               max(date_day) - min(date_day) + 1
        from {TARGET_TABLE}
        """
    ).fetchone()
    print(f"\n  {rows:,} scored cells over {origins} origins spanning {span} days")
    print(f"  horizons 1-{MAX_HORIZON}, censored days excluded")

    # How much of what the baselines learned from was S3.1's estimate rather
    # than an observation. Not a diagnostic of the forecast - a statement about
    # what it was trained on, and the first thing to check if these numbers ever
    # move without the model having changed.
    imputed_share = con.execute(
        """
        select sum(case when demand_imputation_method <> 'observed'
                        then units_demanded_imputed else 0 end)
               / nullif(sum(units_demanded_imputed), 0)
        from marts.agg_store_sku_day
        """
    ).fetchone()[0]
    print(f"  {imputed_share:.1%} of the training signal is imputed, not observed\n")

    print("  WAPE by ABC-XYZ class")
    print(f"  {'class':<7}{'cells':>10}{'actual':>10}{'WAPE':>9}{'naive':>9}{'bias':>9}{'FVA':>9}")
    print("  " + "-" * 63)
    beaten = 0
    classes = con.execute(REPORT_SQL.format(table=TARGET_TABLE)).fetchall()
    for cls, cells, actual, wape, naive_wape, bias, fva in classes:
        flag = "" if fva is None or fva <= 0 else "  <- beats naive"
        if fva is not None and fva > 0:
            beaten += 1
        print(
            f"  {cls:<7}{cells:>10,}{actual:>10,.0f}{wape:>9.3f}"
            f"{naive_wape:>9.3f}{bias:>+9.3f}{fva:>+9.3f}{flag}"
        )

    print("\n  WAPE by horizon")
    print(f"  {'h':<7}{'WAPE':>9}{'naive':>9}")
    print("  " + "-" * 25)
    for horizon, wape, naive_wape in con.execute(HORIZON_SQL.format(table=TARGET_TABLE)).fetchall():
        print(f"  {horizon:<7}{wape:>9.3f}{naive_wape:>9.3f}")

    print(f"\n  the moving average beats seasonal naive on {beaten} of {len(classes)} classes")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Rolling-origin backtest of the baselines.")
    parser.add_argument("--warehouse", default=str(WAREHOUSE))
    args = parser.parse_args()

    warehouse = Path(args.warehouse)
    if not warehouse.exists():
        print(f"\n\033[31mno warehouse at {warehouse}\033[0m\n  run `python tasks.py build`")
        return 1

    con = duckdb.connect(str(warehouse))
    try:
        con.execute("set memory_limit = '4GB'")
        run(con)
        return report(con)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
