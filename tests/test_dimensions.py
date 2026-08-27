"""Contract for the six conformed dimensions (task S2.3).

The dbt tests assert each dimension's key is unique and not null, which is the
acceptance gate. This file asserts the things a passing gate would not notice:
that the classifications have the shape they claim, that the unknown member is
actually load-bearing, and that the two hand-maintained seeds have not drifted
from the events they describe.

Needs a built warehouse:

    python tasks.py build
    python -m pytest tests/test_dimensions.py
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

DIMENSIONS = {
    "dim_store": "store_id",
    "dim_product": "sku_id",
    "dim_customer": "customer_id",
    "dim_date": "date_day",
    "dim_supplier": "supplier_id",
    "dim_promotion": "promo_id",
}


@pytest.fixture(scope="module")
def con():
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    connection = duckdb.connect(str(WAREHOUSE), read_only=True)
    yield connection
    connection.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


# ------------------------------------------------------------- the gate
@pytest.mark.parametrize("table,key", sorted(DIMENSIONS.items()))
def test_every_dimension_has_a_unique_non_null_key(con, table: str, key: str) -> None:
    """The S2.3 acceptance gate, swept across all six at once.

    dbt tests each of these individually. Asserting it here as one loop over a
    declared list is what stops a seventh dimension arriving in S2.4 without
    one - which is how a fact table ends up fanning out on a join nobody
    thought to check.
    """
    rows, distinct, nulls = con.execute(
        f"""
        select count(*), count(distinct {key}), count(*) filter (where {key} is null)
        from marts.{table}
        """
    ).fetchone()
    assert rows > 0, f"{table} is empty"
    assert nulls == 0, f"{table}.{key} has {nulls:,} nulls"
    assert rows == distinct, f"{table}.{key} is not unique: {rows:,} rows, {distinct:,} keys"


# ------------------------------------------------------------ dim_supplier
def test_the_supplier_dimension_carries_a_load_bearing_unknown_member(con) -> None:
    """The S2.1 finding, closed and kept closed.

    SUP-OPENING is a sentinel on the day-one batches, not a supplier. Without
    an explicit row for it every supplier rollup is an inner join that drops
    opening stock, and the result is a slightly smaller number rather than an
    error. The last assertion is the one that matters: it checks the member is
    carrying real volume, so nobody can delete a null-heavy row while tidying
    and leave the tests green.
    """
    unknown = one(con, "select count(*) from marts.dim_supplier where is_unknown_member")
    assert unknown == 1, f"expected exactly one unknown member, found {unknown}"

    orphans = one(
        con,
        """
        select count(*) from staging.stg_wms__inventory_batches as batches
        where not exists (
            select 1 from marts.dim_supplier as suppliers
            where suppliers.supplier_id = batches.supplier_id)
        """,
    )
    assert orphans == 0, f"{orphans:,} batches still resolve to no supplier"

    carried = one(
        con,
        """
        select count(*) from staging.stg_wms__inventory_batches as batches
        join marts.dim_supplier as suppliers using (supplier_id)
        where suppliers.is_unknown_member
        """,
    )
    assert carried > 15_000, (
        f"the unknown member carries only {carried:,} batches - either the sentinel "
        "has left the feed or the join is not doing what it claims"
    )


# --------------------------------------------------------------- dim_store
def test_every_store_has_a_capacity_and_a_radius(con) -> None:
    """Regression: stores.yaml declared a `defaults` block that nothing merged.

    Twelve of fourteen stores emitted a null chilled capacity, thirteen a null
    ambient capacity, and serviceable_radius_km never reached the feed at all.
    Invisible in the simulator, which never reads those values, and immediately
    visible the first time a dimension tried to use one.
    """
    incomplete = one(
        con,
        """
        select count(*) from marts.dim_store
        where chilled_capacity_units is null
           or ambient_capacity_units is null
           or frozen_capacity_units is null
           or serviceable_radius_km is null
        """,
    )
    assert incomplete == 0, f"{incomplete} stores are missing a capacity or a radius"

    low, high = con.execute(
        "select min(chilled_capacity_share), max(chilled_capacity_share) from marts.dim_store"
    ).fetchone()
    assert 0 < low <= high < 1, f"implausible chilled capacity share range: {low} to {high}"


# ------------------------------------------------------------- dim_product
def test_the_abc_split_follows_the_pareto_boundaries(con) -> None:
    """A is the head producing the first 80% of revenue, not the top 80% of SKUs.

    Getting that backwards produces a classification that runs, validates, and
    is useless - hundreds of A-class SKUs and no basis for treating any of them
    differently. The shape is the test: a small minority of SKUs carrying most
    of the money.
    """
    rows = con.execute(
        """
        select abc_class, count(*) as skus, sum(net_revenue_ltd) as revenue
        from marts.dim_product
        group by abc_class
        """
    ).fetchall()
    by_class = {r[0]: (r[1], r[2]) for r in rows}
    assert set(by_class) == {"A", "B", "C"}

    total_skus = sum(v[0] for v in by_class.values())
    total_revenue = sum(v[1] for v in by_class.values())
    a_skus, a_revenue = by_class["A"]

    assert a_skus / total_skus < 0.5, "class A is not a head - the boundary is inverted"
    assert a_revenue / total_revenue > 0.7, "class A does not carry the revenue it should"


def test_the_never_sold_skus_are_classified_and_flagged(con) -> None:
    """SKUs that never sold get C and Z, plus a flag saying which C they are.

    A fourth class would break the dimension registry, which declares exactly
    three values. Null would break every group-by that trusts it. The flag is
    what lets an analysis tell 'C because long tail' from 'C because it has
    never sold a single unit', which are different problems with different
    answers.
    """
    unsold, misclassified = con.execute(
        """
        select
            count(*) filter (where not has_sales),
            count(*) filter (where not has_sales and (abc_class <> 'C' or xyz_class <> 'Z'))
        from marts.dim_product
        """
    ).fetchone()
    assert unsold > 0, "every SKU sold - this test proves nothing"
    assert misclassified == 0, f"{misclassified} never-sold SKUs are not classified CZ"


def test_the_xyz_split_separates_forecastable_from_erratic(con, full_year) -> None:
    """X must actually be steadier than Z, or the class labels are decorative."""
    x_cv, z_cv = con.execute(
        """
        select
            avg(demand_cv) filter (where xyz_class = 'X'),
            avg(demand_cv) filter (where xyz_class = 'Z' and demand_cv is not null)
        from marts.dim_product
        """
    ).fetchone()
    assert x_cv is not None and z_cv is not None, "one of the classes is empty"
    assert x_cv < z_cv, f"X ({x_cv:.3f}) is not steadier than Z ({z_cv:.3f})"


# ---------------------------------------------------------------- dim_date
def test_the_calendar_flags_line_up_with_the_festival_seed(con) -> None:
    """The seed is hand-maintained, so the failure mode is drift.

    Every seeded festival must light up at least one day, and no day may be
    flagged as a festival without naming one. Asserted exactly rather than with
    slack: the calendar spine is padded a month back and a quarter forward
    precisely so that nothing in the seed falls off its edges, and a festival
    that stopped landing would mean either the seed or the spine moved.
    """
    inconsistent = one(
        con,
        "select count(*) from marts.dim_date where is_festival <> (festival_name is not null)",
    )
    assert inconsistent == 0

    # only festivals whose dates fall inside the calendar's span can land in it;
    # on a short window most of the seed is simply out of range, which is not
    # drift and must not read as it
    seeded_in_range = one(
        con,
        """
        select count(*) from seeds.calendar_events as events
        where events.event_type = 'festival'
          and cast(events.start_date as date)
              <= (select max(date_day) from marts.dim_date)
          and cast(events.start_date as date) + cast(events.duration_days as integer) - 1
              >= (select min(date_day) from marts.dim_date)
        """,
    )
    landed = one(con, "select count(distinct festival_name) from marts.dim_date where is_festival")
    assert landed == seeded_in_range, (
        f"{seeded_in_range - landed} festivals fall inside the calendar's span but "
        "never light up a day"
    )


def test_the_ipl_window_comes_from_the_seed_and_lands_in_spring(con, full_year) -> None:
    """The season is reference data, not a literal buried in the model.

    It is a window, not a fixture list - which nights actually had a match is a
    coin flip inside the simulator and cannot be recovered by joining. Naming
    the column `is_ipl_window` and sourcing its dates from the same seed as the
    festivals keeps both facts visible.
    """
    days, first_day, last_day = con.execute(
        """
        select count(*), min(date_day), max(date_day)
        from marts.dim_date where is_ipl_window
        """
    ).fetchone()
    assert days > 0, "the IPL season never appears in the calendar"
    assert first_day.month == 3 and last_day.month == 5, (
        f"the IPL window runs {first_day} to {last_day}, which is not a spring season"
    )


def test_the_calendar_carries_no_simulator_multipliers(con) -> None:
    """The boundary that makes any downstream finding worth something.

    dim_date may know that Diwali is on 20 October - that is a published
    calendar. It may not know that Diwali multiplies snack demand by 1.9, which
    is a generator parameter, and inferring it from emitted events is exactly
    what the forecasting work in Sprint 3 has to earn.
    """
    columns = {
        row[0]
        for row in con.execute(
            """
            select column_name from information_schema.columns
            where table_schema = 'marts' and table_name = 'dim_date'
            """
        ).fetchall()
    }
    leaked = {c for c in columns if "factor" in c or "multiplier" in c or "boost" in c}
    assert not leaked, f"dim_date carries simulator parameters: {sorted(leaked)}"


# ----------------------------------------------------------- dim_promotion
def test_the_promo_master_covers_everything_that_ran(con) -> None:
    """A promo with no master row joins to nulls and drops out of every
    funding-source split - so its subsidy stops counting against
    GM-after-wastage and margin improves because a promotion was forgotten.
    """
    uncovered = one(
        con,
        """
        select count(distinct promo_id) from staging.stg_pos__order_lines as lines
        where promo_id is not null
          and not exists (
              select 1 from marts.dim_promotion as master
              where master.promo_id = lines.promo_id)
        """,
    )
    assert uncovered == 0, f"{uncovered} promotions ran with no master row"


def test_the_contracted_promo_depth_matches_what_was_executed(con) -> None:
    """The seed says 30%; the ledger should agree.

    This is what makes splitting dim_promotion between a seed and the data
    worth doing. A promo configured one way and executed another is otherwise
    a margin variance with nothing attached to explain it.
    """
    drifted = con.execute(
        """
        select promo_id, contracted_depth_pct, observed_depth_pct
        from marts.dim_promotion
        where contracted_depth_pct is not null
          and abs(contracted_depth_pct - observed_depth_pct) > 0.02
        """
    ).fetchall()
    assert not drifted, f"promo depth drifted from contract: {drifted}"
