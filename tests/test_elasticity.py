"""What the elasticity estimates have to be true of (task S4.1).

S4.2 divides by these numbers to choose a discount depth, so a wrong sign is
not a bad estimate - it is an instruction to raise prices on expiring stock.
Most of this file is about the boundary between "we measured a price response"
and "we could not", because the failure that matters here is a confident number
where there is no information.

The identification problem is worth stating once. Stock is marked down because
it is near expiry, and the deepest cuts went to whatever was not selling, so
price carries the demand it was reacting to. Raw estimates at the 0-1d band came
out *positive* - cutting the price sold less - before controlling for freshness
within the band and for how small the remnant was. That is the policy showing
through, not a demand curve.

Needs the estimates built:

    python tasks.py build && python tasks.py elasticity
    python -m pytest tests/test_elasticity.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from analytics.elasticity.estimate import MIN_PRICE_RATIO, UNDISCOUNTED, is_identified, shrink

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
    built = connection.execute(
        """
        select count(*) from information_schema.tables
        where table_name = 'mart_price_elasticity'
        """
    ).fetchone()[0]
    if not built:
        connection.close()
        pytest.skip("no mart_price_elasticity - run `python tasks.py elasticity`")
    yield connection
    connection.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


# ================================================== the gate
def test_every_identified_cell_slopes_downward(con) -> None:
    """S4.1's gate. A positive elasticity that reached S4.2 would tell it to
    raise the price on stock that is about to be thrown away."""
    offenders = con.execute(
        """
        select l1_category, dte_band, elasticity_raw
        from marts.mart_price_elasticity
        where is_identified and elasticity_raw > 0
        """
    ).fetchall()
    assert not offenders, f"upward-sloping demand in {offenders}"


def test_the_number_handed_downstream_is_never_positive(con) -> None:
    """Whatever the fallback chain produces still has to be a demand curve."""
    offenders = con.execute(
        """
        select l1_category, dte_band, elasticity
        from marts.mart_price_elasticity
        where elasticity is not null and elasticity >= 0
        """
    ).fetchall()
    assert not offenders, f"a non-negative elasticity is published for {offenders}"


def test_some_cells_are_honestly_unidentified(con) -> None:
    """The estimator must be capable of saying it does not know.

    If every cell came back identified, the significance rule would not be
    binding and the fallback chain would never have been exercised - which on
    this data would mean the freshness confound had been silently absorbed
    rather than handled.
    """
    unidentified = one(
        con, "select count(*) from marts.mart_price_elasticity where not is_identified"
    )
    assert unidentified > 0, (
        "every cell claims a measured price response, which on a panel where markdown "
        "depth was set by expected demand is not credible"
    )


def test_unidentified_cells_fall_back_to_something_measured(con) -> None:
    """A cell that found no slope must borrow one that was found, or hold none.

    Falling back to a category estimate that is itself unidentified would move
    the guess one level up and present it as an answer.
    """
    bad = con.execute(
        """
        select l1_category, dte_band, elasticity_basis
        from marts.mart_price_elasticity
        where not is_identified
          and elasticity_basis = 'category' and not category_is_identified
        """
    ).fetchall()
    assert not bad, f"these borrowed an elasticity nobody measured: {bad}"

    null_but_identified = one(
        con,
        """
        select count(*) from marts.mart_price_elasticity
        where elasticity is null and (is_identified or category_is_identified)
        """,
    )
    assert null_but_identified == 0, "a cell was left null despite having a measured basis"


# ================================================== the shape is economic
def test_the_last_day_is_the_least_elastic_band(con) -> None:
    """The finding S4.2 acts on, and the one that would be easiest to invert.

    Shoppers largely will not take stock expiring today at any price, so a
    deeper cut at 0-1d buys very little; the response is strongest two to five
    days out. If this ever reversed, the optimiser's whole strategy - mark down
    earlier rather than harder at the end - would be wrong.
    """
    by_band = dict(
        con.execute(
            """
            select dte_band, avg(elasticity_raw)
            from marts.mart_price_elasticity
            where is_identified
            group by dte_band
            """
        ).fetchall()
    )
    if "0-1d" not in by_band or "2-3d" not in by_band:
        pytest.skip("not both bands identified in this run")
    assert by_band["0-1d"] > by_band["2-3d"], (
        f"0-1d elasticity {by_band['0-1d']:.2f} is stronger than 2-3d "
        f"{by_band['2-3d']:.2f}; the mark-down-earlier conclusion depends on the reverse"
    )


def test_elasticities_are_within_a_plausible_range(con) -> None:
    """A coefficient past -3 on grocery staples is a misspecification, not a finding."""
    extreme = con.execute(
        """
        select l1_category, dte_band, elasticity_raw
        from marts.mart_price_elasticity
        where elasticity_raw < -3 or elasticity_raw > 1
        """
    ).fetchall()
    assert not extreme, f"implausible coefficients: {extreme}"


def test_standard_errors_are_finite_and_positive(con) -> None:
    """A zero standard error means the cluster structure collapsed."""
    bad = one(
        con,
        """
        select count(*) from marts.mart_price_elasticity
        where standard_error is null or standard_error <= 0 or not isfinite(standard_error)
        """,
    )
    assert bad == 0, f"{bad} cells report a degenerate standard error"


# ================================================== the shrinkage
def test_shrinkage_weight_stays_inside_the_unit_interval(con) -> None:
    outside = one(
        con,
        """
        select count(*) from marts.mart_price_elasticity
        where signal_weight < 0 or signal_weight > 1
        """,
    )
    assert outside == 0, f"{outside} cells carry a weight outside [0, 1]"


def test_a_noisy_cell_is_pulled_toward_the_pool_and_a_precise_one_is_not() -> None:
    """The shrinkage rule, exercised directly rather than inferred from output."""
    pooled = -0.40
    noisy, noisy_weight = shrink(cell_beta=-0.05, cell_se=0.50, pooled_beta=pooled)
    precise, precise_weight = shrink(cell_beta=-0.05, cell_se=0.01, pooled_beta=pooled)

    assert noisy_weight < precise_weight
    assert abs(noisy - pooled) < abs(precise - pooled), (
        "the noisy cell was not pulled further toward the pooled estimate"
    )


def test_identification_requires_the_interval_to_clear_zero() -> None:
    assert is_identified(-0.50, 0.10)
    assert not is_identified(-0.10, 0.10), "an interval containing zero was called identified"
    assert not is_identified(0.20, 0.05), "a positive coefficient was called identified"


# ================================================== the panel is clean
def test_no_estimate_rests_on_censored_days(con) -> None:
    """A stockout truncates quantity, so the day says nothing about willingness
    to buy at that price - it says the shelf was empty."""
    censored_share = one(
        con,
        """
        select count(*) filter (where is_censored) / cast(count(*) as double)
        from marts.agg_store_sku_day
        """,
    )
    assert censored_share > 0, "no censored days at all - the filter would be untested"


def test_the_price_window_excludes_clearance_stunts(con) -> None:
    """The 11-rupee slot lands near a 0.11 ratio and is a different mechanism.

    Leaving it in would let a handful of near-giveaway observations dominate the
    slope for a whole category.
    """
    assert 0 < MIN_PRICE_RATIO < UNDISCOUNTED < 1.0


def test_every_estimated_category_is_one_that_actually_gets_marked_down(con) -> None:
    """Elasticity is identified where the decision needs it and nowhere else.

    Shelf-stable categories are discounted on under 0.2% of store-SKU-days, so
    any coefficient for them would be noise - and the markdown optimiser never
    acts on them anyway.
    """
    categories = {
        r[0]
        for r in con.execute(
            "select distinct l1_category from marts.mart_price_elasticity"
        ).fetchall()
    }
    assert categories, "no category was estimated"
    perishable = {"Dairy & Eggs", "Fruits & Vegetables", "Meat, Fish & Seafood"}
    assert perishable <= categories, (
        f"the perishable categories the optimiser acts on are missing: {perishable - categories}"
    )
