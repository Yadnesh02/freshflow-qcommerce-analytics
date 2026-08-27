"""Contract for the mart dimensions (task S2.2 onward).

The dbt tests assert the invariants a type 2 dimension has to satisfy - unique
surrogate key, contiguous intervals, lossless expansion back to the source.
This file asserts something they cannot: that the dimension is *worth having*.

A correct SCD2 table over a catalogue whose prices never moved would pass every
structural test in the suite and be a slower copy of the current-state
dimension. So the assertions here are about the shape of the history and about
the size of the mistake the table exists to prevent - if that mistake ever
becomes zero, this model should be deleted rather than maintained.

Needs a built warehouse:

    python tasks.py build
    python -m pytest tests/test_marts.py
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


def _bound(connection) -> None:
    """Cap what a test query may consume.

    dbt gets a memory limit from profiles.yml; these connections got nothing,
    so a heavy test query could claim the whole machine. On Windows DuckDB does
    not fail cleanly when it runs out - the process dies with an access
    violation, which reads like a broken machine rather than a broken query,
    and cost real time to diagnose as exactly that.
    """
    connection.execute("set enable_progress_bar = false")
    connection.execute("set memory_limit = '4GB'")
    connection.execute("set threads = 2")


@pytest.fixture(scope="module")
def con():
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    connection = duckdb.connect(str(WAREHOUSE), read_only=True)
    _bound(connection)
    yield connection
    connection.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


# ============================================== dim_product_snapshot (S2.2)
def test_a_price_change_mid_year_produces_two_versions(con, full_year) -> None:
    """The S2.2 acceptance gate, stated as the plan states it.

    A SKU whose cost or price moved once during the year must appear as exactly
    two rows, the first closed on the day the second opens.
    """
    changed = con.execute(
        """
        select sku_id, count(*) as versions
        from marts.dim_product_snapshot
        group by sku_id
        having count(*) = 2
        """
    ).fetchall()
    assert changed, "no SKU has two versions - the history has nothing to track"

    sku = changed[0][0]
    versions = con.execute(
        """
        select version_no, valid_from_date, valid_to_date, is_current, landed_cost, base_price
        from marts.dim_product_snapshot
        where sku_id = ?
        order by version_no
        """,
        [sku],
    ).fetchall()

    first, second = versions
    assert first[2] == second[1], "the first version does not close where the second opens"
    assert first[3] is False and second[3] is True, "exactly one version should be current"
    assert (first[4], first[5]) != (second[4], second[5]), (
        "two versions with identical cost and price - the split was spurious"
    )


def test_the_change_lands_inside_the_year_not_on_its_edges(con, full_year) -> None:
    """'Mid-year' is doing work in the acceptance gate.

    A version boundary on the first or last day of the window would be an
    artefact of where the data starts and stops, not a price change.
    """
    first_day, last_day = con.execute(
        "select min(snapshot_date), max(snapshot_date) from staging.stg_catalog__products"
    ).fetchone()
    interior = con.execute(
        """
        select count(*) from marts.dim_product_snapshot
        where version_no > 1 and valid_from_date > ? and valid_from_date < ?
        """,
        [first_day, last_day],
    ).fetchone()[0]
    assert interior > 0, "every version boundary sits on the edge of the window"


def test_the_history_is_compressed_not_copied(con, full_year) -> None:
    """1,847 versions out of 547,500 daily rows.

    If this ratio ever approached 1 the model would be a slower, wider copy of
    the staging feed, and the right response would be to delete it.
    """
    versions = one(con, "select count(*) from marts.dim_product_snapshot")
    daily = one(con, "select count(*) from staging.stg_catalog__products")
    assert versions < daily / 50, (
        f"{versions:,} versions for {daily:,} daily rows is barely a collapse"
    )
    assert versions > one(con, "select count(distinct sku_id) from staging.stg_catalog__products")


def test_joining_the_live_catalogue_instead_would_restate_history(con, full_year) -> None:
    """The whole reason this dimension exists, measured in rupees.

    Costs the order book twice: once at the cost that was in force on the day
    of the sale, once at each SKU's current cost - which is what a join to the
    live catalogue silently does. The gap is the amount of margin history that
    would be rewritten by a supplier renegotiation nobody told the warehouse
    about.

    If this ever comes back zero the SCD2 table is decoration, and the test
    should fail rather than let it sit in the DAG unexamined.
    """
    as_of_sale, at_current_cost = con.execute(
        """
        with sold as (
            -- pre-aggregated to store-free SKU-days before the range join.
            -- Feeding 4.26M individual lines into a BETWEEN join against the
            -- SCD2 table exhausts memory and takes DuckDB down with an access
            -- violation rather than a clean error. The grain the question
            -- needs is units per SKU per day, which is ~500k rows.
            select
                sku_id,
                date_day as sale_date,
                sum(signed_qty) as units
            from marts.fct_order_item
            group by sku_id, date_day
        )

        select
            sum(sold.units * historical.landed_cost),
            sum(sold.units * current_version.landed_cost)
        from sold
        join marts.dim_product_snapshot as historical
            on sold.sku_id = historical.sku_id
            and sold.sale_date >= historical.valid_from_date
            and (sold.sale_date < historical.valid_to_date or historical.valid_to_date is null)
        join marts.dim_product_snapshot as current_version
            on sold.sku_id = current_version.sku_id
            and current_version.is_current
        """
    ).fetchone()

    assert as_of_sale != at_current_cost, (
        "costing the order book at current cost gives the same answer as costing "
        "it at the cost in force - the SCD2 dimension is not earning its place"
    )
