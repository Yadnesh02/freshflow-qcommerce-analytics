"""Contract for the deployable slice (task S2.8).

The S2.8 gate is "file size verified; app can open it", and both halves are
checked here. But the assertion that matters most is neither: it is that the
numbers in the slice are the numbers in the warehouse.

A demo that shows different figures from the system it claims to demonstrate is
worse than no demo, because nobody can tell by looking. Every metric below is
computed twice - once from the slice, once from the full warehouse restricted
to the same stores and days - and required to agree exactly. That is the only
version of this test worth having: file size is trivially checkable by anyone,
correctness is not.

Skipped when the slice has not been built, so it never blocks a fresh clone:

    python tasks.py build
    python tasks.py demo-slice
    python -m pytest tests/test_demo_slice.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from analytics.demo_slice import CLUSTER_BY, EXCLUDED, PRUNED, SIZE_LIMIT_MB

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "serving" / "demo" / "freshflow_demo.duckdb"
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

pytestmark = pytest.mark.needs_warehouse

EXPECTED_TABLES = {
    "agg_store_sku_day",
    "dim_customer",
    "dim_date",
    "dim_dte_band",
    "dim_product",
    "dim_product_snapshot",
    "dim_promotion",
    "dim_store",
    "dim_supplier",
    "dq_source_coverage",
    "fct_availability_hour",
    "fct_inventory_batch",
    "fct_order",
    "fct_order_item",
    "fct_price_history",
}


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
def demo():
    if not DEMO.exists():
        pytest.skip(f"no demo slice at {DEMO} - run `python tasks.py demo-slice`")
    connection = duckdb.connect(str(DEMO), read_only=True)
    _bound(connection)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def scope(demo) -> tuple[list[str], object, object]:
    """The stores and window the slice actually covers."""
    stores = [r[0] for r in demo.execute("select store_id from marts.dim_store").fetchall()]
    first_day, last_day = demo.execute(
        "select min(date_day), max(date_day) from marts.agg_store_sku_day"
    ).fetchone()
    return stores, first_day, last_day


def one(con, sql: str, params=None):
    return con.execute(sql, params or []).fetchone()[0]


# ------------------------------------------------------------- the gate
def test_the_slice_fits_under_the_deployment_limit() -> None:
    """Streamlit Community Cloud gives a container, not a database, so the app
    carries its data in the repository. 80 MB is the ceiling this project set
    itself and the reason the slice exists at all."""
    if not DEMO.exists():
        pytest.skip("no demo slice built")
    size_mb = DEMO.stat().st_size / 1_048_576
    assert size_mb < SIZE_LIMIT_MB, f"demo slice is {size_mb:.1f} MB, limit is {SIZE_LIMIT_MB} MB"


def test_the_app_can_open_it_and_find_what_it_needs(demo) -> None:
    """The other half of the gate. A file that opens but is missing a table the
    metric layer reads fails at the first page load, not at build time."""
    present = {
        r[0]
        for r in demo.execute(
            "select table_name from information_schema.tables where table_schema = 'marts'"
        ).fetchall()
    }
    missing = EXPECTED_TABLES - present
    assert not missing, f"the slice is missing {sorted(missing)}"

    empty = [
        t for t in sorted(EXPECTED_TABLES) if one(demo, f"select count(*) from marts.{t}") == 0
    ]
    assert not empty, f"tables present but empty: {empty}"


def test_the_slice_covers_the_window_it_claims(demo, scope) -> None:
    stores, first_day, last_day = scope
    assert len(stores) == 5, f"expected 5 stores, found {len(stores)}"
    assert (last_day - first_day).days == 89, (
        f"expected a 90-day window, found {(last_day - first_day).days + 1} days"
    )


# --------------------------------------------- the assertion that matters
@pytest.mark.parametrize(
    "measure",
    [
        "sum(units_sold)",
        "sum(net_revenue)",
        "sum(cogs)",
        "sum(writeoff_value)",
        "sum(writeoff_units)",
        "sum(received_units)",
        "sum(markdown_subsidy_platform)",
        "sum(units_demanded_imputed)",
        "count(*)",
    ],
)
def test_every_metric_matches_the_full_warehouse(demo, scope, measure: str) -> None:
    """The claim the demo rests on: same question, same answer.

    Computed from the slice and from the warehouse restricted to the identical
    stores and days. If these ever diverge, the slice has stopped being a view
    of the system and become a separate, quieter one - and the only way anyone
    would find out is by trusting a number that was never true.
    """
    if not WAREHOUSE.exists():
        pytest.skip("no full warehouse to compare against")

    stores, first_day, last_day = scope
    placeholders = ", ".join("?" for _ in stores)
    where = f"where store_id in ({placeholders}) and date_day between ? and ?"
    params = [*stores, first_day, last_day]

    from_slice = one(demo, f"select {measure} from marts.agg_store_sku_day {where}", params)

    full = duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        full.execute("set enable_progress_bar = false")
        from_warehouse = full.execute(
            f"select {measure} from marts.agg_store_sku_day {where}", params
        ).fetchone()[0]
    finally:
        full.close()

    if isinstance(from_slice, float) or isinstance(from_warehouse, float):
        assert from_slice == pytest.approx(from_warehouse, abs=0.01), (
            f"{measure}: slice {from_slice} vs warehouse {from_warehouse}"
        )
    else:
        assert from_slice == from_warehouse, (
            f"{measure}: slice {from_slice} vs warehouse {from_warehouse}"
        )


# ------------------------------------------------- what was left out
def test_excluded_tables_are_absent_and_declared(demo) -> None:
    """Omissions have to be deliberate and named.

    An excluded table that nobody wrote down is indistinguishable from one
    somebody forgot, and the difference only surfaces when a page renders
    empty in front of an audience.
    """
    present = {
        r[0]
        for r in demo.execute(
            "select table_name from information_schema.tables where table_schema = 'marts'"
        ).fetchall()
    }
    for table, reason in EXCLUDED.items():
        assert table not in present, f"{table} was meant to be excluded"
        assert reason, f"{table} is excluded without a stated reason"


def test_pruned_columns_are_gone_but_measures_survive(demo) -> None:
    """Narrowing is only safe while it stays confined to build-time machinery.

    Surrogate keys and arrival dates exist so an incremental can merge and a
    replay can rewind. Drop a measure by the same mechanism and the app renders
    a blank chart with no error anywhere.
    """
    for table, dropped in PRUNED.items():
        columns = {
            r[0]
            for r in demo.execute(
                "select column_name from information_schema.columns "
                "where table_schema = 'marts' and table_name = ?",
                [table],
            ).fetchall()
        }
        assert columns, f"{table} has no columns - is it in the slice at all?"
        still_there = set(dropped) & columns
        assert not still_there, f"{table}: expected {sorted(still_there)} to be pruned"

    measures = {
        "units_sold",
        "net_revenue",
        "cogs",
        "writeoff_value",
        "opening_units",
        "closing_units",
        "in_stock_pct",
        "units_demanded_imputed",
        "trailing_7d_avg_units",
    }
    agg_columns = {
        r[0]
        for r in demo.execute(
            "select column_name from information_schema.columns "
            "where table_schema = 'marts' and table_name = 'agg_store_sku_day'"
        ).fetchall()
    }
    assert measures <= agg_columns, f"pruning removed measures: {sorted(measures - agg_columns)}"


def test_the_slice_carries_no_orphan_foreign_keys(demo, scope) -> None:
    """Filtering facts and dimensions separately is how a slice ends up with
    sales pointing at products it does not contain. Every join the app makes
    has to resolve inside the file it was given."""
    orphan_skus = one(
        demo,
        """
        select count(*) from marts.agg_store_sku_day as agg
        where not exists (
            select 1 from marts.dim_product as products where products.sku_id = agg.sku_id)
        """,
    )
    assert orphan_skus == 0, f"{orphan_skus:,} rows reference a SKU not in the slice"

    orphan_stores = one(
        demo,
        """
        select count(*) from marts.agg_store_sku_day as agg
        where not exists (
            select 1 from marts.dim_store as stores where stores.store_id = agg.store_id)
        """,
    )
    assert orphan_stores == 0, f"{orphan_stores:,} rows reference a store not in the slice"

    orphan_customers = one(
        demo,
        """
        select count(*) from marts.fct_order as orders
        where not exists (
            select 1 from marts.dim_customer as customers
            where customers.customer_id = orders.customer_id)
        """,
    )
    assert orphan_customers == 0, f"{orphan_customers:,} orders reference a missing customer"

    orphan_days = one(
        demo,
        """
        select count(*) from marts.agg_store_sku_day as agg
        where not exists (
            select 1 from marts.dim_date as dates where dates.date_day = agg.date_day)
        """,
    )
    assert orphan_days == 0, f"{orphan_days:,} rows reference a day not in dim_date"


def test_every_large_table_is_clustered_on_write(demo) -> None:
    """A table big enough to matter must declare sort keys, or its size is a coin toss.

    Without an explicit `order by`, a table inherits whatever physical row
    order the source warehouse happens to hold - and that order is not stable
    across machines, because dbt builds the warehouse with
    `preserve_insertion_order: false` on three threads. DuckDB picks a
    compression scheme per row group from the data in front of it, so an
    interleaved table compresses far worse than a sorted one.

    The same commit produced 73.3 MB on the development laptop and 90.3 MB on a
    clean CI runner, one side of the 80 MB ceiling each. This test exists so
    the next large table added to the plan cannot quietly reintroduce that.
    """
    rows = dict(
        demo.execute(
            "select table_name, estimated_size from duckdb_tables() where schema_name = 'marts'"
        ).fetchall()
    )
    unclustered = sorted(t for t, n in rows.items() if n and n >= 100_000 and t not in CLUSTER_BY)
    assert not unclustered, (
        f"tables over 100k rows with no CLUSTER_BY entry: {unclustered}. "
        "Their size on disk depends on which machine built them."
    )


def test_every_cluster_key_is_a_real_column(demo) -> None:
    """Sort keys are written by hand against the source schema, so check them.

    A key that no longer exists raises a binder error at build time rather than
    producing a wrong slice, so this is not about correctness of the data - it
    is about failing in a test run rather than eight minutes into CI, after the
    warehouse has already been rebuilt from scratch.
    """
    for table, keys in CLUSTER_BY.items():
        present = {
            r[0]
            for r in demo.execute(
                "select column_name from information_schema.columns "
                "where table_schema = 'marts' and table_name = ?",
                [table],
            ).fetchall()
        }
        if not present:
            continue  # table not in this slice; other tests own that
        missing = [k for k in keys if k not in present]
        assert not missing, f"{table} is clustered on columns it does not have: {missing}"


def test_the_slice_has_headroom_rather_than_just_fitting(demo) -> None:
    """Under the limit is not the same as reliably under it.

    The ceiling is a hard failure in `warehouse.yml`, and the build that
    produces the deployed artefact is the one that has to clear it. Sitting a
    few hundred KB under 80 MB would pass here and fail there for reasons
    nobody controls - a row group boundary landing differently is enough.

    5% rather than something tighter, deliberately. The slice has grown every
    sprint (68.3 -> 74.9 -> 73.3 -> 70.0 MB) and Sprint 5 adds
    `mart_experiment_readout`, so this needs to fire while there is still room
    to act - but a threshold close to the current size would fail on ordinary
    growth rather than on a real problem, and a test that cries wolf gets
    muted, which is worse than not having it.
    """
    mb = DEMO.stat().st_size / 1_048_576
    assert mb < SIZE_LIMIT_MB * 0.95, (
        f"slice is {mb:.1f} MB, inside 5% of the {SIZE_LIMIT_MB} MB ceiling. "
        "Prune a column or drop a table before it fails on a machine you do not own."
    )
