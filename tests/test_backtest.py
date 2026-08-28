"""What the backtest protocol promises, checked against its output (S3.2).

An evaluation harness is the one piece of a forecasting project that nothing
else can check. A broken model shows up as bad numbers; a broken *evaluation*
shows up as good ones, and the better it is broken the better they look. So the
assertions here are almost all about what the harness must never do rather than
about how accurate the baselines turned out.

The one that matters most is leakage: if a forecast for day D were computed from
data including day D, WAPE would collapse toward zero and every conclusion drawn
afterwards - including S3.3's decision about whether LightGBM is worth shipping -
would be drawn from a number that measured nothing.

Needs the backtest to have run:

    python tasks.py build
    python tasks.py backtest
    python -m pytest tests/test_backtest.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from analytics.forecasting.backtest import (
    MA_WINDOW_DAYS,
    MAX_HORIZON,
    ORIGIN_COUNT,
    ORIGIN_STEP_DAYS,
    SEASONAL_LAG_DAYS,
)

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

pytestmark = pytest.mark.needs_warehouse


@pytest.fixture(scope="module")
def con():
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    connection = duckdb.connect(str(WAREHOUSE), read_only=True)
    connection.execute("set enable_progress_bar = false")
    connection.execute("set memory_limit = '4GB'")
    connection.execute("set threads = 2")
    exists = connection.execute(
        """
        select count(*) from information_schema.tables
        where table_name = 'mart_forecast_accuracy'
        """
    ).fetchone()[0]
    if not exists:
        connection.close()
        pytest.skip("no mart_forecast_accuracy - run `python tasks.py backtest`")
    yield connection
    connection.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


# ========================================================= the protocol holds
def test_no_forecast_reads_a_day_its_origin_could_not_see(con) -> None:
    """Leakage, stated directly. Everything downstream rests on this."""
    leaked = one(
        con,
        "select count(*) from marts.mart_forecast_accuracy where date_day <= origin_date",
    )
    assert leaked == 0, f"{leaked:,} rows score a day at or before their own origin"


def test_the_seasonal_reference_is_always_within_the_origin(con) -> None:
    """Seasonal naive reads date_day - 7, which must not sit past the origin.

    This is why horizons stop at 7: at horizon 8 the lag-7 day would be the day
    after the origin, and the naive rule every model is measured against would
    be reading the future it is supposed to be predicting.
    """
    assert MAX_HORIZON <= SEASONAL_LAG_DAYS, (
        f"horizon {MAX_HORIZON} exceeds the seasonal lag {SEASONAL_LAG_DAYS}, "
        "so the naive reference reads past its origin"
    )
    beyond = one(
        con,
        f"""
        select count(*) from marts.mart_forecast_accuracy
        where date_day - {SEASONAL_LAG_DAYS} > origin_date
        """,
    )
    assert beyond == 0, f"{beyond:,} rows take their naive reference from after the origin"


def test_horizons_are_not_locked_to_a_weekday(con) -> None:
    """The confound that made the first run's horizon table meaningless.

    With origins a whole week apart, horizon h lands on the same weekday every
    time, so WAPE-by-horizon reports the weekday's demand level instead of the
    horizon's difficulty. Measured before the step was changed: h=5 was always
    Saturday at 0.87 units/day and h=1 always Tuesday at 0.61, which made h=5
    look the most accurate purely by carrying the largest denominator.
    """
    assert ORIGIN_STEP_DAYS % 7 != 0, (
        f"origin step {ORIGIN_STEP_DAYS} is a multiple of 7 - horizon and weekday "
        "are the same variable again"
    )
    worst = one(
        con,
        """
        select min(n) from (
            select horizon_days, count(distinct dayname(date_day)) as n
            from marts.mart_forecast_accuracy group by horizon_days
        )
        """,
    )
    assert worst >= 5, f"some horizon covers only {worst} weekdays - the confound is back"


def test_every_origin_and_horizon_is_represented(con) -> None:
    origins, horizons = con.execute(
        """
        select count(distinct origin_date), count(distinct horizon_days)
        from marts.mart_forecast_accuracy
        """
    ).fetchone()
    assert origins == ORIGIN_COUNT, f"expected {ORIGIN_COUNT} origins, got {origins}"
    assert horizons == MAX_HORIZON, f"expected {MAX_HORIZON} horizons, got {horizons}"


def test_scoring_excludes_censored_days(con) -> None:
    """On a censored day the actual is S3.1's estimate, not an observation.

    Scoring against it would measure how closely the forecast agrees with the
    imputation, which is not accuracy and would improve if both were wrong in
    the same direction.
    """
    censored = one(
        con,
        """
        select count(*)
        from marts.mart_forecast_accuracy as f
        join marts.agg_store_sku_day as a using (store_id, sku_id, date_day)
        where a.is_censored
        """,
    )
    assert censored == 0, f"{censored:,} scored cells fall on censored days"


# ========================================================= the numbers are real
def test_the_baseline_is_not_accidentally_perfect(con) -> None:
    """A WAPE near zero on intermittent demand means leakage, not skill."""
    wape = one(
        con,
        """
        select sum(abs(actual_units - forecast_units)) / nullif(sum(actual_units), 0)
        from marts.mart_forecast_accuracy
        """,
    )
    assert wape > 0.3, f"overall WAPE of {wape:.3f} on 80%-zero series is implausibly good"
    assert wape < 3.0, f"overall WAPE of {wape:.3f} exceeds the metric registry's own bound"


def test_the_moving_average_beats_the_naive_rule_on_the_head(con) -> None:
    """S3.2's reason to exist. If it does not hold, report the naive rule instead.

    Asserted on A/X only - the head is where a baseline should be able to win,
    and the plan already expects the C/Z tail to be told by a rule rather than a
    model. A future model that loses here has not earned deployment.
    """
    fva = one(
        con,
        """
        select (sum(abs(actual_units - naive_units)) - sum(abs(actual_units - forecast_units)))
               / nullif(sum(actual_units), 0)
        from marts.mart_forecast_accuracy where abc_xyz_class = 'AX'
        """,
    )
    assert fva > 0, (
        f"the moving average adds {fva:+.3f} against naive on AX - it is not earning its place"
    )


def test_accuracy_degrades_toward_the_tail(con) -> None:
    """AX should be the most forecastable class in the table.

    If the intermittent tail ever scored better than the head, the metric is
    measuring volume rather than accuracy and the whole report is upside down.
    """
    rows = dict(
        con.execute(
            """
            select abc_xyz_class,
                   sum(abs(actual_units - forecast_units)) / nullif(sum(actual_units), 0)
            from marts.mart_forecast_accuracy group by abc_xyz_class
            """
        ).fetchall()
    )
    assert rows["AX"] == min(rows.values()), (
        f"AX is not the most accurate class: {sorted(rows.items(), key=lambda kv: kv[1])[:3]}"
    )


def test_the_registry_metrics_resolve_against_this_table(con) -> None:
    """metrics.yml named this table before it existed. It has to fit the contract."""
    columns = {
        r[0]
        for r in con.execute(
            """
            select column_name from information_schema.columns
            where table_name = 'mart_forecast_accuracy'
            """
        ).fetchall()
    }
    required = {
        "store_id",
        "sku_id",
        "date_day",
        "horizon_days",
        "actual_units",
        "forecast_units",
        "naive_units",
        "abc_class",
        "xyz_class",
    }
    assert required <= columns, f"missing columns the metric registry expects: {required - columns}"


def test_the_moving_average_window_is_what_it_claims(con) -> None:
    """A flat forecast repeated across horizons, one value per series per origin."""
    varying = one(
        con,
        """
        select count(*) from (
            select store_id, sku_id, origin_date
            from marts.mart_forecast_accuracy
            group by store_id, sku_id, origin_date
            having count(distinct round(forecast_units, 9)) > 1
        )
        """,
    )
    assert varying == 0, (
        f"{varying:,} series-origins carry more than one forecast value across horizons - "
        f"a {MA_WINDOW_DAYS}-day mean does not vary with horizon"
    )
