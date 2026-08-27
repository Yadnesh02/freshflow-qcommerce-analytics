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

**Rebuilding is not free, and the file is committed.** DuckDB's storage layout
is not byte-stable: rebuilding from identical data produces a different file,
and the size drifts by a few hundred KB run to run. Because the slice ships
inside the repository, every rebuild that gets committed adds a fresh ~68 MB
blob to git history permanently, whether or not a single row changed. Rebuild
it when the warehouse data actually changes - not as part of a routine build -
and if it starts changing often, move it to Git LFS before the history does.

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
    "fct_clickstream": "feeds the Sprint 3 imputation at build time; the app reads the result, not the evidence",
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
