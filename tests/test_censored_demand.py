"""What the censored-demand correction claims, checked against the warehouse (S3.1).

The dbt tests hold the boundaries: imputed never drops below observed, the
method column only ever carries one of three values, each category's curve sums
to one. Those keep the model from being malformed. None of them would notice if
it were merely useless - a curve identical to the uniform baseline passes every
one of them, and so does one fitted entirely on sampling noise.

So this file asserts the things that make the model worth having over the
baseline it replaced:

  - the curve has a shape, and it is the shape of a grocery day
  - categories that brought no evidence did not get a distinctive curve anyway
  - the correction moves morning and evening stockouts in *opposite*
    directions, which a fudge factor cannot do
  - the cells the old code silently scored as losing nothing no longer do

Needs a built warehouse:

    python tasks.py build
    python -m pytest tests/test_censored_demand.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

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
    yield connection
    connection.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


# ==================================================== the curve has a shape
def test_the_day_has_a_morning_and_an_evening_peak(con) -> None:
    """A flat curve would make the whole correction a no-op with extra steps.

    The bars are set well under the fitted values - 1.44x and 1.77x against a
    flat day, overnight at 0.05x - because the claim being made is "this is
    shaped like a grocery day", not "these are today's numbers". A test pinned
    to the current fit would fail on the 30-day CI slice for reasons that have
    nothing to do with the model being wrong.

    Worth knowing if these ever move: the fitted morning is *flatter* than the
    raw event mix, which puts 33% in 06:00-09:00 against 23.9% here. That gap
    is the `was_in_stock` filter working. Out-of-stock views cluster in the
    morning because that is when shelves empty, so leaving them in would have
    steepened exactly the peak the correction is most sensitive to.
    """
    shares = dict(
        con.execute(
            """
            select hour_ist, avg(arrival_share)
            from marts.agg_intraday_arrival_curve
            group by hour_ist
            """
        ).fetchall()
    )
    morning = sum(shares[h] for h in range(6, 10))
    evening = sum(shares[h] for h in range(17, 21))
    overnight = sum(shares[h] for h in range(1, 5))
    flat = 4 / 24

    assert morning > flat * 1.25, f"morning rush is not a rush: {morning / flat:.2f}x flat"
    assert evening > flat * 1.4, f"evening rush is not a rush: {evening / flat:.2f}x flat"
    assert evening > morning, "the evening peak should be the taller of the two"
    assert overnight < flat * 0.25, f"the small hours are not quiet: {overnight / flat:.2f}x flat"


def test_the_curve_is_not_just_the_uniform_baseline(con) -> None:
    """If it were, dividing by it would reproduce the model this one replaced."""
    max_gap = one(
        con,
        """
        select max(abs(cumulative_share_before - hour_ist / 24.0))
        from marts.agg_intraday_arrival_curve
        """,
    )
    assert max_gap > 0.15, (
        f"curve never departs from the clock by more than {max_gap:.3f} - "
        "the correction would be cosmetic"
    )


# ==================================================== shrinkage did its job
def test_a_category_with_little_evidence_does_not_get_a_distinctive_curve(con) -> None:
    """The trap this model was built to avoid.

    Bakery and Meat, Fish & Seafood show the largest raw deviation from the
    global curve and have the least data behind it. Left unshrunk they would
    carry the most opinionated curves in the table on the least evidence, and
    both are perishable enough that the expiry model leans on them hardest.
    """
    rows = con.execute(
        """
        select l1_category, max(category_events), max(signal_weight)
        from marts.agg_intraday_arrival_curve
        group by l1_category
        """
    ).fetchall()
    thinnest = min(rows, key=lambda r: r[1])
    thickest = max(rows, key=lambda r: r[1])

    assert thinnest[2] < thickest[2], (
        f"{thinnest[0]} ({thinnest[1]} events, weight {thinnest[2]:.2f}) is trusted as much as "
        f"{thickest[0]} ({thickest[1]} events, weight {thickest[2]:.2f})"
    )
    assert thinnest[2] < 0.8, f"{thinnest[0]} keeps {thinnest[2]:.2f} of a curve built on noise"


def test_every_category_keeps_some_of_its_own_shape(con) -> None:
    """Shrinkage that collapsed everything to global would erase the signal."""
    weakest = one(con, "select min(signal_weight) from marts.agg_intraday_arrival_curve")
    assert weakest > 0.25, f"a category was shrunk to {weakest:.2f} - that is not a blend"


# ==================================================== the correction's direction
def test_the_correction_reverses_direction_between_morning_and_evening(con) -> None:
    """The claim a fudge factor cannot make.

    Scaling by the clock credits a 08:00 stockout with a third of the day, when
    only an eighth of demand had arrived - so the correction must impute *more*
    there. By 21:00 the clock says 87.5% and the curve says over 90%, so it must
    impute *less*. A correction that only ever pushed one way would be a
    multiplier wearing a model's clothes.
    """
    rows = con.execute(
        """
        select
            case when ran_out_at_hour <= 10 then 'morning' else 'evening' end as part_of_day,
            sum(units_demanded_imputed) as with_curve,
            sum(round(units_sold * 24.0 / ran_out_at_hour)) as with_clock
        from marts.agg_store_sku_day
        where demand_imputation_method = 'arrival_curve_scaled'
            and (ran_out_at_hour <= 10 or ran_out_at_hour >= 21)
        group by 1
        """
    ).fetchall()
    by_part = {r[0]: (r[1], r[2]) for r in rows}
    assert set(by_part) == {"morning", "evening"}, f"expected both halves, got {list(by_part)}"

    morning_curve, morning_clock = by_part["morning"]
    evening_curve, evening_clock = by_part["evening"]

    assert morning_curve > morning_clock, (
        f"morning stockouts impute {morning_curve:.0f} against the clock's {morning_clock:.0f} - "
        "the correction is not recovering the demand the clock misses"
    )
    assert evening_curve < evening_clock, (
        f"evening stockouts impute {evening_curve:.0f} against the clock's {evening_clock:.0f} - "
        "a correction that only ever adds is a multiplier, not a model"
    )


def test_lost_sales_are_material_but_not_absurd(con) -> None:
    """Both failure directions at once: a correction that does nothing, and one
    that has run away. The morning/evening test above is what shows it beats the
    baseline; this only bounds the total it produces."""
    lost_now, sold = con.execute(
        """
        select sum(units_demanded_imputed - units_sold), sum(units_sold)
        from marts.agg_store_sku_day
        """
    ).fetchone()
    assert lost_now > 0, "no lost sales at all - the correction is not running"
    assert lost_now / sold < 0.5, (
        f"lost sales are {lost_now / sold:.1%} of units sold - implausible enough to "
        "suggest the divisor is wrong rather than the demand large"
    )


# ==================================================== the estimator boundaries
def test_all_three_estimators_are_used(con) -> None:
    """A branch nothing reaches is a branch nobody has tested."""
    methods = dict(
        con.execute(
            """
            select demand_imputation_method, count(*)
            from marts.agg_store_sku_day group by 1
            """
        ).fetchall()
    )
    assert set(methods) == {"observed", "arrival_curve_scaled", "trailing_mean"}, (
        f"expected all three estimators, got {sorted(methods)}"
    )
    assert all(n > 0 for n in methods.values())


def test_scaling_is_never_applied_to_a_cell_with_too_little_exposure(con) -> None:
    """The threshold is what stops 5 units becoming 733."""
    worst = one(
        con,
        """
        select min(demand_share_before_stockout)
        from marts.agg_store_sku_day
        where demand_imputation_method = 'arrival_curve_scaled'
        """,
    )
    assert worst >= 0.10, f"a cell was scaled on {worst:.3f} of a day's demand"


def test_no_cell_is_scaled_by_an_implausible_multiplier(con) -> None:
    """The threshold bounds the divisor, so it must bound the multiplier too."""
    biggest = one(
        con,
        """
        select max(units_demanded_imputed / nullif(units_sold, 0))
        from marts.agg_store_sku_day
        where demand_imputation_method = 'arrival_curve_scaled' and units_sold > 0
        """,
    )
    assert biggest <= 10, f"a cell's demand was scaled {biggest:.1f}x from what it sold"


def test_a_full_day_stockout_no_longer_reports_zero_lost_sales(con) -> None:
    """The regression the trailing-mean fallback was added to close.

    The old code imputed these at observed sales, which on a day the shelf was
    empty from opening means zero. They are the most censored cells in the
    table and they were reporting no lost demand at all.
    """
    cells, with_demand = con.execute(
        """
        select count(*), count(*) filter (where units_demanded_imputed > units_sold)
        from marts.agg_store_sku_day
        where is_censored and hours_in_stock = 0
        """
    ).fetchone()
    if not cells:
        pytest.skip("no full-day stockouts in this window")
    assert with_demand > 0, (
        f"all {cells} full-day stockouts still impute zero lost demand - "
        "the trailing-mean fallback is not reaching them"
    )
