"""Inter-store transfer engine: move stock that is dying to a store that is short (S4.4).

Problem P3 is the worst pairing in perishable retail - a write-off and a lost
sale on the same SKU on the same day, in two stores twelve kilometres apart.
It is not hypothetical here:

    On the as-of date, 674 store-SKUs hold stock at risk of expiring and 4,332
    store-SKUs went short in the preceding week. Requiring both an imputed
    shortfall and a positive sell rate at the receiving store leaves 1,557
    candidate from-to-SKU arcs.

**And almost none of them are worth moving, for a reason that is not distance.**
The gate removes 97%, but only 249 of those fail because transit exceeds the
remaining shelf life. 1,343 fail because the origin holds fewer than six units
at risk, and 996 because the destination was short by fewer than six. Stranded
stock in this network is a handful of units per store-SKU, not crates - the same
dregs population `mart_expiry_risk` documents as its blind spot - and a Rs 300
van does not cross Mumbai for three units of paneer. 47 arcs survive, over two
SKUs, and the solver takes three of them for a net Rs 299 a day. **The honest
S4.4 finding is that inter-store transfer is real but marginal here, and the
binding constraint is quantity rather than geography.**

**The shelf-life gate is the whole task, and it has a linear form.** The plan
states it as `transit_time + expected_sell_days < remaining_shelf_life`, which
looks like it needs the transfer quantity before it can be evaluated - and the
quantity is what the optimiser is choosing. It does not. Rearranged, the same
condition is an upper bound on the arc:

    max_units = destination_daily_rate x (days_to_expiry - transit_days)

Stock that cannot clear at the receiving store before it expires simply has a
smaller bound, and an arc that cannot survive transit at all gets a bound of
zero and drops out of the program. So the gate is enforced by construction
rather than checked afterwards, and there is no way for the solver to return a
transfer that fails it. `test_no_recommended_transfer_can_miss_its_expiry`
asserts that on the output anyway, because a constraint that is only true by
argument is one refactor from being false.

**A fixed charge per trip, not per unit, which makes this a MILP.** A van going
from Bandra to Powai costs the same whether it carries one crate or twenty, so
cost per unit is wrong in the direction that matters: it makes small transfers
look proportionally cheap when they are the ones that never pay. The model
carries a binary per store-pair and flows for every SKU on that pair, so the
trip is paid once and shared - which is also why the program is solved across
all SKUs at once rather than SKU by SKU. Solving per SKU would charge each one
its own van.

**Four declared assumptions, because this dataset has no logistics in it.**
The event stream carries no vehicle, no route, no fuel and no driver. Distances
are real - computed haversine from the store dimension's lat/lon, 2.33 km at the
closest pair and 26.89 km at the furthest, median 12.84 - but everything that
turns a distance into a cost is a parameter, declared here and swept by S5.3
alongside the Rs 42 delivery cost, the markdown disposal cost and the deal-slot
reactivation value:

    speed_kmh        18    Mumbai van, allowing for traffic
    handling_hours   2.0   pick, pack, load, unload at both ends
    cost_per_km      15.0  fuel, wear, driver time
    fixed_trip_cost  300.0 the trip itself, paid once per store pair

**A transfer has to pay for itself.** The benefit is the write-off avoided at
the origin - the units were going in the bin, so their landed cost is recovered
- plus the margin earned at the destination, where the demand exists. Against
that sits the trip. Any transfer whose total benefit does not clear its own cost
is not recommended, which is P3's condition stated exactly.

    python tasks.py transfers
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import duckdb
import pandas as pd
import pulp

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

TARGET_TABLE = "marts.rec_transfer_order"

# ASSUMPTIONS. The event stream carries no logistics at all - no vehicle, no
# route, no fuel, no driver - so each of these is declared rather than measured,
# and S5.3 sweeps them. The distances they act on are real.
DEFAULT_SPEED_KMH = 18.0
DEFAULT_HANDLING_HOURS = 2.0
DEFAULT_COST_PER_KM = 15.0
DEFAULT_FIXED_TRIP_COST = 300.0

# Below this a transfer is a rounding error moved across Mumbai.
MIN_TRANSFER_UNITS = 6

# How far back to read unmet demand at the receiving store.
DEFICIT_LOOKBACK_DAYS = 7


CANDIDATE_SQL = """
with as_of as (

    select max(date_day) as as_of_date from marts.mart_expiry_risk

),

-- stock that the risk model expects not to sell where it currently sits
surplus as (

    select
        risk.store_id,
        risk.sku_id,
        sum(risk.units_at_risk) as units_at_risk,
        min(risk.days_to_expiry) as days_to_expiry,
        max(risk.unit_landed_cost) as unit_landed_cost
    from marts.mart_expiry_risk as risk
    where risk.risk_state = 'at_risk' and risk.units_at_risk > 0
    group by risk.store_id, risk.sku_id

),

-- demand that existed and was not served, at some other store
deficit as (

    select
        sales.store_id,
        sales.sku_id,
        sum(sales.units_demanded_imputed - sales.units_sold) as unmet_units,
        avg(sales.trailing_7d_avg_units) as daily_rate
    from marts.agg_store_sku_day as sales
    cross join as_of
    where
        sales.date_day between as_of.as_of_date - {lookback} and as_of.as_of_date
        and sales.is_censored
        and sales.units_demanded_imputed > sales.units_sold
    group by sales.store_id, sales.sku_id
    having avg(sales.trailing_7d_avg_units) > 0

),

-- real geography, from the store dimension
distances as (

    select
        origin.store_id as from_store,
        destination.store_id as to_store,
        2 * 6371 * asin(sqrt(
            pow(sin(radians(destination.lat - origin.lat) / 2), 2)
            + cos(radians(origin.lat))
            * cos(radians(destination.lat))
            * pow(sin(radians(destination.lon - origin.lon) / 2), 2)
        )) as km
    from marts.dim_store as origin
    cross join marts.dim_store as destination
    where origin.store_id <> destination.store_id

)

select
    as_of.as_of_date as date_day,
    distances.from_store,
    distances.to_store,
    surplus.sku_id,
    products.sku_name,
    products.l1_category,
    round(distances.km, 3) as km,
    surplus.units_at_risk,
    surplus.days_to_expiry,
    surplus.unit_landed_cost,
    deficit.unmet_units,
    deficit.daily_rate,
    products.base_price - surplus.unit_landed_cost as unit_margin
from surplus
inner join deficit
    on deficit.sku_id = surplus.sku_id and deficit.store_id <> surplus.store_id
inner join distances
    on
        distances.from_store = surplus.store_id
        and distances.to_store = deficit.store_id
inner join marts.dim_product as products
    on products.sku_id = surplus.sku_id
cross join as_of
"""


def load_candidates(con: duckdb.DuckDBPyConnection, lookback: int) -> pd.DataFrame:
    con.execute("set memory_limit = '4GB'")
    return con.execute(CANDIDATE_SQL.format(lookback=lookback)).df()


def apply_shelf_life_gate(
    candidates: pd.DataFrame, speed_kmh: float, handling_hours: float
) -> pd.DataFrame:
    """Turn `transit + sell_days < shelf_life` into an upper bound per arc.

    The plan states the gate as a comparison that needs the transfer quantity,
    which the optimiser has not chosen yet. Rearranged it is a bound:

        max_units = daily_rate x (days_to_expiry - transit_days)

    An arc whose transit alone exceeds the remaining shelf life gets a bound at
    or below zero and is dropped, so infeasible transfers cannot be expressed in
    the program rather than merely being unattractive within it.
    """
    gated = candidates.copy()
    gated["transit_hours"] = handling_hours + gated["km"] / speed_kmh
    gated["transit_days"] = gated["transit_hours"] / 24.0
    gated["sellable_days"] = gated["days_to_expiry"] - gated["transit_days"]
    gated["max_units"] = (gated["daily_rate"] * gated["sellable_days"]).clip(lower=0.0)

    # never move more than exists, nor more than the receiving store is short
    gated["arc_cap"] = gated[["max_units", "units_at_risk", "unmet_units"]].min(axis=1)
    gated["arc_cap"] = gated["arc_cap"].round().astype(int)

    gated["unit_benefit"] = gated["unit_landed_cost"] + gated["unit_margin"].clip(lower=0.0)
    return gated[gated["arc_cap"] >= MIN_TRANSFER_UNITS].reset_index(drop=True)


def solve(
    arcs: pd.DataFrame, cost_per_km: float, fixed_trip_cost: float
) -> tuple[pd.DataFrame, str]:
    """Fixed-charge network flow over every surviving arc at once.

    maximise  sum x_ijs * unit_benefit_s  -  sum y_ij * (fixed + cost_per_km * km_ij)
    s.t.      x_ijs <= cap_ijs * y_ij            a trip must be paid for to be used
              sum_js x_ijs <= at_risk_is         cannot send stock a store does not have
              sum_i  x_ijs <= unmet_js           cannot fill a gap larger than it is
              x integer >= 0,  y binary

    Solved across all SKUs together rather than SKU by SKU, because the trip is
    the same van: charging each SKU its own fixed cost would make every transfer
    look uneconomic and is the single easiest way to get this answer wrong.
    """
    problem = pulp.LpProblem("transfers", pulp.LpMaximize)

    pairs = arcs[["from_store", "to_store", "km"]].drop_duplicates()
    trip = {
        (row.from_store, row.to_store): pulp.LpVariable(
            f"y_{row.from_store}_{row.to_store}", cat="Binary"
        )
        for row in pairs.itertuples()
    }
    move = {
        (row.from_store, row.to_store, row.sku_id): pulp.LpVariable(
            f"x_{row.from_store}_{row.to_store}_{row.sku_id}",
            lowBound=0,
            upBound=int(row.arc_cap),
            cat="Integer",
        )
        for row in arcs.itertuples()
    }

    benefit = pulp.lpSum(
        move[(row.from_store, row.to_store, row.sku_id)] * row.unit_benefit
        for row in arcs.itertuples()
    )
    cost = pulp.lpSum(
        trip[(row.from_store, row.to_store)] * (fixed_trip_cost + cost_per_km * row.km)
        for row in pairs.itertuples()
    )
    problem += benefit - cost

    # a SKU can only ride a trip that is being paid for
    for row in arcs.itertuples():
        key = (row.from_store, row.to_store, row.sku_id)
        problem += move[key] <= int(row.arc_cap) * trip[(row.from_store, row.to_store)]

    # cannot send more than is actually at risk at the origin
    for (store, sku), group in arcs.groupby(["from_store", "sku_id"]):
        available = int(group["units_at_risk"].iloc[0])
        problem += (
            pulp.lpSum(move[(store, row.to_store, sku)] for row in group.itertuples()) <= available,
            f"supply_{store}_{sku}",
        )

    # cannot fill more than the receiving store was short
    for (store, sku), group in arcs.groupby(["to_store", "sku_id"]):
        needed = int(group["unmet_units"].iloc[0])
        problem += (
            pulp.lpSum(move[(row.from_store, store, sku)] for row in group.itertuples()) <= needed,
            f"demand_{store}_{sku}",
        )

    problem.solve(pulp.PULP_CBC_CMD(msg=False))
    status = pulp.LpStatus[problem.status]
    if status != "Optimal":
        return arcs.head(0), status

    rows = []
    for row in arcs.itertuples():
        qty = move[(row.from_store, row.to_store, row.sku_id)].value() or 0
        if qty < MIN_TRANSFER_UNITS:
            continue
        pair_cost = fixed_trip_cost + cost_per_km * row.km
        rows.append(
            {
                "date_day": row.date_day,
                "from_store": row.from_store,
                "to_store": row.to_store,
                "sku_id": row.sku_id,
                "sku_name": row.sku_name,
                "l1_category": row.l1_category,
                "km": float(row.km),
                "transit_hours": float(row.transit_hours),
                "days_to_expiry": int(row.days_to_expiry),
                "sellable_days_after_transit": float(row.sellable_days),
                "units": int(qty),
                "arc_cap": int(row.arc_cap),
                "unit_landed_cost": float(row.unit_landed_cost),
                "avoided_writeoff": float(qty * row.unit_landed_cost),
                "recovered_margin": float(qty * max(row.unit_margin, 0.0)),
                "trip_cost": float(pair_cost),
                "solver_status": status,
            }
        )
    return pd.DataFrame(rows), status


def attribute_trip_cost(moved: pd.DataFrame) -> pd.DataFrame:
    """Split each trip's fixed cost across the units riding on it.

    A van from Bandra to Powai is paid once. Charging the whole trip to every
    SKU on it would make a two-SKU transfer look twice as expensive as it is,
    and net benefit is what decides whether the transfer is recommended at all.
    """
    if moved.empty:
        return moved
    shared = moved.copy()
    units_on_trip = shared.groupby(["from_store", "to_store"])["units"].transform("sum")
    shared["trip_cost_share"] = shared["trip_cost"] * shared["units"] / units_on_trip
    shared["net_benefit"] = (
        shared["avoided_writeoff"] + shared["recovered_margin"] - shared["trip_cost_share"]
    )
    return shared


def write(con: duckdb.DuckDBPyConnection, moved: pd.DataFrame) -> None:
    con.register("_transfers", moved)
    con.execute(f"create or replace table {TARGET_TABLE} as select * from _transfers")
    con.unregister("_transfers")


def report(moved: pd.DataFrame, arcs: pd.DataFrame, raw: pd.DataFrame, status: str) -> int:
    print(f"\n  {len(raw):,} candidate from-to-SKU arcs before the shelf-life gate")
    print(
        f"  {len(arcs):,} survive it, over {arcs.groupby(['from_store', 'to_store']).ngroups} "
        f"store pairs and {arcs['sku_id'].nunique()} SKUs\n"
    )

    dropped = len(raw) - len(arcs)
    print(f"  the gate removes {dropped:,} ({dropped / max(len(raw), 1):.0%}), and the reason")
    print("  matters more than the number:")
    transit_fail = int((raw["days_to_expiry"] - (2.0 + raw["km"] / 18.0) / 24.0 <= 0).sum())
    thin_origin = int((raw["units_at_risk"] < MIN_TRANSFER_UNITS).sum())
    thin_dest = int((raw["unmet_units"] < MIN_TRANSFER_UNITS).sum())
    print(f"    transit alone exceeds remaining shelf life   {transit_fail:>6,}")
    print(
        f"    origin holds fewer than {MIN_TRANSFER_UNITS} units at risk        {thin_origin:>6,}"
    )
    print(f"    destination short by fewer than {MIN_TRANSFER_UNITS} units      {thin_dest:>6,}")
    print("    (these overlap, so they sum past the total)")

    if moved.empty:
        print(f"\n  solver: {status}. No transfer clears its own trip cost.")
        print("  That is the recommendation, not a failure to find one.\n")
        return 0

    print(f"\n  solver: {status}")
    print(
        f"  {len(moved)} transfers recommended, "
        f"{moved.groupby(['from_store', 'to_store']).ngroups} vans, "
        f"{int(moved['units'].sum())} units\n"
    )

    print(f"    {'avoided write-off':<28}{moved['avoided_writeoff'].sum():>12,.0f}")
    print(f"    {'recovered margin':<28}{moved['recovered_margin'].sum():>12,.0f}")
    trips = moved.drop_duplicates(["from_store", "to_store"])["trip_cost"].sum()
    print(f"    {'trip cost':<28}{-trips:>12,.0f}")
    print(
        f"    {'net':<28}"
        f"{moved['avoided_writeoff'].sum() + moved['recovered_margin'].sum() - trips:>12,.0f}"
    )

    print("\n  the moves:")
    print(
        f"    {'from':<12}{'to':<12}{'sku':<12}{'units':>7}{'km':>7}"
        f"{'transit':>9}{'slack':>8}{'net':>10}"
    )
    for row in moved.sort_values("net_benefit", ascending=False).itertuples():
        print(
            f"    {row.from_store:<12}{row.to_store:<12}{row.sku_id:<12}"
            f"{row.units:>7}{row.km:>7.1f}{row.transit_hours:>8.1f}h"
            f"{row.sellable_days_after_transit:>7.1f}d{row.net_benefit:>10,.0f}"
        )

    infeasible = moved[moved["sellable_days_after_transit"] <= 0]
    gate = infeasible.empty and status == "Optimal"
    print(
        f"\n  S4.4 gate - no transfer recommended that cannot survive transit: "
        f"{'PASS' if gate else 'FAIL'}"
    )
    if not infeasible.empty:
        print(f"    {len(infeasible)} transfers arrive after expiry")
    print()
    return 0 if gate else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recommend inter-store transfers of at-risk stock."
    )
    parser.add_argument("--warehouse", default=str(WAREHOUSE))
    parser.add_argument("--speed-kmh", type=float, default=DEFAULT_SPEED_KMH)
    parser.add_argument("--handling-hours", type=float, default=DEFAULT_HANDLING_HOURS)
    parser.add_argument("--cost-per-km", type=float, default=DEFAULT_COST_PER_KM)
    parser.add_argument("--fixed-trip-cost", type=float, default=DEFAULT_FIXED_TRIP_COST)
    parser.add_argument("--lookback-days", type=int, default=DEFICIT_LOOKBACK_DAYS)
    args = parser.parse_args()

    warehouse = Path(args.warehouse)
    if not warehouse.exists():
        print(f"\n\033[31mno warehouse at {warehouse}\033[0m\n  run `python tasks.py build`")
        return 1

    con = duckdb.connect(str(warehouse))
    try:
        print("  building the transfer network...", flush=True)
        raw = load_candidates(con, args.lookback_days)
        if raw.empty:
            print("\n  no store holds at-risk stock another store is short of")
            return 0
        arcs = apply_shelf_life_gate(raw, args.speed_kmh, args.handling_hours)
        if arcs.empty:
            print("\n  every candidate transfer fails the shelf-life gate")
            write(con, pd.DataFrame())
            return 0
        moved, status = solve(arcs, args.cost_per_km, args.fixed_trip_cost)
        moved = attribute_trip_cost(moved)
        write(con, moved)
        return report(moved, arcs, raw, status)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
