"""Deal-slot allocator: which SKUs get the Rs 11 slot, per store, per day (task S4.3).

Problem P6 says the Rs 11 deal is run as a marketing gimmick rather than a
system - one SKU picked centrally, the same for every store, with no link to
what is actually about to expire. That is measurable, and it is true:

    On the as-of date, 14 store-SKUs were on the deal and 674 store-SKUs were
    at risk of expiring. The intersection is EMPTY.

The mix says the same thing from the other side. Of the 52 SKUs that got a slot
over the year, six are Household & Cleaning at an average 788 days of shelf life
and three are Personal Care at 802 days. More than half the slots went to goods
that cannot meaningfully expire, so their clearance value is exactly zero and
the whole spend is subsidy. Ten Fruits & Vegetables SKUs at 4.7 days and six
Dairy at 6.2 are the only ones where the deal could have cleared anything.

**What the deal costs, measured rather than assumed.** On its own line the deal
runs at minus Rs 426,683 a year: 12,153 units, Rs 162,532 of revenue against
Rs 589,214 of COGS. Anything that justifies it has to come from the basket
around it or from the customers it brings back.

**The elasticity from S4.1 cannot be used here, and that is not a shortcut.**
That estimate deliberately excludes price ratios below 0.15, because the Rs 11
slot lands near 0.11 and a clearance stunt is not a point on the same demand
curve. So uptake is measured directly instead: for the 45 SKUs with both dealt
and undealt uncensored days, units on deal days against units off them give a
median multiplier of **3.57x** (IQR 2.47-5.71), lifting an average store-SKU
from 0.85 units a day to 3.76. Median rather than mean, because the mean of a
ratio with small denominators is 4.49 and is carried by a handful of SKUs that
barely sell otherwise.

**The four terms of the objective, and where each number comes from:**

  clearance_value   the landed cost of at-risk stock the slot actually clears,
                    which is a write-off avoided. Read from mart_expiry_risk and
                    capped by both units at risk and expected uptake. Zero for
                    the long-life SKUs the baseline kept choosing.

  incr_basket_margin  measured within store-day: orders that took the deal
                    carried Rs 77.90 of margin on the REST of the basket
                    against Rs 71.63 for orders that did not, so +Rs 6.27.
                    Observational and generous - shoppers who take a deal are
                    not a random sample of shoppers - so it is an upper bound
                    on this term, not an estimate of it.

  reactivation_value  deal orders are 2.1x more likely to be a customer
                    returning after a 30-day gap (5.31% against 2.48%) and 2.4x
                    at 60 days, while first-ever orders are slightly *lower* -
                    so the slot reactivates rather than acquires. The rate is
                    measured; what a reactivation is worth is not, and cannot be
                    from this event stream. It is a declared parameter defaulting
                    to zero, in the same way the Rs 42 delivery cost and the
                    markdown disposal cost are declared, and S5.3 sweeps it.

  subsidy           not (base - 11) x units, which would charge the discount to
                    units that were never going to sell. The incremental item
                    margin is `uptake x (11 - cost) - baseline_units x (base -
                    cost)`: what the slot earns minus what those units would
                    have earned at their normal price and volume.

**Stacked promotions belong to the deal.** When a markdown and the slot run on
the same SKU on the same day - 33 store-SKU-days, 132 units - the shopper paid
Rs 11 because the slot said so, so the slot owns the units and the markdown
does not. `assert_stacked_promotions_attribute_to_the_price_setter` enforces it,
which is what stops this allocator and the markdown optimiser both claiming the
same 132 units when their performance marts are built.

**The five constraints are the plan's, and the private-label floor is the gate.**
At most K slots per store-day; at most one per L2 subcategory, so a store cannot
spend every slot on chocolate; a minimum on-hand, because a slot on four units
sells out by nine and advertises an empty shelf; at least one day of shelf life,
since a slot on stock expiring tonight cannot be fulfilled all day; and at least
30% of slots to private label, which is P5's margin argument expressed as a
quota the optimiser has to respect rather than a hope.

    python tasks.py deal-slots
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path

import duckdb
import pandas as pd
import pulp

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

TARGET_TABLE = "marts.rec_deal_slot"

# The deal price itself. A fixed rupee point, not a percentage - which is why it
# is deeper than any markdown and always the price that binds when they stack.
DEAL_PRICE = 11.0

# Measured, not assumed: units on deal days over units off them, median across
# the 45 SKUs with both. See the module docstring for why median over mean.
DEAL_UPLIFT_MULTIPLIER = 3.57

# Rest-of-basket margin on orders that took the deal minus orders that did not,
# within the same store-day. An upper bound - deal-takers self-select.
INCREMENTAL_BASKET_MARGIN = 6.27

# Incremental probability that a deal order is a customer returning after a
# 30-day gap: 5.31% against 2.48% on the same store-days.
REACTIVATION_RATE = 0.0283

# AN ASSUMPTION, NOT A MEASUREMENT, and the term the answer turns on. What a
# reactivated customer is worth cannot be read from this event stream: it needs
# a counterfactual about whether they would otherwise have been lost. Zero is
# the value the data supports; the report prints the breakeven and S5.3 sweeps
# it alongside the Rs 42 delivery cost and the markdown disposal cost.
DEFAULT_REACTIVATION_VALUE = 0.0

# The plan's five constraints.
DEFAULT_SLOTS_PER_STORE_DAY = 3  # the baseline ran 1.14
MIN_ON_HAND_UNITS = 12  # a slot on four units advertises an empty shelf by 09:00
MIN_DAYS_TO_EXPIRY = 1  # stock expiring tonight cannot be fulfilled all day
PRIVATE_LABEL_FLOOR = 0.30  # P5's margin argument, as a quota


CANDIDATE_SQL = f"""
with as_of as (

    select max(date_day) as as_of_date from marts.mart_expiry_risk

),

-- what each store-SKU normally sells in a day, off deal. The trailing mean is
-- already computed in the aggregate and is the right base for a multiplier.
baseline as (

    select
        sales.store_id,
        sales.sku_id,
        sales.trailing_7d_avg_units as base_units_per_day
    from marts.agg_store_sku_day as sales
    cross join as_of
    where
        sales.date_day = as_of.as_of_date
        and sales.trailing_7d_avg_units > 0

),

-- stock on hand and how much of it is dying, per store-SKU
position as (

    select
        risk.store_id,
        risk.sku_id,
        sum(risk.qty_remaining) as on_hand_units,
        min(risk.days_to_expiry) as days_to_expiry,
        sum(risk.units_at_risk) filter (where risk.risk_state = 'at_risk') as units_at_risk,
        sum(risk.value_at_risk_inr) filter (where risk.risk_state = 'at_risk') as value_at_risk,
        max(risk.unit_landed_cost) as unit_landed_cost
    from marts.mart_expiry_risk as risk
    where risk.risk_state <> 'expired' and risk.qty_remaining > 0
    group by risk.store_id, risk.sku_id

),

-- the price the slot is discounting from
posted as (

    select
        price.store_id,
        price.sku_id,
        min(price.realized_price) as posted_price
    from marts.fct_price_history as price
    cross join as_of
    where as_of.as_of_date between price.effective_from_date and price.effective_to_date
    group by price.store_id, price.sku_id

)

select
    as_of.as_of_date as date_day,
    position.store_id,
    position.sku_id,
    products.sku_name,
    products.l1_category,
    products.l2_subcategory,
    products.is_private_label,
    coalesce(posted.posted_price, products.base_price) as base_price,
    position.unit_landed_cost,
    position.on_hand_units,
    position.days_to_expiry,
    coalesce(position.units_at_risk, 0) as units_at_risk,
    coalesce(position.value_at_risk, 0) as value_at_risk,
    baseline.base_units_per_day
from position
cross join as_of
inner join baseline
    on baseline.store_id = position.store_id and baseline.sku_id = position.sku_id
inner join marts.dim_product as products
    on products.sku_id = position.sku_id
left join posted
    on posted.store_id = position.store_id and posted.sku_id = position.sku_id
where
    position.on_hand_units >= {MIN_ON_HAND_UNITS}
    and position.days_to_expiry >= {MIN_DAYS_TO_EXPIRY}
    and coalesce(posted.posted_price, products.base_price) > {DEAL_PRICE}
"""


def load_candidates(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    con.execute("set memory_limit = '4GB'")
    return con.execute(CANDIDATE_SQL).df()


def score(candidates: pd.DataFrame, reactivation_value: float) -> pd.DataFrame:
    """The four objective terms, per candidate, in rupees.

    Uptake is capped by stock: a slot cannot sell units the store does not hold,
    and uncapped uptake is how an optimiser talks itself into a slot on a SKU
    with eleven units left.
    """
    scored = candidates.copy()

    uptake = scored["base_units_per_day"] * DEAL_UPLIFT_MULTIPLIER
    scored["expected_units"] = uptake.combine(scored["on_hand_units"], min)

    # a write-off avoided, capped by what is actually dying
    cleared = scored[["expected_units", "units_at_risk"]].min(axis=1)
    scored["clearance_value"] = cleared * scored["unit_landed_cost"]

    scored["basket_value"] = scored["expected_units"] * INCREMENTAL_BASKET_MARGIN
    scored["reactivation_value"] = scored["expected_units"] * REACTIVATION_RATE * reactivation_value

    # what the slot earns on the item, minus what those units would have earned
    # anyway. Charging (base - 11) x uptake instead would bill the discount to
    # units that were never going to sell.
    on_deal = scored["expected_units"] * (DEAL_PRICE - scored["unit_landed_cost"])
    without = scored["base_units_per_day"] * (scored["base_price"] - scored["unit_landed_cost"])
    scored["item_margin_delta"] = on_deal - without
    scored["subsidy"] = -scored["item_margin_delta"]

    scored["slot_value"] = (
        scored["clearance_value"]
        + scored["basket_value"]
        + scored["reactivation_value"]
        + scored["item_margin_delta"]
    )
    return scored


def allocate_store_day(
    store_candidates: pd.DataFrame, slots: int, pl_floor: float
) -> tuple[pd.DataFrame, str]:
    """The integer program, for one store on one day.

    maximise  sum over s of  x_s * slot_value_s
    s.t.      sum x_s <= K
              sum over s in c of x_s <= 1   for each L2 subcategory c
              sum x_s * is_private_label >= ceil(pl_floor * K)
              x_s in {0, 1}

    On-hand and days-to-expiry are enforced in the candidate query rather than
    as rows in the program: a SKU that fails them is not a candidate at all, and
    carrying it into the model as an always-zero variable only makes the
    infeasibility harder to read when the private-label floor cannot be met.
    """
    # PuLP 3.3 warns that both LpVariable(...) and PULP_CBC_CMD move in 4.0. The
    # replacements do not exist in 3.3, so this cannot be pre-adopted, and the
    # warnings are left visible rather than filtered - a silenced warning about a
    # solver API that is going to break is a problem deferred into a future
    # session with no reminder attached to it.
    problem = pulp.LpProblem("deal_slots", pulp.LpMaximize)
    choose = {
        row.sku_id: pulp.LpVariable(f"x_{row.sku_id}", cat="Binary")
        for row in store_candidates.itertuples()
    }

    problem += pulp.lpSum(
        choose[row.sku_id] * row.slot_value for row in store_candidates.itertuples()
    )

    problem += pulp.lpSum(choose.values()) <= slots, "slot_count"

    for subcategory, group in store_candidates.groupby("l2_subcategory"):
        problem += (
            pulp.lpSum(choose[sku] for sku in group["sku_id"]) <= 1,
            f"one_per_subcategory_{subcategory}".replace(" ", "_").replace("&", "and"),
        )

    # Ceiling, not truncation. int(0.30 * 3) is 0, which would make the floor
    # vacuous at the default K and let the gate pass while testing nothing.
    required_pl = math.ceil(pl_floor * slots)
    private_label = store_candidates[store_candidates["is_private_label"]]["sku_id"]
    if required_pl > 0:
        problem += (
            pulp.lpSum(choose[sku] for sku in private_label) >= required_pl,
            "private_label_floor",
        )

    problem.solve(pulp.PULP_CBC_CMD(msg=False))
    status = pulp.LpStatus[problem.status]
    if status != "Optimal":
        return store_candidates.head(0), status

    picked = [sku for sku, var in choose.items() if var.value() == 1]
    return store_candidates[store_candidates["sku_id"].isin(picked)], status


def allocate(candidates: pd.DataFrame, slots: int, pl_floor: float) -> pd.DataFrame:
    """Run the program per store-day and stack the winners into one table."""
    rows, statuses = [], {}
    for (date_day, store_id), group in candidates.groupby(["date_day", "store_id"]):
        chosen, status = allocate_store_day(group, slots, pl_floor)
        statuses[(date_day, store_id)] = status
        for rank, row in enumerate(
            chosen.sort_values("slot_value", ascending=False).itertuples(), start=1
        ):
            rows.append(
                {
                    "date_day": date_day,
                    "store_id": store_id,
                    "slot_rank": rank,
                    "sku_id": row.sku_id,
                    "sku_name": row.sku_name,
                    "l1_category": row.l1_category,
                    "l2_subcategory": row.l2_subcategory,
                    "is_private_label": bool(row.is_private_label),
                    "base_price": float(row.base_price),
                    "deal_price": DEAL_PRICE,
                    "unit_landed_cost": float(row.unit_landed_cost),
                    "on_hand_units": float(row.on_hand_units),
                    "days_to_expiry": int(row.days_to_expiry),
                    "units_at_risk": float(row.units_at_risk),
                    "expected_units": float(row.expected_units),
                    "clearance_value": float(row.clearance_value),
                    "basket_value": float(row.basket_value),
                    "reactivation_value": float(row.reactivation_value),
                    "subsidy": float(row.subsidy),
                    "item_margin_delta": float(row.item_margin_delta),
                    "slot_value": float(row.slot_value),
                    "solver_status": status,
                }
            )
    allocated = pd.DataFrame(rows)
    allocated.attrs["statuses"] = statuses
    return allocated


def write(con: duckdb.DuckDBPyConnection, allocated: pd.DataFrame) -> None:
    con.register("_deal_slots", allocated)
    con.execute(f"create or replace table {TARGET_TABLE} as select * from _deal_slots")
    con.unregister("_deal_slots")


def breakeven_reactivation_value(scored: pd.DataFrame) -> float:
    """What a reactivation must be worth before the median candidate pays for itself.

    Solving `slot_value = 0` for the one term that is declared rather than
    measured, so the assumption can be argued with a number instead of defended
    in prose.
    """
    without = scored["clearance_value"] + scored["basket_value"] + scored["item_margin_delta"]
    per_rupee = scored["expected_units"] * REACTIVATION_RATE
    needed = (-without / per_rupee.replace(0, pd.NA)).dropna()
    positive = needed[needed > 0]
    return float(positive.median()) if len(positive) else 0.0


def report(allocated: pd.DataFrame, scored: pd.DataFrame, slots: int, pl_floor: float) -> int:
    statuses = allocated.attrs.get("statuses", {})
    store_days = len(statuses)
    optimal = sum(1 for s in statuses.values() if s == "Optimal")

    print(f"\n  {len(scored):,} candidate store-SKUs over {store_days} store-days")
    print(f"  {len(allocated):,} slots allocated, K={slots}, private-label floor {pl_floor:.0%}\n")

    print(f"  solver: {optimal}/{store_days} store-days optimal")
    infeasible = {k: v for k, v in statuses.items() if v != "Optimal"}
    if infeasible:
        print(f"  NOT SOLVED on {len(infeasible)}: {sorted(set(infeasible.values()))}")

    if allocated.empty:
        print("\n  nothing allocated")
        return 1

    required_pl = math.ceil(pl_floor * slots)
    per_store = allocated.groupby(["date_day", "store_id"]).agg(
        picked=("sku_id", "size"), private_label=("is_private_label", "sum")
    )
    floor_ok = bool((per_store["private_label"] >= required_pl).all())
    over_k = int((per_store["picked"] > slots).sum())
    worst_subcategory = int(
        allocated.groupby(["date_day", "store_id", "l2_subcategory"]).size().max()
    )

    print("\n  the five constraints:")
    print(f"    slots per store-day <= {slots:<24}{'PASS' if over_k == 0 else 'FAIL'}")
    print(
        f"    at most one per L2 subcategory{'':<13}{'PASS' if worst_subcategory <= 1 else 'FAIL'}"
    )
    print(f"    on-hand >= {MIN_ON_HAND_UNITS} units{'':<20}PASS  (screened in candidates)")
    print(f"    days to expiry >= {MIN_DAYS_TO_EXPIRY}{'':<23}PASS  (screened in candidates)")
    print(
        f"    private label >= {required_pl} of {slots} slots{'':<11}"
        f"{'PASS' if floor_ok else 'FAIL'}"
    )

    print("\n  where the value comes from:")
    for label, column in [
        ("clearance (write-off avoided)", "clearance_value"),
        ("incremental basket margin", "basket_value"),
        ("reactivation", "reactivation_value"),
        ("item margin vs no deal", "item_margin_delta"),
    ]:
        print(f"    {label:<32}{allocated[column].sum():>14,.0f}")
    print(f"    {'net slot value':<32}{allocated['slot_value'].sum():>14,.0f}")

    clearing = int((allocated["clearance_value"] > 0).sum())
    print(
        f"\n  {clearing} of {len(allocated)} allocated slots clear at-risk stock "
        f"({clearing / len(allocated):.0%}),"
    )
    print("  against 0 of 14 on the as-of date under the central pick.")

    print("\n  most valuable slots:")
    header = (
        f"    {'store':<12}{'sku':<12}{'category':<22}{'PL':<4}"
        f"{'units':>7}{'clear':>9}{'value':>10}"
    )
    print(header)
    for row in allocated.nlargest(8, "slot_value").itertuples():
        print(
            f"    {row.store_id:<12}{row.sku_id:<12}{row.l1_category[:21]:<22}"
            f"{'Y' if row.is_private_label else '-':<4}"
            f"{row.expected_units:>7.1f}{row.clearance_value:>9,.0f}{row.slot_value:>10,.0f}"
        )

    breakeven = breakeven_reactivation_value(scored)
    positive = int((scored["slot_value"] > 0).sum())
    print(
        f"\n  Reactivation is worth Rs 0 by default, because this event stream cannot say\n"
        f"  what it is worth. Even at zero, {positive} candidates carry positive value on\n"
        f"  clearance and basket alone. The median candidate that does not needs a\n"
        f"  reactivation worth Rs {breakeven:,.0f} before it pays for itself - the number\n"
        f"  S5.3 sweeps, beside the Rs 42 delivery cost and the markdown disposal cost.\n"
    )

    gate = floor_ok and over_k == 0 and worst_subcategory <= 1 and optimal == store_days
    print(f"  S4.3 gate - solver feasible, PL floor respected: {'PASS' if gate else 'FAIL'}\n")
    return 0 if gate else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Allocate the Rs 11 deal slots per store-day.")
    parser.add_argument("--warehouse", default=str(WAREHOUSE))
    parser.add_argument("--slots", type=int, default=DEFAULT_SLOTS_PER_STORE_DAY)
    parser.add_argument("--pl-floor", type=float, default=PRIVATE_LABEL_FLOOR)
    parser.add_argument(
        "--reactivation-value",
        type=float,
        default=DEFAULT_REACTIVATION_VALUE,
        help=(
            "AN ASSUMPTION. Rupees a reactivated customer is worth. The event stream "
            "carries no counterfactual, so the default is zero; S5.3 sweeps it"
        ),
    )
    args = parser.parse_args()

    warehouse = Path(args.warehouse)
    if not warehouse.exists():
        print(f"\n\033[31mno warehouse at {warehouse}\033[0m\n  run `python tasks.py build`")
        return 1

    con = duckdb.connect(str(warehouse))
    try:
        print("  building the candidate set...", flush=True)
        candidates = load_candidates(con)
        if candidates.empty:
            print("\n  no candidate passes the on-hand and shelf-life screens")
            return 1
        scored = score(candidates, args.reactivation_value)
        allocated = allocate(scored, args.slots, args.pl_floor)
        write(con, allocated)
        return report(allocated, scored, args.slots, args.pl_floor)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
