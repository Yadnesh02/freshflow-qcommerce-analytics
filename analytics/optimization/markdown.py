"""Markdown optimiser: how deep to cut, on what, today (task S4.2).

S3.4 says which batches are at risk and what they are worth. This says what to
do about it, and the answer S4.1 forces is not the one the plan expected.

**The lever is marking down earlier, not deeper.** Elasticity by days-to-expiry
band, for the category that carries most of the markdown decision:

    band      0-1d   1-2d   2-3d   3-5d   5-7d    7d+
    Dairy       --  -0.09  -0.53  -0.54  -0.45  -0.32

Response peaks two to five days out and has collapsed to -0.09 with a day left.
The last day itself is not measurable at all - 0-1d comes back at -0.16 with a
standard error of 0.37, and does so for every category - so the honest statement
is that the price response falls away as expiry approaches and then disappears
into the noise. A shopper will not take curd expiring today at any price, so a
last-day discount buys almost no extra units and gives away the margin on the
ones that were going to sell anyway.

Gate G4 asks that depth increase monotonically as DTE falls
*holding demand constant*, and `check_monotonicity` verifies exactly that at a
fixed daily demand rate and a fixed coefficient. It passes, because it is a
statement about the shape of the objective: fewer days left means less of the
batch clears at any price, which makes the write-off term bigger and the deeper
cut worth more. Run instead on the *measured* per-band coefficients, the chosen
depth peaks in the middle bands and falls away at the end. Both are reported.
The first is the optimiser being correct; the second is the business answer.

**Nine cells have no measured price response and this optimiser will not price
them.** Every 0-1d cell, every 1-2d cell outside Dairy, and Fruits & Veg and
Ready to Eat at 2-3d came back with confidence intervals containing zero - and
two of them (+8.60 and +1.61) are the estimator visibly out of data rather than
finding anything. S4.1 falls all nine back to their category coefficient, which
is the right default for a table somebody reads and the wrong input for a
decision somebody acts on: they are the bands nearest expiry, and the category
means (-0.27 to -0.37) run three to four times the response the nearest
identified cell found (-0.09 at Dairy 1-2d). Taking the fallback would tell this
optimiser that expiring meat responds like mid-life meat, and push the deepest
cuts onto precisely the stock that does not answer to price.

    Note the guard is `is_identified`, not `elasticity is null`. The null path
    exists in S4.1 and fires only when the *category* is unidentified too, which
    no cell currently is - `select count(*) from marts.mart_price_elasticity
    where elasticity is null` returns 0. A guard written against null would have
    silently priced all nine.

**Bands are assigned half-open, because `dim_dte_band` overlaps.** The seed
gives 0-1d as [0,1] and 1-2d as [1,2], so a batch three days out matches both
2-3d and 3-5d and a plain `between` join returns two elasticities for one
decision. Read as `[min_days, max_days)` the intervals are disjoint - {0}, {1},
{2}, {3,4}, {5,6}, {7+} - and the seed other models already read is left alone.
S4.1 assigns identically, so each coefficient is applied to the population it was
fitted on.

    Not "earliest matching band", which was the first attempt and is wrong in a
    way that reads as right: taking the lowest sort_order hands the shared
    endpoint downward, so 0-1d collects {0,1}, 1-2d collects only {2}, and every
    label ends up describing the day above its own range.

**Price is posted per store-SKU; stock expires per batch.** They are not the
same decision unit, and collapsing them is the confusion this project exists to
remove. A markdown lifts demand for the whole store-SKU, and FEFO then hands the
extra units to the oldest batch first, so the at-risk batch is reached only once
the queue in front of it clears - a cut deep enough to move the SKU may still
not reach the units that are actually dying. The objective therefore counts the
*benefit* on the at-risk batch and the *cost* on every unit the discount
touches, queue included. Charging the discount only to the rescued units would
make every markdown look free.

**The objective, per candidate price ratio r against base price p0:**

    demand(r)     = horizon_demand * (r / r_now) ** beta
    batch_sold(r) = clip(demand(r) - units_ahead, 0, qty_remaining)
    total_sold(r) = min(demand(r), units_ahead + qty_remaining)
    margin(r)     = total_sold(r) * (p0 * r - cost)
                    - (qty_remaining - batch_sold(r)) * cost
    spend(r)      = total_sold(r) * p0 * (r_now - r)

Demand scales from the price already posted rather than from base, so depth 0
means "leave it alone" and a discount already reflected in the forecast is not
counted into it a second time.

**The cost floor is the landed cost and it binds.** Nothing is priced below what
it cost. That is a policy choice rather than a mathematical one - clearance
below cost can be rational when the alternative is a total write-off - but it is
the constraint the plan specifies and the one a category manager will sign.

**The budget makes this a knapsack rather than 674 independent decisions.**
Markdown spend is capped per store per day (default 1,000 rupees, against a
median of 907 the estate already spends), so depth is allocated by incremental
margin per rupee across a store's whole at-risk book. Each decision's grid is
reduced to its concave frontier first, which is what makes the greedy pass exact
for the relaxation rather than merely plausible.

    python tasks.py markdown
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

TARGET_TABLE = "marts.rec_markdown"

# Depth grid off base price. Five-point steps: finer than a category manager
# will act on, and the objective is flat enough near its optimum that 1pp steps
# move the chosen depth without moving the margin behind it.
DEPTH_GRID = np.round(np.arange(0.0, 0.65, 0.05), 2)

# Per store per day. The estate already spends a median of 907 rupees a day on
# markdown, so this is "the same money, allocated deliberately" rather than a
# new budget line - the only version of this a store manager can authorise.
DEFAULT_BUDGET_PER_STORE_DAY = 1000.0

# Why a batch got no depth. Kept apart because the fixes are different.
NOT_ESTIMATED = "S4.1 fitted no coefficient for this category and DTE band"
NO_SLOPE = "fitted, but the interval covers zero - no price response established"
NO_HEADROOM = "no feasible price between landed cost and the price already posted"


DECISION_SQL = """
with as_of as (

    select max(date_day) as as_of_date from marts.mart_expiry_risk

),

-- dim_dte_band's intervals share their endpoints, so a `between` join hands
-- back two bands for a batch three days out. Read half-open they are disjoint,
-- and the seed other models already read is left alone. NOT "earliest matching
-- band": that hands the shared endpoint downward and leaves every label
-- describing the day above its own range.
banded as (

    select
        risk.*,
        (
            select bands.dte_band
            from marts.dim_dte_band as bands
            where
                risk.days_to_expiry >= bands.min_days
                and risk.days_to_expiry < bands.max_days
            order by bands.sort_order
            limit 1
        ) as dte_band
    from marts.mart_expiry_risk as risk
    where risk.risk_state = 'at_risk' and risk.qty_remaining > 0

),

-- price posted on the as-of date; deepest of the two when promotions stack
posted as (

    select
        price.store_id,
        price.sku_id,
        min(price.realized_price) as posted_price,
        max(price.base_price) as base_price
    from marts.fct_price_history as price
    cross join as_of
    where as_of.as_of_date between price.effective_from_date and price.effective_to_date
    group by price.store_id, price.sku_id

)

select
    banded.batch_id,
    banded.store_id,
    banded.sku_id,
    banded.l1_category,
    banded.sku_name,
    banded.date_day,
    banded.days_to_expiry,
    banded.dte_band,
    banded.qty_remaining,
    banded.units_ahead_in_queue,
    banded.horizon_demand_mean,
    banded.residual_demand_mean,
    banded.units_at_risk,
    banded.unit_landed_cost,
    banded.value_at_risk_inr,

    coalesce(posted.base_price, products.base_price) as base_price,
    coalesce(posted.posted_price, products.base_price) as posted_price,

    elasticity.elasticity_raw,
    elasticity.standard_error,
    elasticity.is_identified,
    elasticity.elasticity_basis,
    elasticity.elasticity as elasticity_reported
from banded
inner join marts.dim_product as products on products.sku_id = banded.sku_id
left join posted
    on posted.store_id = banded.store_id and posted.sku_id = banded.sku_id
left join marts.mart_price_elasticity as elasticity
    on
        elasticity.l1_category = banded.l1_category
        and elasticity.dte_band = banded.dte_band
"""


def load_decisions(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    con.execute("set memory_limit = '4GB'")
    return con.execute(DECISION_SQL).df()


def usable_elasticity(frame: pd.DataFrame) -> pd.Series:
    """The coefficient this optimiser is willing to divide by.

    S4.1's `elasticity` column is never null in the current estimates: a cell
    that found no slope borrows its category's, which is right for a table
    somebody reads and wrong for a decision somebody acts on. Only a cell that
    measured its own downward slope gets priced here.
    """
    identified = frame["is_identified"].fillna(False).astype(bool)
    return frame["elasticity_raw"].where(identified)


def decline_reason(row: pd.Series) -> str:
    """Why a batch got no depth, kept separate because the fixes differ.

    Never estimated and estimated-but-flat look the same from the optimiser's
    seat and are different problems. The first is S4.1 declining to fit a
    category that is barely ever marked down - Staples is discounted on 0.09% of
    store-SKU-days - and the fix is a randomised depth, or nothing. The second
    is a fitted cell whose interval covers zero, and the fix is S5.1's policy
    backtest. Collapsing them would hide 51 batches inside 489.
    """
    if pd.isna(row.get("elasticity_raw")):
        return NOT_ESTIMATED
    return NO_SLOPE


def evaluate(
    base_price: float,
    cost: float,
    posted_price: float,
    beta: float,
    qty_remaining: float,
    units_ahead: float,
    horizon_demand: float,
    ratios: np.ndarray = DEPTH_GRID,
    disposal_cost: float = 0.0,
) -> pd.DataFrame:
    """Margin, spend and units at every candidate price, for one decision.

    Infeasible candidates - below the cost floor, or above the price already
    posted - are dropped rather than penalised, so nothing downstream has to
    know they were ever considered.

    **While demand is short of stock this objective collapses to revenue**, and
    that is worth seeing rather than discovering later. Sold and unsold units
    are each charged their landed cost exactly once, so with `d` units clearing
    out of `q` on hand the margin is `d*p - q*cost`: the cost term does not move
    with the price, and the decision is `max d(r) * p0 * r`, proportional to
    `r ** (1 + beta)`. The exponent is positive for every coefficient S4.1
    fitted, all of which are inside the unit interval, so the revenue given up
    on the units that were selling anyway exceeds the revenue won on the ones
    the cut brings in. **The optimiser's answer at the measured elasticities is
    therefore "do not mark down", and that is not a bug in the objective.** It
    agrees with the simulator's own baseline, where the flat 50% ladder runs at
    minus 22.7 lakh of margin.

    `disposal_cost` is the only thing that changes that, and it defaults to
    zero. A written-off unit in this dataset costs its landed cost and nothing
    more - the event stream carries no disposal, collection or reverse-logistics
    charge - so charging one would be inventing a number, in the way the
    42-rupee delivery cost in mart_customer_360 is a declared invention. It is a
    parameter here rather than a constant so S5.3 can sweep it, and the report
    prints the breakeven.
    """
    if base_price <= 0:
        return pd.DataFrame()
    now = posted_price / base_price
    candidates = np.asarray(1.0 - ratios, dtype=float)

    feasible = candidates[(candidates <= now + 1e-9) & (candidates * base_price >= cost)]
    if feasible.size == 0:
        return pd.DataFrame()

    demand = horizon_demand * np.power(feasible / now, beta)
    batch_sold = np.clip(demand - units_ahead, 0.0, qty_remaining)
    total_sold = np.minimum(demand, units_ahead + qty_remaining)

    unsold = qty_remaining - batch_sold
    margin = total_sold * (base_price * feasible - cost) - unsold * (cost + disposal_cost)
    spend = total_sold * base_price * (now - feasible)

    return pd.DataFrame(
        {
            "price_ratio": feasible,
            "depth": np.round(1.0 - feasible, 4),
            "price": base_price * feasible,
            "demand": demand,
            "batch_sold": batch_sold,
            "total_sold": total_sold,
            "margin": margin,
            "spend": spend,
        }
    )


def evaluate_row(
    row: pd.Series, ratios: np.ndarray = DEPTH_GRID, disposal_cost: float = 0.0
) -> pd.DataFrame:
    return evaluate(
        base_price=float(row["base_price"]),
        cost=float(row["unit_landed_cost"]),
        posted_price=float(row["posted_price"]),
        beta=float(row["beta"]),
        qty_remaining=float(row["qty_remaining"]),
        units_ahead=float(row["units_ahead_in_queue"]),
        horizon_demand=float(row["horizon_demand_mean"]),
        ratios=ratios,
        disposal_cost=disposal_cost,
    )


def concave_frontier(curve: pd.DataFrame) -> pd.DataFrame:
    """Keep only the steps a rational buyer of margin would ever take.

    Sorted by spend, a step survives when it beats every cheaper step on margin
    and its marginal return has not already been bettered by one behind it. That
    upper concave hull is what makes the greedy allocation exact for the budget
    relaxation instead of a heuristic that merely looks sensible.
    """
    curve = curve.sort_values("spend").reset_index(drop=True)
    kept = [0]
    for i in range(1, len(curve)):
        if curve.loc[i, "margin"] <= curve.loc[kept[-1], "margin"]:
            continue
        while len(kept) > 1:
            last, prev = kept[-1], kept[-2]
            gain_new = curve.loc[i, "margin"] - curve.loc[prev, "margin"]
            cost_new = max(curve.loc[i, "spend"] - curve.loc[prev, "spend"], 1e-9)
            gain_old = curve.loc[last, "margin"] - curve.loc[prev, "margin"]
            cost_old = max(curve.loc[last, "spend"] - curve.loc[prev, "spend"], 1e-9)
            if gain_new / cost_new >= gain_old / cost_old:
                kept.pop()
            else:
                break
        kept.append(i)
    return curve.loc[kept].reset_index(drop=True)


def allocate(curves: dict[str, pd.DataFrame], budget: float) -> dict[str, int]:
    """Greedy over incremental margin per rupee, until the budget is gone.

    Every decision starts on its cheapest feasible step - depth 0, costing
    nothing - and the best available upgrade anywhere in the store is taken
    repeatedly. On concave frontiers this solves the relaxed knapsack exactly;
    the integrality gap is one step on one decision.
    """
    position = dict.fromkeys(curves, 0)
    spent = sum(float(curve.loc[0, "spend"]) for curve in curves.values())

    while True:
        best_key, best_ratio, best_cost = None, 0.0, 0.0
        for key, curve in curves.items():
            nxt = position[key] + 1
            if nxt >= len(curve):
                continue
            gain = float(curve.loc[nxt, "margin"] - curve.loc[position[key], "margin"])
            cost = float(curve.loc[nxt, "spend"] - curve.loc[position[key], "spend"])
            if gain <= 0 or cost <= 0 or spent + cost > budget:
                continue
            if gain / cost > best_ratio:
                best_key, best_ratio, best_cost = key, gain / cost, cost
        if best_key is None:
            return position
        position[best_key] += 1
        spent += best_cost


def _declined(row: pd.Series, reason: str) -> dict:
    return {
        "batch_id": row["batch_id"],
        "store_id": row["store_id"],
        "sku_id": row["sku_id"],
        "sku_name": row["sku_name"],
        "l1_category": row["l1_category"],
        "date_day": row["date_day"],
        "days_to_expiry": int(row["days_to_expiry"]),
        "dte_band": row["dte_band"],
        "qty_remaining": float(row["qty_remaining"]),
        "units_at_risk": float(row["units_at_risk"]),
        "value_at_risk_inr": float(row["value_at_risk_inr"]),
        "elasticity_used": None,
        "elasticity_basis": row.get("elasticity_basis"),
        "base_price": float(row["base_price"]),
        "posted_price": float(row["posted_price"]),
        "recommended_depth": 0.0,
        "recommended_price": float(row["posted_price"]),
        "unconstrained_depth": 0.0,
        "is_budget_constrained": False,
        "expected_units_sold": 0.0,
        "expected_markdown_spend": 0.0,
        "expected_margin": 0.0,
        "margin_vs_do_nothing": 0.0,
        "decision": "no_recommendation",
        "decline_reason": reason,
    }


def optimise(decisions: pd.DataFrame, budget: float, disposal_cost: float = 0.0) -> pd.DataFrame:
    """One row per at-risk batch: a depth, or a stated reason there isn't one."""
    decisions = decisions.copy()
    decisions["beta"] = usable_elasticity(decisions)

    rows = [
        _declined(row, decline_reason(row))
        for _, row in decisions[decisions["beta"].isna()].iterrows()
    ]

    priced = decisions[decisions["beta"].notna()]
    for store, group in priced.groupby("store_id"):
        curves, unconstrained = {}, {}
        for _, row in group.iterrows():
            curve = evaluate_row(row, disposal_cost=disposal_cost)
            if curve.empty:
                rows.append(_declined(row, NO_HEADROOM))
                continue
            unconstrained[row["batch_id"]] = curve.loc[curve["margin"].idxmax()]
            curves[row["batch_id"]] = concave_frontier(curve)

        if not curves:
            continue

        for batch_id, index in allocate(curves, budget).items():
            row = group[group["batch_id"] == batch_id].iloc[0]
            pick = curves[batch_id].loc[index]
            free = unconstrained[batch_id]
            hold = curves[batch_id].loc[0]
            rows.append(
                {
                    "batch_id": batch_id,
                    "store_id": store,
                    "sku_id": row["sku_id"],
                    "sku_name": row["sku_name"],
                    "l1_category": row["l1_category"],
                    "date_day": row["date_day"],
                    "days_to_expiry": int(row["days_to_expiry"]),
                    "dte_band": row["dte_band"],
                    "qty_remaining": float(row["qty_remaining"]),
                    "units_at_risk": float(row["units_at_risk"]),
                    "value_at_risk_inr": float(row["value_at_risk_inr"]),
                    "elasticity_used": float(row["beta"]),
                    "elasticity_basis": "cell",
                    "base_price": float(row["base_price"]),
                    "posted_price": float(row["posted_price"]),
                    "recommended_depth": float(pick["depth"]),
                    "recommended_price": float(pick["price"]),
                    "unconstrained_depth": float(free["depth"]),
                    "is_budget_constrained": bool(pick["depth"] < free["depth"] - 1e-9),
                    "expected_units_sold": float(pick["batch_sold"]),
                    "expected_markdown_spend": float(pick["spend"]),
                    "expected_margin": float(pick["margin"]),
                    "margin_vs_do_nothing": float(pick["margin"] - hold["margin"]),
                    # against the price posted today, not against base. Stock
                    # already sitting at 30% off and told to stay there has
                    # depth 0.30 and is not a markdown - it is a decision to
                    # leave the ticket alone, and counting it as an action
                    # would put 22 phantom recommendations on the queue.
                    "decision": (
                        "markdown"
                        if float(pick["price"]) < float(row["posted_price"]) - 1e-9
                        else "hold_price"
                    ),
                    "decline_reason": None,
                }
            )
    return pd.DataFrame(rows)


# ============================================================ gate G4
def check_monotonicity(
    beta: float = -1.6,
    daily_demand: float = 12.0,
    qty_remaining: float = 120.0,
    base_price: float = 100.0,
    cost: float = 60.0,
    horizon_days: tuple[int, ...] = (7, 6, 5, 4, 3, 2, 1),
) -> pd.DataFrame:
    """G4: depth rises monotonically as DTE falls, holding demand constant.

    "Holding demand constant" has to mean the demand *rate*, not demand over the
    remaining life - the latter cannot be held constant while DTE moves, and the
    whole mechanism runs through it. Fewer days left is fewer days of selling at
    any price, so more of the batch is heading for the bin, so the price that
    just clears it is lower. The optimum sits exactly at that clearing price:
    above it stock is written off, below it the cut is being paid on units that
    would have sold anyway.

    **`beta` must be elastic or this gate passes while testing nothing.** Below
    |beta| = 1 no depth is ever chosen at any DTE, for the reason `evaluate`
    sets out, and a column of zeroes satisfies "non-decreasing" perfectly. The
    first draft ran at -0.45 - a plausible figure from the middle of what S4.1
    fitted - and reported PASS on a completely flat sweep. Every coefficient
    S4.1 fitted is inelastic, so running this gate on the real estimates would
    pass it by default and prove nothing about the objective. -1.6 is the
    smallest round elastic value, and the check is of the objective's shape,
    which is only visible where the lever moves.

    `qty_remaining` above the horizon demand is the milder second condition. A
    batch that clears itself is not at risk and takes depth 0 whatever the
    coefficient, so at 40 units against 84 of demand the first four bands go
    flat and only the last three carry information. The sweep still moves; it
    just stops covering the range. 120 units keeps all seven live.

    This is the optimiser's objective being correctly shaped, which is a
    different question from what the fitted coefficients then say to do. That
    answer is the opposite, and it is the finding rather than the bug.
    """
    rows = []
    for days in horizon_days:
        curve = evaluate(
            base_price=base_price,
            cost=cost,
            posted_price=base_price,
            beta=beta,
            qty_remaining=qty_remaining,
            units_ahead=0.0,
            horizon_demand=daily_demand * days,
        )
        best = curve.loc[curve["margin"].idxmax()]
        rows.append(
            {
                "days_to_expiry": days,
                "horizon_demand": daily_demand * days,
                "depth": float(best["depth"]),
                "margin": float(best["margin"]),
            }
        )
    return pd.DataFrame(rows)


def measured_depth_profile(decisions: pd.DataFrame) -> pd.DataFrame:
    """G4's sweep again, each band using the coefficient actually fitted to it.

    Identical synthetic batch to `check_monotonicity` - same stock, same rate,
    same cost - so the only thing changing between the two tables is that a
    constant elastic coefficient has been replaced by the estimates. That is the
    whole comparison: the gate passes on the constant and the business answer
    inverts on the estimates, and holding everything else fixed is what makes
    that attributable to the elasticities rather than to the fixture.
    """
    bands = (
        decisions.dropna(subset=["elasticity_raw"])
        .drop_duplicates(subset=["l1_category", "dte_band"])
        .sort_values(["l1_category", "days_to_expiry"])
    )
    rows = []
    for _, band in bands.iterrows():
        identified = bool(band["is_identified"])
        days = max(int(band["days_to_expiry"]), 1)
        if identified:
            curve = evaluate(
                base_price=100.0,
                cost=60.0,
                posted_price=100.0,
                beta=float(band["elasticity_raw"]),
                qty_remaining=120.0,
                units_ahead=0.0,
                horizon_demand=12.0 * days,
            )
            best = curve.loc[curve["margin"].idxmax()]
            depth, margin = float(best["depth"]), float(best["margin"])
        else:
            depth, margin = float("nan"), float("nan")
        rows.append(
            {
                "l1_category": band["l1_category"],
                "dte_band": band["dte_band"],
                "days_to_expiry": days,
                "elasticity": float(band["elasticity_raw"]),
                "is_identified": identified,
                "depth": depth,
                "margin": margin,
            }
        )
    return pd.DataFrame(rows)


def write(con: duckdb.DuckDBPyConnection, recommendations: pd.DataFrame) -> None:
    con.register("_markdown", recommendations)
    con.execute(f"create or replace table {TARGET_TABLE} as select * from _markdown")
    con.unregister("_markdown")


def report(recommendations: pd.DataFrame, decisions: pd.DataFrame, budget: float) -> int:
    as_of = decisions["date_day"].max()
    print(
        f"\n  {len(decisions):,} at-risk batches as at {as_of}, "
        f"budget Rs {budget:,.0f} per store per day\n"
    )

    priced = recommendations[recommendations["decision"] == "markdown"]
    held = recommendations[recommendations["decision"] == "hold_price"]
    declined = recommendations[recommendations["decision"] == "no_recommendation"]

    print(
        f"  {'marked down':<22}{len(priced):>6}  "
        f"Rs {priced['expected_markdown_spend'].sum():>10,.0f} spend  "
        f"Rs {priced['margin_vs_do_nothing'].sum():>10,.0f} margin gained"
    )
    print(
        f"  {'held at posted price':<22}{len(held):>6}  "
        f"{'':>14}  a cut does not pay at the fitted response"
    )
    print(
        f"  {'not priced':<22}{len(declined):>6}  "
        f"{'':>14}  Rs {declined['value_at_risk_inr'].sum():,.0f} at risk, left alone"
    )

    if not declined.empty:
        print("\n  declined, by reason:")
        for reason, group in declined.groupby("decline_reason"):
            print(
                f"    {len(group):>4} batches  Rs {group['value_at_risk_inr'].sum():>9,.0f}"
                f"  {reason}"
            )

        guarded = declined[declined["decline_reason"] == NO_SLOPE]
        if not guarded.empty:
            breakdown = (
                guarded.groupby(["l1_category", "dte_band"])
                .agg(batches=("batch_id", "size"), value=("value_at_risk_inr", "sum"))
                .reset_index()
                .sort_values("value", ascending=False)
            )
            print(f"\n  the {len(breakdown)} unidentified cells, which is the guard doing its job:")
            for _, row in breakdown.iterrows():
                print(
                    f"    {row['l1_category']:<24}{row['dte_band']:<7}"
                    f"{row['batches']:>5} batches  Rs {row['value']:>9,.0f}"
                )
            unmeasured = decisions["is_identified"] == False  # noqa: E712 - NaN must stay excluded
            fallback = decisions.loc[unmeasured, "elasticity_reported"]
            nearest = decisions.loc[decisions["is_identified"].fillna(False), "elasticity_raw"]
            print(
                "    Every one is a band near expiry. Taking S4.1's category fallback\n"
                f"    would have priced them at {fallback.min():.2f} to {fallback.max():.2f}, "
                f"against {nearest.max():.2f} at the\n"
                "    nearest cell that did find a slope - and cut deepest exactly where\n"
                "    the data says nobody responds."
            )

    if not priced.empty:
        print("\n  chosen depth by days to expiry (measured elasticities):")
        by_dte = (
            priced.groupby("days_to_expiry")
            .agg(
                batches=("batch_id", "size"),
                depth=("recommended_depth", "mean"),
                elasticity=("elasticity_used", "mean"),
                margin=("margin_vs_do_nothing", "sum"),
            )
            .reset_index()
        )
        print(f"    {'dte':>4}{'batches':>9}{'elasticity':>13}{'mean depth':>13}{'margin Rs':>13}")
        for _, row in by_dte.iterrows():
            print(
                f"    {row['days_to_expiry']:>4.0f}{row['batches']:>9.0f}"
                f"{row['elasticity']:>13.2f}{row['depth']:>12.0%}{row['margin']:>13,.0f}"
            )

        constrained = priced[priced["is_budget_constrained"]]
        if not constrained.empty:
            print(
                f"\n  {len(constrained)} of {len(priced)} recommendations are held "
                f"below their unconstrained depth by the budget"
            )

    print("\n  G4 - depth rises as DTE falls, at a constant coefficient and demand rate:")
    gate = check_monotonicity()
    print(f"    {'dte':>4}{'horizon demand':>17}{'depth':>9}{'margin Rs':>12}")
    for _, row in gate.iterrows():
        print(
            f"    {row['days_to_expiry']:>4.0f}{row['horizon_demand']:>17,.0f}"
            f"{row['depth']:>8.0%}{row['margin']:>12,.0f}"
        )

    depths = gate.sort_values("days_to_expiry", ascending=False)["depth"].to_numpy()
    monotone = bool(np.all(np.diff(depths) >= -1e-9))
    print(f"\n  gate G4: {'PASS' if monotone else 'FAIL'}", end="")
    print("  (non-decreasing as days to expiry fall)")

    print("\n  the identical batch, at the coefficients actually fitted:")
    profile = measured_depth_profile(decisions)
    for category, group in profile.groupby("l1_category"):
        cells = []
        for _, row in group.iterrows():
            shown = "  --" if not row["is_identified"] else f"{row['depth']:>4.0%}"
            cells.append(f"{row['dte_band']}:{shown}")
        print(f"    {category:<24}{'  '.join(cells)}")
    print("    ('--' is a band where no price response was measured, and none is guessed)")

    strongest = decisions.loc[decisions["elasticity_raw"].idxmin()]
    print(
        f"\n  Every fitted coefficient is inelastic. The strongest is "
        f"{strongest['elasticity_raw']:.2f}, at\n"
        f"  {strongest['l1_category']} {strongest['dte_band']}, and while stock is "
        "short of demand this objective is\n"
        "  revenue - which falls with price whenever |elasticity| < 1. So the\n"
        "  optimiser holds price nearly everywhere, and the breakeven it would\n"
        "  need is |elasticity| > 1.00, which no band reaches.\n"
        "\n  That is the S4.2 result and it is consistent with the simulator's own\n"
        "  baseline: the flat 50% markdown ladder runs at minus Rs 22.7 L of margin.\n"
        "  The lever is ordering less and marking down earlier, where response\n"
        "  peaks - not cutting deeper on the last day, where it has collapsed.\n"
    )

    return 0 if monotone else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Choose markdown depth for at-risk batches.")
    parser.add_argument("--warehouse", default=str(WAREHOUSE))
    parser.add_argument(
        "--budget",
        type=float,
        default=DEFAULT_BUDGET_PER_STORE_DAY,
        help="markdown spend cap per store per day, in rupees",
    )
    parser.add_argument(
        "--disposal-cost",
        type=float,
        default=0.0,
        help=(
            "AN ASSUMPTION. Cost per written-off unit beyond its landed cost. "
            "The event stream carries none, so the default is zero; S5.3 sweeps it"
        ),
    )
    args = parser.parse_args()

    warehouse = Path(args.warehouse)
    if not warehouse.exists():
        print(f"\n\033[31mno warehouse at {warehouse}\033[0m\n  run `python tasks.py build`")
        return 1

    con = duckdb.connect(str(warehouse))
    try:
        print("  loading at-risk batches...", flush=True)
        decisions = load_decisions(con)
        if decisions.empty:
            print("\n  nothing at risk - no markdown to recommend")
            return 0
        recommendations = optimise(decisions, args.budget, args.disposal_cost)
        write(con, recommendations)
        return report(recommendations, decisions, args.budget)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
