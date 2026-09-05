"""Contract and behaviour for the customer marts (task S3.5).

The gate is "cohort triangle renders; retention decays monotonically", and both
halves of that turned out to be about what gets *excluded* rather than what gets
computed. The tests here are mostly guards against the three ways this table
went wrong while it was being built, each of which produced a plausible-looking
number rather than an error:

  - cohorts whose entire first year predates the order feed read as 0% retained
    and dragged the pooled curve from 75% to 32%;
  - `ntile` scattered a 41%-wide tie block across score bands by sort position;
  - frequency measured over the same 90 days as recency made `at_risk` - the
    one segment worth acting on - unreachable by construction.

Needs a built warehouse:

    python tasks.py build
    python -m pytest tests/test_customer_360.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

pytestmark = pytest.mark.needs_warehouse

SEGMENTS = {
    "champion",
    "loyal",
    "promising",
    "at_risk",
    "hibernating",
    "needs_attention",
    "never_ordered",
}


@pytest.fixture(scope="module")
def con():
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    connection = duckdb.connect(str(WAREHOUSE), read_only=True)
    connection.execute("set enable_progress_bar = false")
    connection.execute("set memory_limit = '4GB'")
    connection.execute("set threads = 2")
    built = connection.execute(
        """
        select count(*) from information_schema.tables
        where table_name in ('mart_customer_360', 'mart_cohort_retention')
        """
    ).fetchone()[0]
    if built < 2:
        connection.close()
        pytest.skip("customer marts not built - run `python tasks.py build`")
    yield connection
    connection.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


@pytest.fixture(scope="module")
def delivery_cost() -> float:
    project = yaml.safe_load((ROOT / "transform" / "dbt_project.yml").read_text(encoding="utf-8"))
    return float(project["vars"]["assumed_delivery_cost_per_order"])


# ==================================================== the gate
def test_pooled_retention_decays_monotonically_from_its_peak(con, full_year) -> None:
    """S3.5's gate.

    Measured from M1, not M0, and that is a property of signup cohorts rather
    than a concession: a customer who signs up on the 28th has two days of
    opportunity in M0 and a full month in M1, so every cohort rises once before
    it decays. Keying cohorts on first-order month would force M0 to 100% and
    hide how many signups never convert at all.
    """
    curve = con.execute(
        """
        select month_index, sum(active_customers) / cast(sum(cohort_size) as double)
        from marts.mart_cohort_retention
        group by month_index order by month_index
        """
    ).fetchall()
    assert len(curve) >= 6, f"only {len(curve)} month indices - not enough to call it a curve"

    values = [rate for _, rate in curve]
    peak = values.index(max(values))
    assert peak <= 1, f"retention peaks at M{peak}, which is not a decay curve at all"

    after_peak = values[peak:]
    rising = [
        (curve[peak + i][0], after_peak[i], after_peak[i + 1])
        for i in range(len(after_peak) - 1)
        if after_peak[i + 1] > after_peak[i] + 1e-9
    ]
    assert not rising, (
        f"retention rises after its peak at {[(m, f'{a:.3f}->{b:.3f}') for m, a, b in rising]}"
    )


def test_no_cohort_predates_the_order_feed(con) -> None:
    """The exclusion that moved pooled retention from 32% to 75%.

    28,000 customers signed up before the first order in the dataset. Their
    cohorts have a known denominator and unobservable early numerators, so they
    read as churned when they are merely invisible.
    """
    stragglers = one(
        con,
        """
        select count(*) from marts.mart_cohort_retention
        where cohort_month < (select date_trunc('month', min(date_day)) from marts.fct_order)
        """,
    )
    assert stragglers == 0, (
        f"{stragglers:,} cohort-months predate the order feed and will read as 0% retained"
    )


def test_the_triangle_is_not_padded_past_what_was_observed(con) -> None:
    """A cohort two months old has no M6, and a zero there is a lie."""
    padded = one(
        con,
        "select count(*) from marts.mart_cohort_retention where month_index > months_observed",
    )
    assert padded == 0, f"{padded:,} rows report a month the cohort has not lived through"


# ==================================================== scoring is not degenerate
def test_every_rfm_score_band_is_reachable(con) -> None:
    """The `ntile` bug and the lifetime-frequency fix, guarded together.

    Frequency scored over `orders_90d` put 41.3% of the base at exactly zero,
    which `cume_dist` maps to a single band and leaves the two below it empty.
    If any band is unreachable, some segment defined against it cannot fire.
    """
    for column in ("r_score", "f_score", "m_score"):
        bands = {
            r[0]
            for r in con.execute(
                f"select distinct {column} from marts.mart_customer_360 where {column} is not null"
            ).fetchall()
        }
        assert bands == {1, 2, 3, 4, 5}, f"{column} only ever takes {sorted(bands)}"


def test_no_single_value_dominates_the_frequency_scale(con) -> None:
    """Why frequency is a lifetime figure rather than a 90-day one.

    `cume_dist` handles ties correctly but cannot rescue a column where one
    value holds a fifth of the mass: that block lands wholesale in whichever
    band its cumulative share falls in and empties the bands below.
    """
    heaviest = one(
        con,
        """
        select max(share) from (
            select count(*) / cast(sum(count(*)) over () as double) as share
            from marts.mart_customer_360
            where orders_lifetime > 0
            group by orders_lifetime
        )
        """,
    )
    assert heaviest < 0.2, (
        f"the most common lifetime order count holds {heaviest:.1%} of the base, enough "
        "to collapse a quintile band"
    )


def test_every_segment_is_populated(con) -> None:
    found = {
        r[0]
        for r in con.execute(
            "select distinct customer_segment from marts.mart_customer_360"
        ).fetchall()
    }
    assert found <= SEGMENTS, f"unexpected segments: {found - SEGMENTS}"
    assert found == SEGMENTS, f"segments never assigned to anyone: {SEGMENTS - found}"


def test_at_risk_customers_are_actually_valuable_and_lapsed(con, full_year) -> None:
    """The segment the whole grid exists to surface.

    Frequency over the recent window correlates -0.55 with recency, so "used to
    buy often, has stopped" is nearly self-contradictory and this segment came
    out empty. Over lifetime it correlates -0.37 and the segment means something.
    """
    at_risk_orders, hibernating_orders = con.execute(
        """
        select
            avg(orders_lifetime) filter (where customer_segment = 'at_risk'),
            avg(orders_lifetime) filter (where customer_segment = 'hibernating')
        from marts.mart_customer_360
        """
    ).fetchone()
    assert at_risk_orders > hibernating_orders * 2, (
        f"at_risk customers average {at_risk_orders:.1f} lifetime orders against "
        f"{hibernating_orders:.1f} for hibernating - the two are not distinguishable, "
        "so the grid is not separating value from lapse"
    )

    recency = one(
        con,
        "select avg(recency_days) from marts.mart_customer_360 where customer_segment = 'at_risk'",
    )
    assert recency > 90, f"at_risk customers averaged {recency:.0f} days recency - not lapsed"


# ==================================================== the assumption is exposed
def test_the_ddi_conclusion_is_disclosed_as_assumption_sensitive(con, delivery_cost) -> None:
    """The delivery cost is invented, and the P7 answer moves with it.

    Contribution by discount-dependency band reorders around 31.3 rupees an
    order, because low-DDI customers order 16.3 times a quarter and high-DDI
    ones 2.5. The figure in use is 42. This test does not assert which band
    wins - it asserts that the crossover is close enough to the assumption that
    the docstring's warning is still true, so nobody reads the ranking as a
    finding about customers when it is a statement about delivery cost.
    """
    low, medium = con.execute(
        """
        select
            avg(gross_margin_90d) filter (where ddi_band = 'low'),
            avg(gross_margin_90d) filter (where ddi_band = 'medium')
        from marts.mart_customer_360 where ddi_band <> 'no_orders'
        """
    ).fetchone()
    low_orders, medium_orders = con.execute(
        """
        select
            avg(orders_90d) filter (where ddi_band = 'low'),
            avg(orders_90d) filter (where ddi_band = 'medium')
        from marts.mart_customer_360 where ddi_band <> 'no_orders'
        """
    ).fetchone()

    crossover = (low - medium) / (low_orders - medium_orders)
    assert 0 < crossover < delivery_cost * 3, (
        f"low and medium DDI contribution cross at Rs {crossover:.1f} against an assumed "
        f"Rs {delivery_cost:.0f} - the sensitivity note in the model docstring needs "
        "rewriting to match"
    )


def test_contribution_subtracts_exactly_the_declared_delivery_cost(con, delivery_cost) -> None:
    """One assumption, one place. If the var and the column disagree, the number
    published is not the one the docstring explains."""
    mismatched = one(
        con,
        f"""
        select count(*) from marts.mart_customer_360
        where abs(delivery_cost_90d - orders_90d * {delivery_cost}) > 1e-9
           or abs(contribution_90d - (gross_margin_90d - delivery_cost_90d)) > 1e-6
        """,
    )
    assert mismatched == 0, f"{mismatched:,} rows do not reconcile to the declared assumption"


def test_the_discount_is_not_charged_twice(con) -> None:
    """The bug this column shipped with for one build, and the reason it was easy.

    `gross_margin` is built in fct_order_item as qty * (realized_price - cogs),
    and realized_price is already net of the discount - measured over this
    warehouse, base - realized - discount is exactly 0.00. Subtracting
    `subsidy_90d` on top therefore charged the discount a second time. It cost
    11.7% of total contribution and fell hardest on the discount-heavy
    customers, which is exactly the population the P7 comparison exists to
    judge, so the error moved the answer and not just the level.

    The registry said to do it, the description said to do it, and every test in
    the suite stayed green. This one would not have.
    """
    margin, subsidy, delivery, published = con.execute(
        """
        select sum(gross_margin_90d), sum(subsidy_90d),
               sum(delivery_cost_90d), sum(contribution_90d)
        from marts.mart_customer_360
        """
    ).fetchone()
    assert abs(published - (margin - delivery)) < 1.0, (
        f"contribution sums to {published:,.0f} against gross margin less delivery of "
        f"{margin - delivery:,.0f} - a gap of {margin - delivery - published:,.0f}, "
        f"against a total subsidy of {subsidy:,.0f}. The discount is being charged twice."
    )


# ==================================================== the registry's contract
def test_the_metric_registry_columns_all_exist(con) -> None:
    """metrics.yml named these before the table did."""
    columns = {
        r[0]
        for r in con.execute(
            """
            select column_name from information_schema.columns
            where table_name = 'mart_customer_360'
            """
        ).fetchall()
    }
    required = {
        "customer_id",
        "store_id",
        "cohort_month",
        "customer_segment",
        "ddi_band",
        "active_curr_90d",
        "active_prev_90d",
        "ordered_m1",
        "orders_90d",
        "promo_orders_90d",
        "gross_margin_90d",
        "delivery_cost_90d",
        "subsidy_90d",
    }
    assert required <= columns, (
        f"metric registry expects columns that do not exist: {required - columns}"
    )


def test_the_retention_windows_do_not_overlap(con) -> None:
    """retention_90d divides the current window by the previous one.

    Overlapping them would count one order on both sides and put a floor under
    retention that has nothing to do with customer behaviour.
    """
    both = one(
        con,
        """
        select count(*) from marts.mart_customer_360
        where active_curr_90d and active_prev_90d
          and orders_90d + orders_prev_90d > orders_lifetime
        """,
    )
    assert both == 0, (
        f"{both:,} customers have more window orders than lifetime orders - the two "
        "90-day windows are double counting"
    )


# Metrics whose window is longer than any single 90-day span. Kept as a named
# set rather than a string check on the SQL, because "90d" appears in the other
# expressions too - they just use one window rather than comparing two.
NEEDS_TWO_WINDOWS = {"retention_90d"}


@pytest.mark.parametrize(
    ("metric", "expression"),
    [
        (
            "retention_90d",
            """
            count(distinct case when active_curr_90d and active_prev_90d then customer_id end)
            / nullif(count(distinct case when active_prev_90d then customer_id end), 0)
        """,
        ),
        (
            "retention_m1",
            """
            count(distinct case when ordered_m1 then customer_id end)
            / nullif(count(distinct customer_id), 0)
        """,
        ),
        (
            "repeat_rate",
            """
            count(distinct case when orders_90d > 1 then customer_id end)
            / nullif(count(distinct customer_id), 0)
        """,
        ),
        (
            "discount_dependency_index",
            """
            sum(promo_orders_90d) / nullif(sum(orders_90d), 0)
        """,
        ),
    ],
)
def test_registry_ratio_metrics_stay_inside_their_declared_bounds(
    con, dataset_days, metric, expression
) -> None:
    """Each metric declares `between: [0.0, 1.0]`. Run its own SQL and check."""
    # retention_90d is the only one here that spans *two* consecutive 90-day
    # windows - it asks who was active in both - so it needs 180 days of
    # history before it is defined at all, and on a shorter dataset its
    # denominator is empty. The other ratios are single-window and hold on any
    # dataset, so gating the whole parametrisation would stop checking two
    # metrics that work perfectly well in the fast lane.
    if metric in NEEDS_TWO_WINDOWS and dataset_days < 180:
        pytest.skip(
            f"{metric} compares two 90-day windows; this warehouse covers {dataset_days} days. "
            "warehouse.yml runs the full year, which is where this one is checked."
        )
    value = one(con, f"select {expression} from marts.mart_customer_360")
    assert value is not None, f"{metric} evaluates to null over the whole table"
    assert 0.0 <= value <= 1.0, f"{metric} computes {value:.4f}, outside its declared [0, 1]"
