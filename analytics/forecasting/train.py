"""Gradient-boosted demand forecast, scored through S3.2's harness (task S3.3).

Trains one LightGBM model on origin-anchored features and writes its
predictions into `mart_forecast_accuracy`, replacing the moving average in the
`forecast_units` column and leaving it behind in `baseline_units`. The
evaluation origins come from `backtest.EVAL_ORIGINS_SQL` rather than being
restated here, because a model compared on different days than the baseline it
claims to beat has not been compared.

**The objective is Tweedie, and WAPE is the reason it is not L1.** This panel
is intermittent - between 36% and 95% of store-SKU-days are zero - and on a
series like that WAPE has a pathology worth stating plainly: a forecast of
zero everywhere scores exactly 1.000, because the numerator collapses to the
sum of actuals. Measured on this data, seven of the nine ABC-XYZ classes have
baseline WAPE *above* 1.0, meaning both baselines are beaten by predicting
nothing at all. Optimising the metric directly would therefore push the model
toward zero and score well doing it.

That would be worthless. The forecast exists to size orders, and a replenisher
needs the conditional mean of demand, not its median - which on an 80%-zero
series is zero. Tweedie regression targets that mean and handles the zero mass
directly, so the model stays useful for the thing it is for and its WAPE is
reported honestly rather than gamed. The zero forecast is printed alongside as
a column so a reader can see which classes are worth forecasting at all.

**The split is by time, not at random.** A shuffled split would put a Tuesday
from the middle of the training period in the test set with its own neighbours
visible, which is not a forecasting problem. Training stops a full horizon
before the earliest evaluation origin, so no training row's target day is
visible to any evaluated forecast.

**One model, not twelve.** Refitting at each origin is more faithful and costs
twelve times as much for sixty-two days of drift. The single fit is trained on
everything up to the earliest evaluation origin, which makes the last origin's
forecast the staler one - a bias against the model, not for it.

**Do not read the importance table as a list of what matters.** Gain puts
`roll_mean_28` at 64% and `zero_share_28` at 25%, and the whole calendar -
festival, monsoon, salary week, day of week - at around 1% between them. That
reads like the calendar features are dead weight, and measured against the
outcome they are the opposite:

    segment            actual   this model   7-day mean
    ordinary weekday    0.654    0.684        0.736
    weekend             0.855    0.922        0.731
    festival weekday    0.822    0.829        0.726
    festival weekend    1.058    1.103        0.774

The moving average is pinned near 0.73 in every row, because a trailing mean
cannot know a festival is coming; it under-forecasts festival weekends by 27%,
which in an inventory system is an empty shelf on the busiest day of the
quarter. This model moves from 0.68 to 1.10 across the same rows and stays
within 8% throughout.

Gain measures total variance explained across all rows. A flag that is decisive
on the 37 festival days of the year and irrelevant on the other 328 cannot
accumulate much of it, however much it is worth on the days it fires. The level
features earn their 89% honestly - most days really are just "about as much as
last month" - but that is a statement about most days, not about which feature
you would least like to remove. `test_the_model_tracks_the_festival_lift` is
what actually guards them.

    python tasks.py forecast
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np

from analytics.forecasting.backtest import (
    EVAL_ORIGINS_SQL,
    MAX_HORIZON,
    TARGET_TABLE,
)
from analytics.forecasting.features import (
    ALL_FEATURES,
    BOOLEAN_FEATURES,
    CATEGORICAL_FEATURES,
    feature_sql,
)

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

MODEL_NAME = "lightgbm_tweedie"
FORECAST_TABLE = "marts.mart_demand_forecast"

# Weekly training origins: 7 x 17.8k series x 7 horizons is already ~900k rows a
# month, and denser origins buy correlated copies of the same week rather than
# new information.
TRAIN_ORIGIN_STEP_DAYS = 7
# Nothing before this has 28 days of history behind it, so the lag features
# would be zeros pretending to be observations.
MIN_HISTORY_DAYS = 28

PARAMS = {
    "objective": "tweedie",
    # 1.1 sits near the Poisson end of the Tweedie family, which is where retail
    # count demand lives. Higher values assume a heavier continuous tail than
    # units-per-day has.
    "tweedie_variance_power": 1.1,
    "metric": "tweedie",
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "num_threads": 4,
    "verbosity": -1,
    "seed": 42,
}
NUM_ROUNDS = 400


TRAIN_ORIGINS_SQL = f"""
-- generate_series over dates yields timestamps; everything downstream joins on
-- dates, so the cast is not cosmetic
select cast(origin_ts as date) as origin_date
from (
    select unnest(generate_series(
        (select min(date_day) + {MIN_HISTORY_DAYS} from marts.agg_store_sku_day),
        -- stop a full horizon short of the earliest evaluated origin, so no
        -- training row's target day is a day any evaluated forecast predicts
        (select min(origin_date) - {MAX_HORIZON} from ({EVAL_ORIGINS_SQL})),
        interval {TRAIN_ORIGIN_STEP_DAYS} day
    )) as origin_ts
) as grid
"""


def _frame(con: duckdb.DuckDBPyConnection, table: str):
    """Pull a feature table into pandas with dtypes LightGBM can consume."""
    df = con.execute(f"select * from {table}").df()
    for col in BOOLEAN_FEATURES:
        df[col] = df[col].astype("int8")
    for col in CATEGORICAL_FEATURES:
        df[col] = df[col].astype("category")
    return df


def train(con: duckdb.DuckDBPyConnection) -> lgb.Booster:
    con.execute(f"create or replace table main._train_origins as {TRAIN_ORIGINS_SQL}")
    origins = con.execute("select count(*) from main._train_origins").fetchone()[0]
    print(f"  building features over {origins} training origins...", flush=True)

    started = time.time()
    con.execute(feature_sql("main._train_origins", "main._train_features", MAX_HORIZON))
    df = _frame(con, "main._train_features")
    print(f"  {len(df):,} training rows in {time.time() - started:.1f}s")

    dataset = lgb.Dataset(
        df[ALL_FEATURES],
        label=df["target_demand"],
        categorical_feature=CATEGORICAL_FEATURES,
        free_raw_data=False,
    )
    started = time.time()
    booster = lgb.train(PARAMS, dataset, num_boost_round=NUM_ROUNDS)
    print(f"  trained {NUM_ROUNDS} rounds in {time.time() - started:.1f}s")
    return booster


def predict_and_score(con: duckdb.DuckDBPyConnection, booster: lgb.Booster) -> None:
    con.execute(f"create or replace table main._eval_origins as {EVAL_ORIGINS_SQL}")
    con.execute(feature_sql("main._eval_origins", "main._eval_features", MAX_HORIZON))

    df = _frame(con, "main._eval_features")
    # Demand cannot be negative and Tweedie will not predict so, but a floor
    # costs nothing and makes the column safe to divide into downstream.
    df["prediction"] = np.maximum(booster.predict(df[ALL_FEATURES]), 0.0)
    print(f"  predicted {len(df):,} evaluation rows")

    con.register(
        "predictions", df[["store_id", "sku_id", "date_day", "horizon_days", "prediction"]]
    )
    con.register(
        "_eval_predictions",
        df[["store_id", "sku_id", "date_day", "origin_date", "horizon_days", "prediction"]],
    )

    # The forecast itself, kept whole. `mart_forecast_accuracy` drops censored
    # days because you cannot grade a model against an imputation, but a
    # consumer that needs to know how much will sell - the expiry risk model in
    # S3.4, the newsvendor in S4 - needs every cell, and a stockout day is one
    # of the cells it needs most. Scoring and serving are different jobs and
    # this is the table for the second one.
    con.execute(
        f"""
        create or replace table {FORECAST_TABLE} as
        select
            store_id,
            sku_id,
            -- pandas hands these back as timestamps; downstream joins and date
            -- arithmetic are on dates, and DATE - TIMESTAMP is an interval
            cast(date_day as date) as date_day,
            cast(origin_date as date) as origin_date,
            horizon_days,
            prediction as forecast_units,
            '{MODEL_NAME}' as model_name
        from _eval_predictions
        """
    )
    con.execute(
        f"""
        update {TARGET_TABLE} as accuracy
        set forecast_units = predictions.prediction,
            model_name = '{MODEL_NAME}'
        from predictions
        where
            accuracy.store_id = predictions.store_id
            and accuracy.sku_id = predictions.sku_id
            and accuracy.date_day = predictions.date_day
            and accuracy.horizon_days = predictions.horizon_days
        """
    )

    # Every feature is anchored at the origin, so a series with no observation
    # on its origin date produces no feature row and gets no prediction. Those
    # keep the moving average, and their model_name still says so - a mixed
    # column is the accurate description of a mixed table. Reported rather than
    # left to be discovered, because a silent fallback is indistinguishable
    # from a broken join, and the difference is 436 rows versus all of them.
    fell_back, series = con.execute(
        f"""
        select count(*), count(distinct store_id || sku_id)
        from {TARGET_TABLE} where model_name <> '{MODEL_NAME}'
        """
    ).fetchone()
    if fell_back:
        total = con.execute(f"select count(*) from {TARGET_TABLE}").fetchone()[0]
        print(
            f"  {fell_back:,} of {total:,} rows ({fell_back / total:.3%}) across {series} series "
            f"kept the baseline: no observation on their origin date to anchor features to"
        )


REPORT_SQL = f"""
select
    abc_xyz_class as class,
    count(*) as cells,
    round(sum(actual_units)) as actual_units,
    round(sum(abs(actual_units - forecast_units)) / nullif(sum(actual_units), 0), 3) as wape,
    round(sum(abs(actual_units - baseline_units)) / nullif(sum(actual_units), 0), 3) as ma_wape,
    round(sum(abs(actual_units - naive_units)) / nullif(sum(actual_units), 0), 3) as naive_wape,
    round(sum(forecast_units - actual_units) / nullif(sum(actual_units), 0), 3) as bias,
    round(
        (sum(abs(actual_units - naive_units)) - sum(abs(actual_units - forecast_units)))
        / nullif(sum(actual_units), 0), 3
    ) as fva
from {TARGET_TABLE}
group by abc_xyz_class
order by abc_xyz_class
"""


def report(con: duckdb.DuckDBPyConnection, booster: lgb.Booster) -> int:
    print(f"\n  {MODEL_NAME} against the S3.2 baselines, same origins, same days")
    print(
        f"\n  {'class':<7}{'cells':>10}{'actual':>9}{'WAPE':>8}"
        f"{'7d MA':>8}{'naive':>8}{'zero':>7}{'bias':>8}{'FVA':>8}"
    )
    print("  " + "-" * 72)

    rows = con.execute(REPORT_SQL).fetchall()
    worth_forecasting = []
    for cls, cells, actual, wape, ma_wape, naive_wape, bias, fva in rows:
        # a WAPE of 1.000 is what forecasting zero everywhere scores, so it is
        # the line below which a forecast is earning anything at all
        mark = "  <- beats a zero forecast" if wape < 1.0 else ""
        if wape < 1.0:
            worth_forecasting.append(cls)
        print(
            f"  {cls:<7}{cells:>10,}{actual:>9,.0f}{wape:>8.3f}{ma_wape:>8.3f}"
            f"{naive_wape:>8.3f}{1.0:>7.3f}{bias:>+8.3f}{fva:>+8.3f}{mark}"
        )

    ax_fva = con.execute(
        f"""
        select (sum(abs(actual_units - naive_units)) - sum(abs(actual_units - forecast_units)))
               / nullif(sum(actual_units), 0)
        from {TARGET_TABLE} where abc_xyz_class = 'AX'
        """
    ).fetchone()[0]

    print("\n  top features by gain")
    gains = sorted(
        zip(booster.feature_name(), booster.feature_importance("gain"), strict=True),
        key=lambda kv: -kv[1],
    )
    total = sum(g for _, g in gains) or 1
    for name, gain in gains[:8]:
        print(f"    {name:<24}{100 * gain / total:>6.1f}%")

    print(f"\n  S3.3 gate - FVA against naive on A/X: {ax_fva:+.3f}", end="  ")
    print("PASS" if ax_fva > 0 else "FAIL")
    print(
        f"  classes where a forecast beats predicting zero: {', '.join(worth_forecasting) or 'none'}"
    )
    return 0 if ax_fva > 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Train and score the LightGBM demand model.")
    parser.add_argument("--warehouse", default=str(WAREHOUSE))
    args = parser.parse_args()

    warehouse = Path(args.warehouse)
    if not warehouse.exists():
        print(f"\n\033[31mno warehouse at {warehouse}\033[0m\n  run `python tasks.py build`")
        return 1

    con = duckdb.connect(str(warehouse))
    try:
        con.execute("set memory_limit = '5GB'")
        exists = con.execute(
            """
            select count(*) from information_schema.tables
            where table_name = 'mart_forecast_accuracy'
            """
        ).fetchone()[0]
        if not exists:
            print("\n\033[31mno mart_forecast_accuracy\033[0m\n  run `python tasks.py backtest`")
            return 1

        booster = train(con)
        predict_and_score(con, booster)
        return report(con, booster)
    finally:
        con.execute("drop table if exists main._train_features")
        con.execute("drop table if exists main._eval_features")
        con.execute("drop table if exists main._train_origins")
        con.execute("drop table if exists main._eval_origins")
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
