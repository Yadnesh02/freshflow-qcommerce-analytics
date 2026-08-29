"""Perishable newsvendor replenishment: how much to order, per store-SKU (S4.5).

Problem P4 is that ordering causes the problem the rest of Sprint 4 spends its
time cleaning up. A static reorder point ignores day of week, monsoon,
festivals, salary week and - the one that matters here - shelf life. You cannot
mark your way out of a purchase order that was too big for the shelf life of
what it bought.

**The critical ratio is where shelf life enters, and it is the whole task.**
The textbook newsvendor sets a service level from `CR = Cu / (Cu + Co)`, where
`Cu` is the cost of being one unit short and `Co` the cost of being one unit
long. For a durable good `Co` is a few days of holding cost, `CR` sits close to
one, and the answer is "order plenty". For a perishable, a leftover unit is not
carried - it is thrown away - so `Co` is the landed cost times the probability
that unit spoils, which for two-day curd is most of it. The same formula that
says "order plenty" for detergent says "order tightly" for paneer, and it is the
shelf life that separates them:

    Cu = base_price - landed_cost  (+ a declared retention term, default 0)
    Co = landed_cost x P(spoil)
    CR = Cu / (Cu + Co)
    order_up_to = F_W^-1(CR)  over the protection window W = lead time + review

**P(spoil) depends on the order quantity, which depends on P(spoil).** A unit
spoils if demand over its usable life never reaches it, so `P(spoil) = P(D_U <
order_up_to)` - and `order_up_to` is what is being solved for. That circularity
is real rather than a modelling convenience, and it is resolved by iterating the
pair to a fixed point, which converges in a handful of passes because both
directions are monotone. Computing P(spoil) once at an arbitrary starting
quantity would bias every perishable in the same direction.

**The cap is the gate, and it binds where it should.** Even at the service level
the critical ratio asks for, an order larger than what can plausibly sell before
expiry is guaranteed waste. So the order-up-to level is capped at a high
quantile of demand over the *usable* window - the shorter of the shelf life at
receipt and the review cycle - rather than over the protection window. For a
230-day SKU the cap is far above the newsvendor level and never binds; for the
292 SKUs at seven days or less it is what actually determines the order. The
gate asks that the cap be real, and `test_the_shelf_life_cap_actually_binds`
asks the sharper question of whether it ever changes an answer.

**Demand is negative binomial, with the dispersion the forecast actually
exhibits.** Reusing `expiry_risk.fit_dispersion` rather than refitting: it
solves `Var = mu + mu^2 / k` on the pooled backtest residuals, so the spread
comes from this model's own errors. Poisson would fix `Var = mu`, understate the
spread, and quietly under-order every SKU whose demand is lumpy - which is most
perishables.

**Lead time is a distribution, not a number.** Purchase orders in this dataset
arrive in 2.57 days on average with a standard deviation of 1.17 and a tail to
14. The protection window uses a high quantile rather than the mean, because a
service level computed against average lead time is not the service level it
claims to be on the days the van is late.

    python tasks.py newsvendor
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy import stats

from analytics.expiry_risk import fit_dispersion

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

TARGET_TABLE = "marts.rec_purchase_order"

# Days between replenishment reviews. Daily ordering in q-commerce.
REVIEW_PERIOD_DAYS = 1

# Lead time is a distribution - 2.57 days mean, 1.17 sd, tail to 14 - so the
# protection window is taken at a quantile rather than at the mean. A service
# level computed against average lead time is not the service level it claims
# on the days the van is late.
LEAD_TIME_QUANTILE = 0.90

# The order-up-to level is capped at this quantile of demand over the usable
# shelf life. Above it, the order is buying units that cannot sell in time.
SHELF_LIFE_CAP_QUANTILE = 0.95

# AN ASSUMPTION, defaulting to zero. The plan's Cu includes "retention damage" -
# what a stockout costs beyond the lost margin, through customers who do not
# come back. mart_customer_360 has a churn hazard but no causal link from a
# single stockout to lifetime value, so this dataset cannot supply the number.
# Declared here and swept by S5.3 with the other four.
DEFAULT_STOCKOUT_RETENTION_COST = 0.0

# Below this the store-SKU is not worth an order line.
MIN_ORDER_UNITS = 1


CANDIDATE_SQL = """
with as_of as (

    select max(origin_date) as origin_date from marts.mart_demand_forecast

),

-- forecast demand per day over the horizon we can see
horizon as (

    select
        forecast.store_id,
        forecast.sku_id,
        avg(forecast.forecast_units) as daily_forecast,
        count(*) as horizon_days
    from marts.mart_demand_forecast as forecast
    cross join as_of
    where forecast.origin_date = as_of.origin_date
    group by forecast.store_id, forecast.sku_id
    having avg(forecast.forecast_units) > 0

),

-- what is already on the shelf, and how fresh it is
position as (

    select
        risk.store_id,
        risk.sku_id,
        sum(risk.qty_remaining) as on_hand_units,
        min(risk.usable_days) as usable_days_on_hand
    from marts.mart_expiry_risk as risk
    where risk.risk_state <> 'expired'
    group by risk.store_id, risk.sku_id

),

-- observed lead time per supplier, which is what a buyer actually faces
lead_times as (

    select
        purchase.store_id,
        purchase.sku_id,
        avg(purchase.lead_time_days) as mean_lead_days,
        quantile_cont(purchase.lead_time_days, {lead_quantile}) as lead_days
    from staging.stg_wms__purchase_orders as purchase
    where purchase.lead_time_days is not null
    group by purchase.store_id, purchase.sku_id

)

select
    as_of.origin_date as date_day,
    horizon.store_id,
    horizon.sku_id,
    products.sku_name,
    products.l1_category,
    products.shelf_life_days,
    products.base_price,
    products.landed_cost,
    horizon.daily_forecast,
    coalesce(position.on_hand_units, 0) as on_hand_units,
    coalesce(lead_times.lead_days, 3) as lead_days,
    coalesce(lead_times.mean_lead_days, 2.57) as mean_lead_days
from horizon
cross join as_of
inner join marts.dim_product as products
    on products.sku_id = horizon.sku_id
left join position
    on position.store_id = horizon.store_id and position.sku_id = horizon.sku_id
left join lead_times
    on lead_times.store_id = horizon.store_id and lead_times.sku_id = horizon.sku_id
where products.landed_cost > 0 and products.base_price > products.landed_cost
"""


def load_candidates(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    con.execute("set memory_limit = '4GB'")
    return con.execute(CANDIDATE_SQL.format(lead_quantile=LEAD_TIME_QUANTILE)).df()


def negbin_quantile(mean: np.ndarray, k: float, q: float) -> np.ndarray:
    """Inverse CDF of a negative binomial with the given mean and dispersion.

    Parameterised so Var = mu + mu^2 / k, matching `expiry_risk.fit_dispersion`.
    """
    mean = np.maximum(np.asarray(mean, dtype=float), 1e-9)
    p = k / (k + mean)
    return stats.nbinom.ppf(q, k, p)


def negbin_cdf(x: np.ndarray, mean: np.ndarray, k: float) -> np.ndarray:
    """P(D <= x). Used for the spoilage probability at a candidate quantity."""
    mean = np.maximum(np.asarray(mean, dtype=float), 1e-9)
    p = k / (k + mean)
    return stats.nbinom.cdf(x, k, p)


def solve_order_up_to(
    candidates: pd.DataFrame,
    dispersion: float,
    retention_cost: float,
    iterations: int = 12,
) -> pd.DataFrame:
    """Critical ratio and order-up-to level, iterated to a fixed point.

    P(spoil) sets the overage cost, the overage cost sets the critical ratio,
    the critical ratio sets the order-up-to level, and the order-up-to level
    sets P(spoil). The loop is monotone in both directions, so it settles in a
    handful of passes; evaluating P(spoil) once at an arbitrary starting
    quantity would bias every perishable the same way.
    """
    out = candidates.copy()

    # protection window: what has to be covered between now and the next
    # delivery arriving. Lead time at a high quantile, not at the mean.
    out["protection_days"] = out["lead_days"] + REVIEW_PERIOD_DAYS
    out["demand_protection"] = out["daily_forecast"] * out["protection_days"]

    # usable window: how long a unit received today can still be sold
    out["usable_days"] = out["shelf_life_days"].clip(lower=1)
    out["demand_usable"] = out["daily_forecast"] * out["usable_days"]

    out["underage_cost"] = (out["base_price"] - out["landed_cost"]) + retention_cost

    # the most that could plausibly clear before expiry. Above this an order is
    # buying units that cannot sell in time, whatever the service level says.
    out["shelf_life_cap"] = negbin_quantile(
        out["demand_usable"].to_numpy(), dispersion, SHELF_LIFE_CAP_QUANTILE
    )

    spoil = np.full(len(out), 0.5)
    order_up_to = np.zeros(len(out))
    critical_ratio = np.zeros(len(out))

    for _ in range(iterations):
        overage = out["landed_cost"].to_numpy() * spoil
        # Clamped, and the clamped value is the one stored. Over a four-day
        # protection window a 1,440-day SKU has a spoilage probability far below
        # float resolution, so Co underflows and the ratio evaluates to exactly
        # 1.0 - a service level whose inverse CDF is infinite. Storing the raw
        # 1.0 while quietly passing a clipped value to the quantile would leave
        # rec_purchase_order reporting a number the model never used.
        critical_ratio = np.clip(
            out["underage_cost"].to_numpy() / (out["underage_cost"].to_numpy() + overage),
            1e-6,
            1.0 - 1e-9,
        )
        uncapped = negbin_quantile(out["demand_protection"].to_numpy(), dispersion, critical_ratio)
        order_up_to = np.minimum(uncapped, out["shelf_life_cap"].to_numpy())
        # a unit spoils when demand over its usable life never reaches it
        spoil = negbin_cdf(order_up_to - 1, out["demand_usable"].to_numpy(), dispersion)

    out["uncapped_order_up_to"] = negbin_quantile(
        out["demand_protection"].to_numpy(), dispersion, critical_ratio
    )
    out["p_spoil"] = spoil
    out["overage_cost"] = out["landed_cost"] * out["p_spoil"]
    out["critical_ratio"] = critical_ratio
    out["order_up_to"] = order_up_to
    out["is_shelf_life_capped"] = out["order_up_to"] < out["uncapped_order_up_to"] - 1e-9
    out["order_units"] = (out["order_up_to"] - out["on_hand_units"]).clip(lower=0).round()

    return out[out["order_units"] >= MIN_ORDER_UNITS].reset_index(drop=True)


def write(con: duckdb.DuckDBPyConnection, orders: pd.DataFrame) -> None:
    keep = [
        "date_day",
        "store_id",
        "sku_id",
        "sku_name",
        "l1_category",
        "shelf_life_days",
        "base_price",
        "landed_cost",
        "daily_forecast",
        "on_hand_units",
        "lead_days",
        "protection_days",
        "usable_days",
        "underage_cost",
        "overage_cost",
        "p_spoil",
        "critical_ratio",
        "uncapped_order_up_to",
        "shelf_life_cap",
        "order_up_to",
        "is_shelf_life_capped",
        "order_units",
    ]
    con.register("_newsvendor", orders[keep])
    con.execute(f"create or replace table {TARGET_TABLE} as select * from _newsvendor")
    con.unregister("_newsvendor")


def report(orders: pd.DataFrame, dispersion: float) -> int:
    print(f"\n  {len(orders):,} order lines, negative-binomial dispersion k={dispersion:.3f}\n")

    capped = orders[orders["is_shelf_life_capped"]]
    print(
        f"  {len(capped):,} of {len(orders):,} ({len(capped) / max(len(orders), 1):.0%}) "
        f"are capped by shelf life rather than by the critical ratio"
    )

    print("\n  critical ratio and cap, by shelf life:")
    bands = pd.cut(
        orders["shelf_life_days"],
        [0, 2, 7, 30, 400, 100000],
        labels=["0-2d", "3-7d", "8-30d", "1-13mo", "13mo+"],
    )
    grouped = orders.groupby(bands, observed=True).agg(
        lines=("sku_id", "size"),
        critical_ratio=("critical_ratio", "mean"),
        p_spoil=("p_spoil", "mean"),
        capped=("is_shelf_life_capped", "mean"),
        order_units=("order_units", "sum"),
    )
    print(f"    {'shelf life':<12}{'lines':>8}{'CR':>8}{'P(spoil)':>11}{'capped':>9}{'units':>10}")
    for band, row in grouped.iterrows():
        print(
            f"    {str(band):<12}{row.lines:>8,.0f}{row.critical_ratio:>8.2f}"
            f"{row.p_spoil:>11.2f}{row.capped:>9.0%}{row.order_units:>10,.0f}"
        )

    print("\n  The critical ratio falls as shelf life shortens, which is the whole")
    print("  point: a leftover unit of curd is thrown away, a leftover unit of")
    print("  detergent is just early. The same formula gives both answers.")

    breached = orders[orders["order_up_to"] > orders["shelf_life_cap"] + 1e-6]
    gate = breached.empty and not capped.empty
    print(f"\n  S4.5 gate - order-up-to capped by shelf life: {'PASS' if gate else 'FAIL'}")
    if not breached.empty:
        print(f"    {len(breached)} order lines exceed their shelf-life cap")
    if capped.empty:
        print("    the cap never binds, so it is untested - check the cap quantile")
    print()
    return 0 if gate else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Perishable newsvendor replenishment.")
    parser.add_argument("--warehouse", default=str(WAREHOUSE))
    parser.add_argument(
        "--retention-cost",
        type=float,
        default=DEFAULT_STOCKOUT_RETENTION_COST,
        help=(
            "AN ASSUMPTION. Rupees of retention damage per unit short, beyond the "
            "lost margin. This dataset cannot supply it; S5.3 sweeps it"
        ),
    )
    args = parser.parse_args()

    warehouse = Path(args.warehouse)
    if not warehouse.exists():
        print(f"\n\033[31mno warehouse at {warehouse}\033[0m\n  run `python tasks.py build`")
        return 1

    con = duckdb.connect(str(warehouse))
    try:
        print("  fitting demand dispersion from the backtest residuals...", flush=True)
        dispersion = fit_dispersion(con)
        print("  building the order book...", flush=True)
        candidates = load_candidates(con)
        if candidates.empty:
            print("\n  no store-SKU has a positive forecast to order against")
            return 1
        orders = solve_order_up_to(candidates, dispersion, args.retention_cost)
        write(con, orders)
        return report(orders, dispersion)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
