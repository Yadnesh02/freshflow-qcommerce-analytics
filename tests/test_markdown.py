"""What the markdown optimiser has to be true of (task S4.2).

Two failures dominate this file, and neither one announces itself.

The first is a gate that passes because nothing happened. G4 asks that depth
rise as days-to-expiry falls, and a sweep where every depth is zero satisfies
"non-decreasing" perfectly while testing nothing at all. The first draft of
`check_monotonicity` did exactly that - it used a fixture whose stock was
already smaller than its horizon demand, so no batch was ever at risk and no
discount could ever help. It reported PASS. Several tests here exist only to
hold that shut: the gate has to move before it is allowed to pass.

The second is optimising on a coefficient nobody measured. S4.1 reports 23
category x DTE cells, five of which found no downward slope, and falls those
back to their category's number so the table has no holes in it. Every one of
the five is a last-day band, and the category means are around three times the
response the one identified last-day cell actually found. Taking the fallback
would cut deepest precisely where shoppers do not respond. The guard is on
`is_identified` and *not* on `elasticity is null`, because the null path never
fires in the current estimates - `test_the_null_guard_would_have_caught_nothing`
is that fact written down so the guard is not "simplified" later.

Needs the recommendations built:

    python tasks.py build && python tasks.py elasticity
    python tasks.py expiry-risk && python tasks.py markdown
    python -m pytest tests/test_markdown.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import numpy as np
import pytest

from analytics.optimization.markdown import (
    DEPTH_GRID,
    NO_SLOPE,
    NOT_ESTIMATED,
    check_monotonicity,
    concave_frontier,
    evaluate,
)

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
        select count(*) from information_schema.tables where table_name = 'rec_markdown'
        """
    ).fetchone()[0]
    if not built:
        connection.close()
        pytest.skip("no marts.rec_markdown - run `python tasks.py markdown`")
    yield connection
    connection.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


# ================================================== gate G4
def test_depth_rises_as_expiry_approaches() -> None:
    """G4, stated as the plan states it: deeper as DTE falls, demand held constant."""
    sweep = check_monotonicity().sort_values("days_to_expiry", ascending=False)
    depths = sweep["depth"].to_numpy()
    pairs = list(zip(sweep["days_to_expiry"], depths, strict=True))
    assert np.all(np.diff(depths) >= -1e-9), f"depth is not monotone as DTE falls: {pairs}"


def test_the_g4_sweep_actually_moves() -> None:
    """The gate above is worthless if every depth is zero, and once was.

    A flat sweep satisfies "non-decreasing" trivially, so the gate has to be
    shown moving before it is allowed to pass.
    """
    sweep = check_monotonicity()
    assert sweep["depth"].max() > 0, "no depth was ever chosen - the gate is vacuous"
    assert sweep["depth"].nunique() > 1, (
        "every band chose the same depth - the sweep is not exercising DTE"
    )


def test_an_inelastic_coefficient_makes_the_g4_sweep_vacuous() -> None:
    """The fixture bug that shipped a green gate, kept as a test not a comment.

    The first draft ran the sweep at beta = -0.45, a plausible-looking figure
    near the middle of what S4.1 fitted. Every depth came back zero, "monotone
    non-decreasing" held, and the gate reported PASS while testing nothing. The
    cause was the coefficient alone: below |beta| = 1 no discount is ever chosen
    at any DTE, so `check_monotonicity` must be run at an elastic coefficient or
    it is measuring nothing.

    Stock below horizon demand was the second thing wrong with that fixture, and
    it is milder - it flattens the long-DTE end while the short end still moves.
    `test_the_g4_sweep_covers_every_band` holds that half.
    """
    flat = check_monotonicity(beta=-0.45)
    assert flat["depth"].max() == 0, (
        "an inelastic coefficient now chooses a discount - the objective changed, "
        "and G4's elastic fixture needs rethinking"
    )


def test_the_g4_sweep_covers_every_band() -> None:
    """Stock has to exceed horizon demand or the long-DTE end has nothing to say.

    A batch that clears itself is not at risk, so the optimum there is depth 0
    whatever the coefficient. At 40 units against 84 of demand the first four
    bands are all zero and only the last three carry information.
    """
    truncated = check_monotonicity(qty_remaining=40.0)
    assert (truncated["depth"] == 0).sum() > 0, "expected the long-DTE end to be flat here"
    assert (check_monotonicity()["depth"] > 0).all(), (
        "some band chose no discount - the gate's stock is no longer above horizon demand"
    )


def test_an_inelastic_coefficient_chooses_no_discount() -> None:
    """The economic core, and the reason the recommendation book is nearly empty.

    While stock is short of demand the margin is `sold * price - qty * cost`,
    the cost term does not move with price, and the decision is revenue -
    proportional to `r ** (1 + beta)`. Every coefficient S4.1 fitted is inside
    the unit interval, so that exponent is positive and the optimum is the
    highest price available. A markdown optimiser that recommended cuts here
    would be arguing with arithmetic.
    """
    for beta in (-0.10, -0.45, -0.88, -0.99):
        curve = evaluate(
            base_price=100.0,
            cost=60.0,
            posted_price=100.0,
            beta=beta,
            qty_remaining=120.0,
            units_ahead=0.0,
            horizon_demand=36.0,
        )
        best = curve.loc[curve["margin"].idxmax()]
        assert best["depth"] == 0.0, f"beta={beta} is inelastic but chose {best['depth']:.0%}"


def test_an_elastic_coefficient_prices_at_the_clearing_point() -> None:
    """Past |beta| = 1 the lever switches on, and stops exactly where stock clears.

    Below the clearing price the extra demand has nothing left to buy, so the
    cut is being paid on units that were selling anyway. The optimum sits on
    that kink, which is the classic markdown answer and the thing worth checking
    the implementation actually reproduces.
    """
    qty, horizon, beta = 120.0, 84.0, -1.6
    curve = evaluate(
        base_price=100.0,
        cost=60.0,
        posted_price=100.0,
        beta=beta,
        qty_remaining=qty,
        units_ahead=0.0,
        horizon_demand=horizon,
    )
    best = curve.loc[curve["margin"].idxmax()]
    assert best["depth"] > 0, "an elastic coefficient chose no discount"

    clearing = (qty / horizon) ** (1.0 / beta)
    assert abs(best["price_ratio"] - clearing) <= 0.05 + 1e-9, (
        f"chose {best['price_ratio']:.3f} against a clearing price of {clearing:.3f}"
    )


# ================================================== the guard
def test_no_recommendation_rests_on_an_unidentified_cell(con) -> None:
    """The guard S4.2 exists to enforce: never price on a coefficient nobody found."""
    leaked = one(
        con,
        """
        select count(*)
        from marts.rec_markdown as rec
        inner join marts.mart_price_elasticity as est
            on est.l1_category = rec.l1_category and est.dte_band = rec.dte_band
        where rec.decision <> 'no_recommendation' and not est.is_identified
        """,  # noqa: S608 - table names are module constants, not input
    )
    assert leaked == 0, f"{leaked} recommendations priced on a cell with no measured slope"


def test_the_unidentified_cells_are_declined_by_name(con) -> None:
    """And declined for the right reason, not swept into a generic bucket."""
    rows = con.execute(
        """
        select distinct rec.l1_category, rec.dte_band
        from marts.rec_markdown as rec
        inner join marts.mart_price_elasticity as est
            on est.l1_category = rec.l1_category and est.dte_band = rec.dte_band
        where not est.is_identified and rec.decline_reason is distinct from ?
        """,
        [NO_SLOPE],
    ).fetchall()
    assert not rows, f"unidentified cells declined for the wrong reason: {rows}"


def test_the_null_guard_would_have_caught_nothing(con) -> None:
    """Why the guard is on is_identified rather than on a null check.

    S4.1's fallback chain leaves `elasticity` null only when the *category* is
    also unidentified, which no category currently is. So a guard written as
    `where elasticity is null` matches zero rows and would have priced all five
    unidentified cells at their category's coefficient without a word. If this
    ever starts failing, the null path has begun firing and the guard can be
    widened - but it must not be narrowed to null on the strength of the
    docstring in S4.1 alone.
    """
    nulls = one(con, "select count(*) from marts.mart_price_elasticity where elasticity is null")
    unidentified = one(
        con, "select count(*) from marts.mart_price_elasticity where not is_identified"
    )
    assert nulls == 0, "the null path has started firing - revisit the guard in markdown.py"
    assert unidentified > 0, "no unidentified cells at all - the guard is untested by the data"


def test_declines_distinguish_never_fitted_from_fitted_flat(con) -> None:
    """Two different problems with two different fixes, so two different reasons.

    A category S4.1 never modelled needs a randomised markdown before anything
    can be said about it. A cell it modelled and could not identify needs S5.1's
    policy backtest. Reporting both as "no elasticity" hides the 51 batches that
    are actually a measurement problem inside the 438 that are not.
    """
    reasons = {
        r[0]
        for r in con.execute(
            "select distinct decline_reason from marts.rec_markdown "
            "where decision = 'no_recommendation'"
        ).fetchall()
    }
    assert NOT_ESTIMATED in reasons and NO_SLOPE in reasons, (
        f"the two decline reasons have collapsed into one: {reasons}"
    )


# ================================================== constraints
def test_nothing_is_priced_below_landed_cost(con) -> None:
    """The cost floor, which the plan specifies and a category manager will check."""
    breaches = one(
        con,
        """
        select count(*)
        from marts.rec_markdown as rec
        inner join marts.mart_expiry_risk as risk on risk.batch_id = rec.batch_id
        where rec.decision = 'markdown'
          and rec.recommended_price < risk.unit_landed_cost - 0.005
        """,
    )
    assert breaches == 0, f"{breaches} recommendations price below landed cost"


def test_a_markdown_is_below_the_price_already_posted(con) -> None:
    """Otherwise "markdown" includes stock that was told to stay where it is.

    Depth is measured off base price, so a SKU already ticketed at 30% off and
    left alone carries depth 0.30. Labelling that a markdown put 22 phantom
    actions on the queue with zero spend behind them before this was caught.
    """
    phantom = one(
        con,
        """
        select count(*) from marts.rec_markdown
        where decision = 'markdown' and recommended_price >= posted_price - 0.005
        """,
    )
    assert phantom == 0, f"{phantom} 'markdowns' do not lower the posted price"


def test_markdown_spend_respects_the_daily_budget(con) -> None:
    """The knapsack constraint, checked per store rather than in total."""
    over = con.execute(
        """
        select store_id, sum(expected_markdown_spend) as spend
        from marts.rec_markdown
        group by store_id
        having sum(expected_markdown_spend) > 1000.0 + 0.01
        """
    ).fetchall()
    assert not over, f"stores over the 1,000 rupee daily budget: {over}"


def test_every_at_risk_batch_gets_exactly_one_row(con) -> None:
    """No batch silently dropped, and none duplicated by the band join.

    `dim_dte_band` shares its endpoints - 0-1d is [0,1] and 1-2d is [1,2] - so a
    plain `between` join returns two bands for a batch one, two, three, five or
    seven days out, and therefore two elasticities and two recommendations for
    one decision. The optimiser takes the lowest-sorted match; this is what says
    so in numbers.
    """
    at_risk = one(
        con,
        "select count(*) from marts.mart_expiry_risk "
        "where risk_state = 'at_risk' and qty_remaining > 0",
    )
    recommended = one(con, "select count(*) from marts.rec_markdown")
    distinct = one(con, "select count(distinct batch_id) from marts.rec_markdown")

    assert recommended == at_risk, f"{at_risk} at-risk batches produced {recommended} rows"
    assert distinct == recommended, "a batch was recommended twice - the band join duplicated"


def test_the_band_intervals_are_disjoint_as_applied(con) -> None:
    """The same overlap, checked at the source rather than through its effect."""
    doubled = one(
        con,
        """
        select count(*) from (
            select risk.batch_id
            from marts.mart_expiry_risk as risk
            inner join marts.dim_dte_band as bands
                on risk.days_to_expiry between bands.min_days and bands.max_days
            where risk.risk_state = 'at_risk'
            group by risk.batch_id
            having count(*) > 1
        )
        """,
    )
    assert doubled > 0, (
        "dim_dte_band no longer overlaps - if the seed was fixed, the first-match "
        "subquery in DECISION_SQL is now redundant and should be simplified away"
    )


# ================================================== the objective
def test_the_frontier_has_decreasing_marginal_returns() -> None:
    """What makes the greedy allocation exact rather than merely reasonable."""
    curve = evaluate(
        base_price=100.0,
        cost=40.0,
        posted_price=100.0,
        beta=-1.8,
        qty_remaining=200.0,
        units_ahead=0.0,
        horizon_demand=60.0,
        ratios=DEPTH_GRID,
    )
    frontier = concave_frontier(curve)
    if len(frontier) < 3:
        pytest.skip("frontier too short to have an interior slope")

    slopes = np.diff(frontier["margin"].to_numpy()) / np.diff(frontier["spend"].to_numpy())
    assert np.all(np.diff(slopes) <= 1e-9), f"marginal return is not decreasing: {slopes}"


def test_the_cost_floor_removes_candidates_rather_than_penalising_them() -> None:
    """A floor implemented as a large negative margin still gets picked when
    everything else is worse. Implemented as a filter, it cannot be."""
    curve = evaluate(
        base_price=100.0,
        cost=85.0,
        posted_price=100.0,
        beta=-2.0,
        qty_remaining=100.0,
        units_ahead=0.0,
        horizon_demand=10.0,
    )
    assert not curve.empty
    assert (curve["price"] >= 85.0 - 1e-9).all(), "a below-cost price survived the filter"


def test_a_disposal_cost_can_only_make_discounting_more_attractive() -> None:
    """The S5.3 lever, and a monotonicity that has to hold for it to be swept.

    Charging something for a written-off unit raises the value of rescuing it,
    so the chosen depth may deepen but can never shrink. The default is zero
    because this dataset carries no disposal charge, and inventing one would be
    the same move as the 42-rupee delivery cost in mart_customer_360 - which is
    why it is a parameter with a stated default rather than a constant.
    """
    previous = -1.0
    for disposal in (0.0, 10.0, 25.0, 50.0, 100.0):
        curve = evaluate(
            base_price=100.0,
            cost=60.0,
            posted_price=100.0,
            beta=-0.60,
            qty_remaining=120.0,
            units_ahead=0.0,
            horizon_demand=36.0,
            disposal_cost=disposal,
        )
        depth = float(curve.loc[curve["margin"].idxmax()]["depth"])
        assert depth >= previous - 1e-9, (
            f"depth fell from {previous:.0%} to {depth:.0%} when disposal cost rose to {disposal}"
        )
        previous = depth
    assert previous > 0, "no disposal cost was ever enough to justify a markdown"


def test_the_fefo_queue_gates_what_a_markdown_can_rescue(con) -> None:
    """A cut lifts the store-SKU, and FEFO hands the units to the oldest batch.

    So a batch sitting behind a large queue is not reachable by price at all -
    the extra demand is absorbed in front of it. Charging the discount to the
    whole store-SKU while crediting only the at-risk batch is what stops the
    optimiser treating those units as free to rescue.
    """
    behind = evaluate(
        base_price=100.0,
        cost=60.0,
        posted_price=100.0,
        beta=-1.8,
        qty_remaining=50.0,
        units_ahead=500.0,
        horizon_demand=60.0,
    )
    assert (behind["batch_sold"] == 0).all(), (
        "a batch behind a 500-unit queue was credited with sales it cannot reach"
    )
    assert (behind["spend"] > 0).any(), "the discount on the queue ahead was not charged"
