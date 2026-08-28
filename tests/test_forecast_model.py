"""What the demand model has to be true of to ship (task S3.3).

The gate the plan sets is one number - forecast value add against the naive
rule on A/X - and a single number is not enough to trust a model with ordering
decisions. These tests surround it:

  - it must beat the naive rule where the plan says, and beat the moving
    average it replaced somewhere that matters;
  - it must not be *too* good, because on an 80%-zero panel a low WAPE is far
    more likely to mean a leaked feature than a good model;
  - it must move with the calendar, which is the one thing a trailing mean
    structurally cannot do and therefore the only reason to carry a gradient
    booster at all.

That last one exists because the gain table actively argues against it: the
calendar features score about 1% between them and look droppable. They are not,
and this is what says so.

Needs the model to have run:

    python tasks.py build && python tasks.py backtest && python tasks.py forecast
    python -m pytest tests/test_forecast_model.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from analytics.forecasting.train import MODEL_NAME

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

pytestmark = pytest.mark.needs_warehouse

# WAPE of forecasting zero everywhere: the numerator collapses to the sum of
# actuals, so it is exactly 1. The line below which a forecast earns anything.
ZERO_FORECAST_WAPE = 1.0


@pytest.fixture(scope="module")
def con():
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    connection = duckdb.connect(str(WAREHOUSE), read_only=True)
    connection.execute("set enable_progress_bar = false")
    connection.execute("set memory_limit = '4GB'")
    connection.execute("set threads = 2")
    trained = connection.execute(
        """
        select count(*) from information_schema.tables
        where table_name = 'mart_forecast_accuracy'
        """
    ).fetchone()[0]
    if not trained:
        connection.close()
        pytest.skip("no mart_forecast_accuracy - run `python tasks.py backtest`")
    # Present-and-dominant, not exclusive: a handful of rows legitimately keep
    # the baseline because their series has no observation on the origin date.
    # An exclusive check here would turn that into a skip, and skipping is how
    # a test file quietly stops testing anything.
    share = connection.execute(
        f"""
        select count(*) filter (where model_name = '{MODEL_NAME}') / cast(count(*) as double)
        from marts.mart_forecast_accuracy
        """
    ).fetchone()[0]
    if share < 0.5:
        connection.close()
        pytest.skip(f"{MODEL_NAME} holds {share:.1%} of rows - run `python tasks.py forecast`")
    yield connection
    connection.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


def wape(con, where: str = "true") -> float:
    return one(
        con,
        f"""
        select sum(abs(actual_units - forecast_units)) / nullif(sum(actual_units), 0)
        from marts.mart_forecast_accuracy where {where}
        """,
    )


# ============================================================== the plan's gate
def test_forecast_value_add_is_positive_on_ax(con) -> None:
    """S3.3's acceptance gate, stated as the plan states it."""
    fva = one(
        con,
        """
        select (sum(abs(actual_units - naive_units)) - sum(abs(actual_units - forecast_units)))
               / nullif(sum(actual_units), 0)
        from marts.mart_forecast_accuracy where abc_xyz_class = 'AX'
        """,
    )
    assert fva > 0, f"FVA against naive on A/X is {fva:+.3f} - the model has not earned deployment"


def test_the_model_beats_the_naive_rule_on_every_class(con) -> None:
    """Weaker per class than the A/X gate, but it should hold everywhere.

    Losing to seasonal naive on any class would mean the model is worse than
    copying last Tuesday, which is not a tuning problem.
    """
    losses = con.execute(
        """
        select abc_xyz_class,
               (sum(abs(actual_units - naive_units)) - sum(abs(actual_units - forecast_units)))
               / nullif(sum(actual_units), 0) as fva
        from marts.mart_forecast_accuracy
        group by abc_xyz_class having fva <= 0
        """
    ).fetchall()
    assert not losses, f"loses to the naive rule on {[(c, round(f, 3)) for c, f in losses]}"


def test_the_head_is_worth_forecasting_at_all(con) -> None:
    """A/X must beat the zero forecast, or daily forecasting is not well posed.

    On this panel most classes do not clear this bar and are not expected to -
    the C/Z tail sells on 5% of days and belongs to a reorder rule. A/X is the
    region where a forecast is a forecast rather than a formality.
    """
    ax = wape(con, "abc_xyz_class = 'AX'")
    assert ax < ZERO_FORECAST_WAPE, (
        f"A/X WAPE is {ax:.3f} - forecasting zero everywhere would score "
        f"{ZERO_FORECAST_WAPE:.3f}, so this model is not earning its place"
    )


# ============================================================== not too good
def test_the_model_is_not_implausibly_accurate(con) -> None:
    """A leaked feature shows up as a good score, not as a failure.

    The panel is 36-95% zeros depending on class. Anything approaching a tight
    fit on series like that means a column is carrying the answer - the way
    base_price_avg would have, being null on exactly the cells where nothing
    sold.
    """
    overall = wape(con)
    assert overall > 0.3, (
        f"overall WAPE of {overall:.3f} on an intermittent panel indicates leakage, not skill"
    )


def test_predictions_are_non_negative(con) -> None:
    """Demand cannot be negative and downstream divides by this column."""
    negatives = one(
        con,
        "select count(*) from marts.mart_forecast_accuracy where forecast_units < 0",
    )
    assert negatives == 0, f"{negatives:,} negative demand forecasts"


def test_the_only_unpredicted_cells_are_ones_with_no_origin_observation(con) -> None:
    """The fallback has exactly one legitimate cause, and this pins it to that.

    Features are anchored at the origin, so a series with a hole in it on its
    origin date has nothing to anchor to and keeps the baseline. That is correct
    and it is 436 rows across 30 series. What would not be correct is the same
    symptom arising from a mismatched join key - a renamed column, a date cast,
    a horizon off by one - which would silently blend two models into one
    reported WAPE. Both look identical in the model_name column; only the cause
    distinguishes them, so the cause is what gets asserted.
    """
    unexplained = one(
        con,
        f"""
        select count(*)
        from marts.mart_forecast_accuracy as accuracy
        left join marts.agg_store_sku_day as history
            on
                history.store_id = accuracy.store_id
                and history.sku_id = accuracy.sku_id
                and history.date_day = accuracy.origin_date
        where accuracy.model_name <> '{MODEL_NAME}' and history.date_day is not null
        """,
    )
    assert unexplained == 0, (
        f"{unexplained:,} cells kept the baseline despite having an origin observation - "
        "the prediction join is dropping rows for some other reason"
    )

    fell_back = one(
        con,
        f"select count(*) from marts.mart_forecast_accuracy where model_name <> '{MODEL_NAME}'",
    )
    total = one(con, "select count(*) from marts.mart_forecast_accuracy")
    assert fell_back < total * 0.001, (
        f"{fell_back:,} of {total:,} rows fell back to the baseline - too many for panel "
        "holes alone, so the join is likely at fault"
    )


# ============================================================== the calendar
def test_the_model_tracks_the_festival_lift(con) -> None:
    """The only structural reason to prefer this over a trailing mean.

    Demand on festival days runs 54% above ordinary days, and a moving average
    cannot anticipate that by construction - it under-forecasts festival
    weekends by 27%. If this model ever flattens to the same behaviour, the
    calendar features have stopped working and the gain table will not say so,
    because they only ever accounted for about 1% of it.
    """
    rows = con.execute(
        """
        select
            calendar.is_festival,
            avg(accuracy.actual_units) as actual,
            avg(accuracy.forecast_units) as model,
            avg(accuracy.baseline_units) as moving_average
        from marts.mart_forecast_accuracy as accuracy
        join marts.dim_date as calendar using (date_day)
        group by calendar.is_festival
        """
    ).fetchall()
    by_flag = {bool(r[0]): r[1:] for r in rows}
    assert set(by_flag) == {True, False}, "the evaluation window contains no festival days"

    ordinary_actual, ordinary_model, ordinary_ma = by_flag[False]
    festive_actual, festive_model, festive_ma = by_flag[True]

    actual_lift = festive_actual / ordinary_actual - 1
    model_lift = festive_model / ordinary_model - 1
    ma_lift = festive_ma / ordinary_ma - 1

    assert actual_lift > 0.1, f"no festival lift to track in this window ({actual_lift:+.1%})"
    assert model_lift > actual_lift * 0.5, (
        f"the model lifts {model_lift:+.1%} against an actual {actual_lift:+.1%} - "
        "it is not using the calendar"
    )
    assert model_lift > ma_lift, (
        f"the model tracks the festival no better than the trailing mean "
        f"({model_lift:+.1%} vs {ma_lift:+.1%}), which is the one thing it exists to do"
    )


def test_the_model_tracks_the_weekend_lift(con) -> None:
    """Same argument, 104 days a year instead of 37."""
    rows = con.execute(
        """
        select
            calendar.is_weekend,
            avg(accuracy.actual_units) as actual,
            avg(accuracy.forecast_units) as model
        from marts.mart_forecast_accuracy as accuracy
        join marts.dim_date as calendar using (date_day)
        group by calendar.is_weekend
        """
    ).fetchall()
    by_flag = {bool(r[0]): r[1:] for r in rows}
    actual_lift = by_flag[True][0] / by_flag[False][0] - 1
    model_lift = by_flag[True][1] / by_flag[False][1] - 1

    assert actual_lift > 0.1, f"no weekend lift in this window ({actual_lift:+.1%})"
    assert model_lift > actual_lift * 0.5, (
        f"the model lifts {model_lift:+.1%} into the weekend against an actual {actual_lift:+.1%}"
    )
