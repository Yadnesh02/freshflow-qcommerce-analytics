"""Print every published figure the warehouse can answer for (task S5.0).

A report, not a gate. `anchors.py` pins seven numbers and fails when they move;
this one asks the warehouse for the whole set of figures the README, the plan
and the model docstrings quote, and prints what they are *now*. Nothing here can
fail, because there is nothing to disagree with - the point is to make a
documentation pass a checklist rather than an archaeology exercise.

**Why it exists.** `04737af` gave each simulator component its own random
substream, which changed every draw and therefore every quoted figure. The
anchors were re-derived from a workflow run, but the longer tail - how many
batches are at risk, how many store-SKU-days carry stacked promotions, what
share of intended demand went unfulfilled - lives in prose across a dozen files.
Finding them by grepping for digits and re-running analyses one at a time is how
documentation quietly goes stale: it is expensive enough that it gets deferred,
and once deferred nobody can tell which numbers were checked.

**What it deliberately does not cover.** Figures that are the output of a fitted
model rather than a query - the 3.57x deal uptake multiplier, the Rs 28.6
delivery-cost crossover, the Rs 426,683 the deal line loses in a year. Those
come from an estimator, not a `select`, and re-deriving them means re-running
the module that owns them. Each of those modules prints its own summary, and
`warehouse.yml` runs all of them, so the run log is the right source. Listing
them here with a stub query would suggest they had been checked.

    python tasks.py published
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)


@dataclass(frozen=True)
class Figure:
    """One quoted number, its query, and where it is written down."""

    name: str
    sql: str
    quoted_in: str
    unit: str = "count"


FIGURES: tuple[Figure, ...] = (
    Figure(
        "net_revenue",
        "select sum(net_revenue) from marts.agg_store_sku_day",
        "README headline, plan G2, metrics.yml",
        "money",
    ),
    Figure(
        "writeoff_value",
        "select sum(writeoff_value) from marts.agg_store_sku_day",
        "README, plan, the baseline outcome the project exists to beat",
        "money",
    ),
    Figure(
        "gross_margin_pct",
        "select 100.0 * (sum(net_revenue) - sum(cogs)) / nullif(sum(net_revenue), 0) "
        "from marts.agg_store_sku_day",
        "README, plan baseline (24.4%)",
        "percent",
    ),
    Figure(
        "at_risk_batches",
        "select count(*) from marts.mart_expiry_risk where risk_state = 'at_risk'",
        "expiry_risk.py, the Expiry Control Tower page - and should equal rec_markdown's rows",
    ),
    Figure(
        "expired_batches",
        "select count(*) from marts.mart_expiry_risk where risk_state = 'expired'",
        "expiry_risk.py docstring (17,293 batches, Rs 1.3M) - a booked loss, not actionable",
    ),
    Figure(
        "expired_value",
        "select sum(value_at_risk_inr) from marts.mart_expiry_risk where risk_state = 'expired'",
        "expiry_risk.py docstring (Rs 1.3M)",
        "money",
    ),
    Figure(
        "value_at_risk",
        "select sum(value_at_risk_inr) from marts.mart_expiry_risk where risk_state = 'at_risk'",
        "expiry_risk.py docstring, the Expiry Control Tower tile",
        "money",
    ),
    # NOT the 33 store-SKU-days S2.4 and S4.3 quote, and the distinction cost a
    # correction once already. `is_promo_stacked` is a `bool_or` over a price
    # interval, so this counts *intervals flagged as containing* stacking.
    # Expanding those intervals day by day gives ~3x the true figure - the
    # earlier reading of 120 against a true 33 came from exactly that. The real
    # number needs a day-level intersection of the two promotions and is not a
    # one-line query, so it is reported here for what it is rather than dressed
    # up as the figure the documents quote.
    Figure(
        "stacked_promo_intervals",
        "select count(*) from marts.fct_price_history where is_promo_stacked",
        "fct_price_history.is_promo_stacked - NOT the 33 store-SKU-days S2.4 and S4.3 quote",
    ),
    Figure(
        "unfulfilled_units",
        "select sum(unfulfilled_units) from marts.fct_order",
        "S2.4's censored-demand signal (1,111,254 units)",
    ),
    Figure(
        "unfulfilled_share_pct",
        "select 100.0 * sum(unfulfilled_units) / nullif(sum(requested_units), 0) "
        "from marts.fct_order",
        "S2.4 (20.6% of intended demand)",
        "percent",
    ),
    Figure(
        "short_filled_orders_pct",
        "select 100.0 * count(*) filter (where is_short_filled) / nullif(count(*), 0) "
        "from marts.fct_order",
        "S2.4 (50.4% of orders short-filled)",
        "percent",
    ),
    Figure(
        "elasticity_cells_identified",
        "select count(*) from marts.mart_price_elasticity where is_identified",
        "S4.1/S4.2 (14 of 23 identified, 9 not)",
    ),
    Figure(
        "strongest_elasticity",
        "select max(elasticity) from marts.mart_price_elasticity where is_identified",
        "Price Elasticity page (-0.97, none reaches -1)",
        "coefficient",
    ),
    Figure("rec_markdown_rows", "select count(*) from marts.rec_markdown", "S4.2, the anchors"),
    Figure("rec_deal_slot_rows", "select count(*) from marts.rec_deal_slot", "S4.3 (42 slots)"),
    Figure(
        "rec_transfer_order_rows",
        "select count(*) from marts.rec_transfer_order",
        "S4.4 (3 transfers estate-wide)",
    ),
    Figure(
        "rec_purchase_order_rows",
        "select count(*) from marts.rec_purchase_order",
        "S4.5 (11,824 lines, cap binds on 177) - also the laptop/runner 4,187 vs 4,178 gap",
    ),
    # net_units > 0, not line_count > 0: 76,121 headers (4.9%) are total
    # stockouts at pick time - a basket was built and nothing was fulfilled.
    # The registry says "per delivered order", and including them moves AOV by
    # 5.1% (Rs 278.29 against Rs 292.56).
    Figure(
        "orders_delivered",
        "select count(*) from marts.fct_order where net_units > 0",
        "metrics.yml orders_count, README AOV denominator",
    ),
    Figure(
        "orders_total_stockout",
        "select count(*) from marts.fct_order where net_units = 0",
        "S4.2 note (76,121 headers, 4.9%) - the AOV denominator choice",
    ),
    Figure(
        "aov",
        "select sum(net_revenue) / nullif(count(distinct order_id), 0) from marts.fct_order_item",
        "README and the Executive page (Rs 292.56)",
        "money",
    ),
)


def _format(value, unit: str) -> str:
    if value is None:
        return "-"
    if unit == "money":
        return f"{value:,.2f}"
    if unit == "percent":
        return f"{value:.2f}%"
    if unit == "coefficient":
        return f"{value:.4f}"
    return f"{int(value):,}"


def report(con: duckdb.DuckDBPyConnection) -> int:
    width = max(len(f.name) for f in FIGURES)
    print(f"\n  published figures, as this warehouse answers them\n  {WAREHOUSE}\n")
    missing = 0
    for figure in FIGURES:
        try:
            value = con.execute(figure.sql).fetchone()[0]
            shown = _format(value, figure.unit)
        except duckdb.Error as exc:
            shown = f"unavailable ({str(exc).splitlines()[0][:60]})"
            missing += 1
        print(f"  {figure.name:<{width}}  {shown:>18}   {figure.quoted_in}")

    print(
        "\n  Not listed, because a query cannot answer them - re-run the module that owns each\n"
        "  and read its printed summary: the deal uptake multiplier and reactivation uplift\n"
        "  (tasks.py deal-slots), the delivery-cost crossover (mart_customer_360's header),\n"
        "  the markdown disposal-cost breakeven (tasks.py markdown --disposal-cost)."
    )
    if missing:
        print(f"\n  {missing} figure(s) unavailable - run the optimisers first")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--warehouse", type=Path, default=WAREHOUSE)
    args = parser.parse_args(argv)
    if not args.warehouse.exists():
        print(f"\n  no warehouse at {args.warehouse} - run `python tasks.py build` first")
        return 1
    con = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        con.execute("set enable_progress_bar=false")
        return report(con)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
