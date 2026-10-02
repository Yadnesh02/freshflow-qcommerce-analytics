"""The deal rail's own measurement (task D2).

Three modules, one question each: what the rail costs, what it earns back, and
whether the units it moved were ever going to be sold anyway.

**The test this file exists for is the price invariant.** `deal_slots.py`
carried a standalone P&L in its docstring for a sprint and a half - minus
Rs 426,683 on 12,153 units and Rs 162,532 of revenue - and the figure was
impossible: Rs 13.37 a unit on an Rs 11 deal. Nothing caught it because prose
cannot be executed. `test_the_price_invariant_catches_the_error_that_shipped`
is that figure, encoded, so the same mistake fails a build instead of surviving
one.

    python -m pytest tests/test_deal_analysis.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from analytics.deal import cannibalisation as cann
from analytics.deal.pnl import DEAL_PRICE, DealPnL, standalone_pnl

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
    yield connection
    connection.close()


# ================================================== the P&L
def test_the_price_invariant_catches_the_error_that_shipped() -> None:
    """The exact figure that was wrong, rejected.

    Not a synthetic example: these are the numbers `deal_slots.py` carried. The
    assertion is that a build asserting them would now fail rather than quote
    them for another sprint.
    """
    shipped = DealPnL(
        lines=11_388,
        units=12_153,
        revenue=162_532.0,
        cogs=589_214.0,
        first_day="2025-09-01",
        last_day="2026-08-29",
    )
    assert abs(shipped.price_per_unit - 13.37) < 0.01, "this is the figure that was published"
    with pytest.raises(ValueError, match="a unit against a"):
        shipped.check()


def test_a_correct_pnl_passes_its_own_check() -> None:
    clean = DealPnL(
        lines=10,
        units=100,
        revenue=100 * DEAL_PRICE,
        cogs=5_000.0,
        first_day="2025-09-01",
        last_day="2025-09-08",
    )
    clean.check()
    assert clean.margin == pytest.approx(100 * DEAL_PRICE - 5_000.0)


def test_the_rail_realises_the_deal_price_on_the_real_warehouse(con) -> None:
    """If this fails, the promo tag is on lines the rail did not price."""
    pnl = standalone_pnl(con)
    assert pnl.price_per_unit == pytest.approx(DEAL_PRICE, abs=0.05)
    assert pnl.units > 0


def test_the_rail_loses_money_on_its_own_line(con) -> None:
    """The premise of the whole project, checked rather than assumed.

    If the rail ever turns a standalone profit the question changes completely -
    there would be nothing to earn back and no reason for the attach and
    retention chapters to exist.
    """
    pnl = standalone_pnl(con)
    assert pnl.margin < 0, (
        f"the rail made Rs {pnl.margin:,.0f} on its own line. That is not a loss leader, "
        f"and D2's question no longer applies."
    )


def test_returns_are_netted_rather_than_double_counted(con) -> None:
    """`units` counts a returned unit twice; `signed_qty` nets it.

    Using the wrong one inflates the denominator and drags the realised price
    below the deal price - which is a quiet error, because it moves the number
    in a plausible direction.
    """
    gross, net = con.execute(
        """
        select sum(units), sum(signed_qty)
        from marts.fct_order_item where promo_id = 'PROMO-DEAL11'
        """
    ).fetchone()
    assert net <= gross, "signed quantity should never exceed unsigned"
    assert standalone_pnl(con).units == net


# ================================================== cannibalisation
def test_the_event_study_finds_blocks_not_isolated_days(con) -> None:
    """The rail runs in blocks, and the first version of this looked for days.

    695 of 710 store-SKU slots run seven consecutive days, so every day of a
    block sits inside another day's window. A day-based design finds nothing at
    all - silently, by returning an empty frame rather than erroring.
    """
    panel = cann.event_panel(con)
    assert not panel.empty
    blocks = panel[["store_id", "sku_id", "block_start"]].drop_duplicates()
    assert len(blocks) > 100, f"only {len(blocks)} blocks - the window filter is too strict"


def test_the_baseline_never_includes_a_day_inside_a_slot_window(con) -> None:
    """Otherwise the comparison is the slot against itself."""
    contaminated = con.execute(
        f"""
        with blocks as (
            select store_id, sku_id, effective_from_date as s,
                   coalesce(effective_to_date, effective_from_date) as e
            from marts.fct_price_history where promo_id = 'PROMO-DEAL11'
        ),
        base as ({cann.BASELINE})
        select count(*) from base b
        join blocks k on k.store_id = b.store_id and k.sku_id = b.sku_id
        where b.clean_days = 0
        """
    ).fetchone()[0]
    assert contaminated == 0


def test_censored_days_are_excluded_from_the_panel(con) -> None:
    """A stockout is a supply fact. Counting it as a dip reads the wrong cause."""
    panel = cann.event_panel(con)
    assert not panel["is_censored"].any()


def test_the_slot_lifts_sales_while_it_runs(con) -> None:
    """A rail that does not move units during its own block is not a rail.

    This is the sanity check under everything else: if the in-block lift were
    zero, the cannibalisation figure would be a ratio with nothing in the
    denominator and the uptake multiplier quoted upstream would be fiction.
    """
    summary = cann.summarise(cann.event_panel(con))
    assert summary["in_block_lift"] > 0, (
        f"the slot moved {summary['in_block_lift']:+.2f} units per block while running"
    )


# ================================================== attach
def test_attach_refuses_a_build_with_no_control_group(con) -> None:
    """A pre-D1 warehouse has no holdout, and must say so rather than guess.

    The failure mode this prevents is the worst available one: silently
    computing a difference between an arm and itself, returning zero, and
    letting somebody conclude the rail does nothing.
    """
    from analytics.deal.attach import itt_effects, per_customer

    try:
        frame = per_customer(con)
    except duckdb.CatalogException:
        pytest.skip("this build predates stg_crm__deal_exposure")

    arms = set(frame["deal_arm"].unique())
    if arms == {"exposed", "holdout"}:
        effects = itt_effects(frame)
        assert {e.metric for e in effects} >= {"orders", "revenue", "nondeal_margin"}
        for e in effects:
            assert e.ci_low <= e.diff <= e.ci_high, f"{e.metric}: estimate outside its own interval"
    else:
        with pytest.raises(ValueError, match="one arm is empty"):
            itt_effects(frame)


# ================================================== retention
def test_the_holdout_never_redeems(con) -> None:
    """One-sided non-compliance, which is a fact about the price surface.

    A held-out customer could not have taken the deal if they had wanted to -
    they were never shown the price. This is what makes the compliance rate
    interpretable; it is NOT what makes an instrumental variable valid, which
    is a different condition and one that fails here.
    """
    from analytics.deal.retention import outcomes

    try:
        frame = outcomes(con)
    except (duckdb.CatalogException, ValueError):
        pytest.skip("this build predates the D1 holdout")

    held = frame[frame["deal_arm"] == "holdout"]
    assert held["redeemed"].sum() == 0, (
        f"{int(held['redeemed'].sum())} held-out customers redeemed. The control arm is "
        f"contaminated and every retention estimate from this build is a blend."
    )


def test_exposure_reaches_churn_without_redemption(con) -> None:
    """Why no CACE is reported, checked rather than asserted in a comment.

    If exposed customers who never redeemed looked exactly like the holdout, the
    exclusion restriction would be defensible and a Wald ratio would estimate
    the effect on redeemers. They do not: the rail concentrates demand onto a
    few SKUs which then run dry, and that reaches everyone near it.
    """
    from analytics.deal.retention import exclusion_fails, outcomes

    try:
        frame = outcomes(con)
    except (duckdb.CatalogException, ValueError):
        pytest.skip("this build predates the D1 holdout")

    never, held = exclusion_fails(frame)
    assert never > held, (
        f"exposed non-redeemers carry {never:.4f} stockout days against the holdout's "
        f"{held:.4f}. If this ever reverses, revisit whether a CACE is available."
    )


def test_every_retention_estimate_carries_a_detectable_floor(con) -> None:
    """A null with no MDE beside it is a shrug, not a result."""
    from analytics.deal.retention import _compare, outcomes

    try:
        frame = outcomes(con)
    except (duckdb.CatalogException, ValueError):
        pytest.skip("this build predates the D1 holdout")

    for column in ("churned", "active_recent"):
        estimate = _compare(frame, column)
        assert estimate.mde > 0
        assert estimate.ci_low <= estimate.diff <= estimate.ci_high
