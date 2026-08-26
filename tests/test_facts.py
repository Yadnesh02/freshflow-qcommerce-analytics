"""Contract for the fact layer (task S2.4).

The dbt tests assert keys, relationships and the tie to raw. This file asserts
the things that make the fact layer coherent rather than merely correct: that
two models built from the same events agree with each other, that the
conventions the whole warehouse depends on actually hold, and that the
modelling decisions the plan calls keystones are earning their keep.

Needs a built warehouse:

    python tasks.py build
    python -m pytest tests/test_facts.py
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
    yield connection
    connection.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


# ------------------------------------------------------- header vs its lines
def test_order_totals_agree_with_the_lines_beneath_them(con) -> None:
    """fct_order rolls its basket up from fct_order_item, so the two cannot
    disagree - but only if the rollup covers every line and drops none.

    Checked in total and at the worst single order. A total that matches while
    individual orders are wrong in offsetting directions is the failure a
    grand-total comparison is blind to.
    """
    header_gmv, line_gmv = con.execute(
        """
        select
            (select sum(gmv) from marts.fct_order),
            (select sum(net_revenue) from marts.fct_order_item)
        """
    ).fetchone()
    assert abs(header_gmv - line_gmv) < 0.01, (
        f"header GMV {header_gmv:,.2f} vs line revenue {line_gmv:,.2f}"
    )

    worst = one(
        con,
        """
        select coalesce(max(abs(orders.gmv - lines.gmv)), 0)
        from marts.fct_order as orders
        join (
            select order_id, sum(net_revenue) as gmv
            from marts.fct_order_item group by order_id
        ) as lines on orders.order_id = lines.order_id
        """,
    )
    assert worst < 0.01, f"worst single order disagrees by {worst:,.4f}"


def test_the_requested_basket_bounds_what_was_actually_served(con) -> None:
    """`requested_units` is what the customer asked for, written before
    allocation ran. Two things have to hold for it to be usable as a demand
    signal rather than a puzzling mismatch.

    It must bound fulfilment - a store cannot serve more than was asked for, so
    a single order exceeding its own request would mean the two numbers are
    measuring different things and the whole reading is wrong. And the shortfall
    must be material, or there is no censoring to correct for and Sprint 3 is
    modelling noise.
    """
    over_served = one(
        con, "select count(*) from marts.fct_order where gross_units > requested_units"
    )
    assert over_served == 0, (
        f"{over_served:,} orders served more than was requested - requested_units "
        "is not what this test assumes it is"
    )

    requested, served, unfulfilled = con.execute(
        "select sum(requested_units), sum(gross_units), sum(unfulfilled_units) from marts.fct_order"
    ).fetchone()
    assert requested - served == unfulfilled
    assert unfulfilled / requested > 0.05, (
        f"only {unfulfilled / requested:.1%} of demand went unserved - too little "
        "censoring for the Sprint 3 correction to be worth building"
    )

    negative = one(con, "select count(*) from marts.fct_order where unfulfilled_units < 0")
    assert negative == 0


# ------------------------------------------------------------ the ledger
def test_the_ledger_replays_to_the_same_balance_it_aggregates_to(con) -> None:
    """qty_remaining is a sum; running_balance is a window. They are computed
    by different mechanisms over the same events and must land in the same
    place.

    This is what catches a window frame bug - `rows between unbounded preceding
    and current row` quietly becoming a range, or the ordering falling back to
    event_date and losing intra-day sequence. Both produce a running balance
    that looks plausible on every row and is wrong on most of them.
    """
    mismatched = one(
        con,
        """
        with final_balance as (
            select batch_id, running_balance
            from marts.fct_inventory_movement
            qualify row_number() over (partition by batch_id order by movement_seq desc) = 1
        )
        select count(*)
        from marts.fct_inventory_batch as batches
        join final_balance on batches.batch_id = final_balance.batch_id
        where batches.qty_remaining <> final_balance.running_balance
        """,
    )
    assert mismatched == 0, f"{mismatched:,} batches replay to a different balance than they sum to"


def test_no_batch_is_over_consumed_once_its_history_is_intact(con) -> None:
    """Stock cannot go negative where the ledger is complete.

    Restricted to reconciled batches on purpose: defect 3 quarantines about 1%
    of movements, and a batch missing its inbound reads as over-consumed. That
    is a property of the documented damage, not of the model, and the
    difference between the two is exactly what is_reconciled records.
    """
    negative = one(
        con,
        """
        select count(*) from marts.fct_inventory_batch
        where is_reconciled and qty_remaining < 0
        """,
    )
    assert negative == 0, f"{negative:,} fully-reconciled batches went negative"


# --------------------------------------------------------- the keystone
def test_lines_really_are_split_across_batches(con) -> None:
    """The reason the grain is (order_id, sku_id, batch_id) and not
    (order_id, sku_id).

    When a line is larger than the batch at the front of the FEFO queue it is
    served from two batches with different expiry dates. If that never
    happened, the batch key on the sale would be decoration and the simpler
    grain would be right. It happens, and each half carries its own
    dte_at_sale - which is the whole basis of expiry attribution.
    """
    split_lines = one(
        con,
        """
        select count(*) from (
            select order_id, sku_id
            from marts.fct_order_item
            where line_type = 'sale'
            group by order_id, sku_id
            having count(distinct batch_id) > 1)
        """,
    )
    assert split_lines > 0, (
        "no order line was ever split across batches - the batch grain would be "
        "unnecessary and the FEFO allocation is not doing what it claims"
    )

    differing_freshness = one(
        con,
        """
        select count(*) from (
            select order_id, sku_id
            from marts.fct_order_item
            where line_type = 'sale'
            group by order_id, sku_id
            having count(distinct batch_id) > 1 and min(dte_at_sale) <> max(dte_at_sale))
        """,
    )
    assert differing_freshness > 0, (
        "split lines all carry identical shelf life - the split is not "
        "attributing different expiry dates, which is the point of it"
    )


# --------------------------------------------------------- price history
def test_the_price_fact_records_only_discounted_intervals(con) -> None:
    """The source is a promotion ledger, not a full price ledger.

    Worth asserting rather than only documenting, because 'price history'
    invites the assumption that a missing day means a missing price. It means
    the SKU was selling at catalogue base price. If an undiscounted interval
    ever appeared here, that assumption would silently become wrong everywhere
    it is relied on.
    """
    undiscounted = one(
        con,
        "select count(*) from marts.fct_price_history where realized_price >= base_price",
    )
    assert undiscounted == 0, f"{undiscounted:,} intervals are not actually discounts"

    assert one(con, "select count(*) from marts.fct_price_history where promo_id is null") == 0


def test_stacked_promotions_are_flagged_rather_than_double_counted(con) -> None:
    """A markdown and the Rs 11 deal slot on the same ageing SKU.

    The interval keeps one primary promotion - the deepest, which is the one
    that set the price - and records that others were live. Sprint 4 must not
    let markdown performance and deal-slot performance each claim these units:
    the same uplift, counted twice, in two marts that each look right alone.
    """
    stacked = one(con, "select count(*) from marts.fct_price_history where is_promo_stacked")
    assert stacked > 0, "no stacked promotions - the flag is untested by the data"

    unflagged = one(
        con,
        """
        select count(*) from marts.fct_price_history
        where max_promos_active > 1 and not is_promo_stacked
        """,
    )
    assert unflagged == 0, f"{unflagged} intervals had promos stack without being flagged"
