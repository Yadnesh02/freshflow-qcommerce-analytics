"""Build the deployable slice of the warehouse (task S2.8).

Streamlit Community Cloud gives you a container, not a database. The app has to
carry its data with it, inside a git repository, which is why this file exists:
the full warehouse is 1.5 GB and the deployable one has to fit under 80 MB.

**Why a slice and not a smaller warehouse.** Every number the app shows must be
the same number the full warehouse produces, or the demo is a different project
wearing the same name. So nothing here aggregates, rounds or recomputes - it
copies rows out of the built marts, restricted to a window and a set of stores
and narrowed to the columns a reader needs. Rows are never altered, so a metric
computed on the slice equals the same metric computed on the full warehouse
restricted the same way, and `tests/test_demo_slice.py` asserts exactly that
rather than trusting it.

**Rebuilding is not free, which is why the file is not committed.** DuckDB's
storage layout is not byte-stable: rebuilding from identical data produces a
different file, and the size drifts by a few hundred KB run to run. Committed,
that would add a fresh ~68 MB blob to git history on every rebuild, whether or
not a single row changed - and history only grows. So the slice is gitignored
and ships as a GitHub Release asset instead; `serving/publish_demo.py` uploads
it and `serving/demo_data.py` fetches it. Building locally does not change what
the deployed app reads until you run `python tasks.py publish-demo`.

**What gets cut, and why that is a modelling decision rather than a filter.**
Slicing by store and day alone does not fit: 5 of 14 stores over 90 of 365 days
is still ~8.8% of 26M rows, and DuckDB stores this data at roughly 59 bytes a
row. Two tables are therefore excluded outright:

  - `fct_inventory_movement`, the stock ledger. It is an audit artefact - every
    figure derived from it is already materialised in `fct_inventory_batch`
    (sold, written off, remaining) and `agg_store_sku_day` (received, written
    off, closing). The reconciliation that needs the raw events runs in CI
    against the full warehouse, which is where it belongs: a deployed demo is
    not the place you audit a ledger from.

  - `fct_clickstream`, which exists to feed the censored-demand correction in
    Sprint 3. That correction runs at build time and lands in
    `agg_store_sku_day.units_demanded_imputed`; the app reads the answer, not
    the evidence.

Both are excluded loudly - `EXCLUDED` below says what and why, the summary
prints it, and a test asserts the app's metric layer never references them. An
undocumented omission would be indistinguishable from having forgotten.

    python tasks.py demo-slice
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = ROOT / "data" / "warehouse" / "freshflow.duckdb"
DEMO = ROOT / "serving" / "demo" / "freshflow_demo.duckdb"

STORE_COUNT = 5
WINDOW_DAYS = 90
SIZE_LIMIT_MB = 80

# Sort keys applied on write, low cardinality first.
#
# **This is a correctness fix, not an optimisation.** Without it the slice
# inherits whatever physical row order the source warehouse happens to hold,
# and that order is not stable across machines: dbt builds the warehouse with
# `preserve_insertion_order: false` on three threads, so which thread finishes
# which chunk decides the layout. DuckDB then chooses a compression scheme per
# row group from the data it sees, and RLE and dictionary encoding are worth
# far more on sorted columns than on interleaved ones. The size of the file is
# therefore a property of the machine that built it.
#
# That is not a theory. The same commit produced **73.3 MB on the development
# laptop and 90.3 MB on a clean CI runner** - identical row counts bar nine,
# the same DuckDB 1.5.5, and one over the 80 MB ceiling while the other sat 7
# MB under it. Measured directly on the two largest tables, ordering is worth
# **20.1%** (92.3 MB -> 73.8 MB).
#
# So the ceiling was never really being tested. A limit that passes or fails on
# thread scheduling is a coin toss with a number written on it, and it landed
# heads every time the check ran on one laptop.
#
# Keys are the ones a reader filters by, which is also what compresses: a
# store's rows land together, then a SKU's, then time runs in order within
# them. Tables absent here are small enough that ordering changes nothing.
CLUSTER_BY = {
    "fct_availability_hour": ("store_id", "sku_id", "date_day", "valid_to_hour_ts_ist"),
    "agg_store_sku_day": ("store_id", "sku_id", "date_day"),
    "fct_order_item": ("store_id", "date_day", "sku_id", "order_id"),
    "fct_inventory_batch": ("store_id", "sku_id", "expiry_date", "batch_id"),
    "fct_order": ("store_id", "date_day", "order_id"),
    "mart_expiry_risk": ("store_id", "sku_id", "expiry_date"),
    "mart_customer_360": ("store_id", "customer_id"),
    "dim_customer": ("customer_id",),
    "rec_purchase_order": ("store_id", "sku_id", "date_day"),
    "fct_price_history": ("store_id", "sku_id", "effective_from_date"),
}

# Columns dropped from the slice. Every one is build-time machinery, not
# something the app reads: surrogate keys exist so an incremental can merge and
# a test can assert uniqueness, and the arrival columns exist so the 48h
# lookback can replay history. They are also expensive - a surrogate key is a
# 32-character md5 string on every row of the two largest tables, which is most
# of the gap between 85.8 MB and fitting.
#
# Rows are never altered, only narrowed: a metric computed on the slice still
# equals the same metric computed on the full warehouse.
PRUNED = {
    "fct_order_item": [
        "order_item_key",
        "arrival_date",
        "order_arrival_date",
        "order_arrival_lag_days",
    ],
    "agg_store_sku_day": ["store_sku_day_key"],
    "fct_price_history": ["price_interval_key", "interval_no"],
    "fct_order": ["arrival_date", "arrival_lag_days"],
    "fct_inventory_batch": ["arrival_date"],
    "dim_product_snapshot": ["dbt_scd_id"],
}

# Left out on purpose, with the reason. See the module docstring.
EXCLUDED = {
    "fct_inventory_movement": "audit ledger; its derived figures are already in the batch and daily marts",
    "fct_clickstream": "1.7M raw events behind the imputation; the app ships the fitted curve instead",
}


def pick_stores(con: duckdb.DuckDBPyConnection, count: int) -> list[str]:
    """The busiest store from each catchment tier, then the next busiest overall.

    Deterministic, and deliberately not just the top N by volume: the tiers
    have different basket composition and elasticity, so a demo made only of
    premium stores would show a network that behaves more uniformly than the
    real one does. Ties break on store_id so a rebuild picks the same five.
    """
    rows = con.execute(
        """
        with volume as (
            select
                stores.store_id,
                stores.catchment_tier,
                count(*) as orders
            from source.marts.fct_order as orders
            join source.marts.dim_store as stores on orders.store_id = stores.store_id
            group by stores.store_id, stores.catchment_tier
        ),

        ranked as (
            select
                *,
                row_number() over (
                    partition by catchment_tier order by orders desc, store_id
                ) as rank_in_tier
            from volume
        )

        select store_id from ranked
        order by rank_in_tier, orders desc, store_id
        """
    ).fetchall()
    return [r[0] for r in rows[:count]]


def build(warehouse: Path, demo: Path, stores: int, days: int) -> dict:
    if not warehouse.exists():
        raise SystemExit(f"no warehouse at {warehouse} - run `python tasks.py build` first")

    demo.parent.mkdir(parents=True, exist_ok=True)
    if demo.exists():
        demo.unlink()

    # Connect to the slice and attach the warehouse read-only, not the other
    # way round: an attachment inherits the parent connection's mode, so a
    # read-only source connection cannot create anything.
    con = duckdb.connect(str(demo))
    con.execute("set enable_progress_bar = false")
    con.execute(f"attach '{warehouse.as_posix()}' as source (read_only)")

    selected = pick_stores(con, stores)
    last_day = con.execute("select max(date_day) from source.marts.agg_store_sku_day").fetchone()[0]
    first_day = con.execute("select ? - ?::integer + 1", [last_day, days]).fetchone()[0]

    store_list = ", ".join(f"'{s}'" for s in selected)
    window = f"date_day between '{first_day}' and '{last_day}'"

    # Each entry is the SELECT that fills one table in the slice. Written out
    # rather than derived, because "which rows does this table need" is a
    # different question for every one of them and a generic filter would
    # silently ship the wrong answer for at least three.
    plan = {
        # --- dimensions -------------------------------------------------
        "dim_store": f"select * from source.marts.dim_store where store_id in ({store_list})",
        "dim_product": "select * from source.marts.dim_product",
        "dim_supplier": "select * from source.marts.dim_supplier",
        "dim_promotion": "select * from source.marts.dim_promotion",
        "dim_dte_band": "select * from source.marts.dim_dte_band",
        # padded past the window so Sprint 3's forecast horizons have rows
        "dim_date": (
            "select * from source.marts.dim_date "
            f"where date_day between '{first_day}'::date - 30 and '{last_day}'::date + 90"
        ),
        # versions overlapping the window, not versions starting inside it -
        # a price set last year is still the price in force today
        "dim_product_snapshot": (
            "select * from source.marts.dim_product_snapshot "
            f"where valid_from_date <= '{last_day}' "
            f"  and (valid_to_date is null or valid_to_date > '{first_day}')"
        ),
        # only customers who actually appear in the sliced orders
        "dim_customer": (
            "select customers.* from source.marts.dim_customer as customers "
            "where customers.customer_id in ("
            f"  select distinct customer_id from source.marts.fct_order "
            f"  where store_id in ({store_list}) and {window})"
        ),
        # --- facts ------------------------------------------------------
        "agg_store_sku_day": (
            f"select * from source.marts.agg_store_sku_day where store_id in ({store_list}) and {window}"
        ),
        # 288 rows, and not filtered by store or window: it is fitted on the
        # whole year across all stores, and a slice of it would be a different
        # curve. This is the evidence behind every lost-sales number the app
        # shows - shipping the conclusion without it would make the headline
        # figure unauditable by the person reading it.
        "agg_intraday_arrival_curve": "select * from source.marts.agg_intraday_arrival_curve",
        # the action queue the app is built around. Filtered to the demo stores
        # but not to the window: it is a single as-of snapshot, and slicing a
        # snapshot by date range would empty it.
        "mart_expiry_risk": (
            f"select * from source.marts.mart_expiry_risk where store_id in ({store_list})"
        ),
        "mart_customer_360": (
            f"select * from source.marts.mart_customer_360 where store_id in ({store_list})"
        ),
        # not filtered by store: cohorts are defined across the estate, and a
        # per-store slice of a cohort is a different cohort with the same name
        "mart_cohort_retention": "select * from source.marts.mart_cohort_retention",
        # Sprint 4's decision engines. Snapshots like mart_expiry_risk, so
        # filtered by store and not by window.
        "rec_markdown": (
            f"select * from source.marts.rec_markdown where store_id in ({store_list})"
        ),
        "rec_deal_slot": (
            f"select * from source.marts.rec_deal_slot where store_id in ({store_list})"
        ),
        # both ends have to be demo stores, or the page shows a transfer to a
        # store the slice does not contain
        "rec_transfer_order": (
            "select * from source.marts.rec_transfer_order "
            f"where from_store in ({store_list}) and to_store in ({store_list})"
        ),
        "rec_purchase_order": (
            f"select * from source.marts.rec_purchase_order where store_id in ({store_list})"
        ),
        # not filtered at all: elasticity is fitted per category and freshness
        # band across the whole estate, so a per-store slice of it is not the
        # coefficient anything was priced against
        "mart_price_elasticity": "select * from source.marts.mart_price_elasticity",
        "fct_order": (
            f"select * from source.marts.fct_order where store_id in ({store_list}) and {window}"
        ),
        "fct_order_item": (
            f"select * from source.marts.fct_order_item where store_id in ({store_list}) and {window}"
        ),
        "fct_availability_hour": (
            "select * from source.marts.fct_availability_hour "
            f"where store_id in ({store_list}) and {window}"
        ),
        "fct_price_history": (
            "select * from source.marts.fct_price_history "
            f"where store_id in ({store_list}) "
            f"  and effective_from_date <= '{last_day}' and effective_to_date > '{first_day}'"
        ),
        # batches whose shelf life overlaps the window, not those received in
        # it: stock received in July is what expires in September, and an
        # expiry-risk page that cannot see it has nothing to rank
        "fct_inventory_batch": (
            "select * from source.marts.fct_inventory_batch "
            f"where store_id in ({store_list}) "
            f"  and received_date <= '{last_day}' and expiry_date >= '{first_day}'"
        ),
        # --- data quality -----------------------------------------------
        "dq_source_coverage": (f"select * from source.marts.dq_source_coverage where {window}"),
    }

    con.execute("create schema if not exists marts")

    counts: dict[str, int] = {}
    for table, query in plan.items():
        if table in PRUNED:
            columns = ", ".join(PRUNED[table])
            query = query.replace("select *", f"select * exclude ({columns})", 1)
        if table in CLUSTER_BY:
            query += " order by " + ", ".join(CLUSTER_BY[table])
        con.execute(f"create table marts.{table} as {query}")
        counts[table] = con.execute(f"select count(*) from marts.{table}").fetchone()[0]

    con.execute("detach source")
    con.execute("checkpoint")
    con.close()

    return {
        "stores": selected,
        "first_day": first_day,
        "last_day": last_day,
        "counts": counts,
        "size_mb": demo.stat().st_size / 1_048_576,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stores", type=int, default=STORE_COUNT)
    parser.add_argument("--days", type=int, default=WINDOW_DAYS)
    parser.add_argument("--warehouse", type=Path, default=WAREHOUSE)
    parser.add_argument("--out", type=Path, default=DEMO)
    args = parser.parse_args(argv)

    result = build(args.warehouse, args.out, args.stores, args.days)

    print(
        f"\ndemo slice: {args.stores} stores x {args.days} days "
        f"({result['first_day']} to {result['last_day']})"
    )
    print(f"  stores: {', '.join(result['stores'])}\n")
    for table, rows in sorted(result["counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {table:<28}{rows:>12,}")
    print(f"  {'TOTAL':<28}{sum(result['counts'].values()):>12,}\n")

    for table, reason in EXCLUDED.items():
        print(f"  excluded {table}: {reason}")

    mb = result["size_mb"]
    verdict = "ok" if mb < SIZE_LIMIT_MB else "OVER LIMIT"
    print(f"\n  {args.out.relative_to(ROOT)}: {mb:.1f} MB  [{verdict}] (limit {SIZE_LIMIT_MB} MB)")
    return 0 if mb < SIZE_LIMIT_MB else 1


if __name__ == "__main__":
    raise SystemExit(main())
