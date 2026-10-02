"""What the rail earns back, measured against its own control group (task D2).

The plan wrote this chapter as propensity weighting, because when it was written
the only comparison available was deal-takers against non-takers and those two
groups differ in every way that matters. D1 changed the problem: there is now a
randomised customer-level holdout, so the causal estimate is a difference
between arms and propensity weighting is no longer the best tool in the room.
It is kept here as the *naive* comparison, which is the thing the headline
number has to be stated against.

**Both numbers, side by side, deliberately.** `naive_attach` reproduces the
method behind the +Rs 6.27 that `deal_slots.py` uses: rest-of-basket margin on
orders that took the deal, against orders that did not. `itt_effects` compares
the arms the holdout created. The gap between them is not a footnote - it is
the finding, and page 1 of the dashboard is built on it.

**Intent to treat, which means non-orderers count as zeros.** The arms are
compared over every assigned customer, including the ones who never ordered.
Dropping them would condition on an outcome the rail is supposed to move -
whether somebody ordered at all - and would quietly turn a clean experiment back
into the self-selected comparison it exists to replace. This is why the per
customer frame is a LEFT join from the assignment and not an inner join from
orders.

**Why Welch rather than a bootstrap.** The estimand is a difference of means on
tens of thousands of customers per arm, where the CLT does the work even though
per-customer spend is badly skewed. Welch also drops the equal-variance
assumption, which matters here because the treated arm's variance is inflated by
the very effect being measured. A bootstrap would agree and cost a thousand
times the compute.

    python tasks.py deal-attach
"""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

DEAL_PROMO_ID = "PROMO-DEAL11"

# The figure this chapter exists to replace. Quoted so the comparison is in the
# output rather than in a reader's memory.
NAIVE_ATTACH_IN_USE = 6.27

PER_CUSTOMER = f"""
with per_cust as (
    select
        customer_id,
        count(distinct order_id)                                      as orders,
        sum(net_revenue)                                              as revenue,
        sum(gross_margin)                                             as margin,
        sum(case when promo_id = '{DEAL_PROMO_ID}'
                 then gross_margin else 0 end)                        as deal_margin,
        sum(case when promo_id is distinct from '{DEAL_PROMO_ID}'
                 then gross_margin else 0 end)                        as nondeal_margin,
        sum(case when promo_id = '{DEAL_PROMO_ID}'
                 then signed_qty else 0 end)                          as deal_units
    from marts.fct_order_item
    group by 1
)
select
    e.deal_arm,
    coalesce(p.orders, 0)          as orders,
    coalesce(p.revenue, 0.0)       as revenue,
    coalesce(p.margin, 0.0)        as margin,
    coalesce(p.deal_margin, 0.0)   as deal_margin,
    coalesce(p.nondeal_margin, 0.0) as nondeal_margin,
    coalesce(p.deal_units, 0)      as deal_units
from staging.stg_crm__deal_exposure e
left join per_cust p using (customer_id)
"""

# The self-selected comparison, reproduced rather than paraphrased: margin on
# the REST of the basket, orders that took the deal against orders that did not,
# within the same store-day so the mix of stores and days cannot drive it.
NAIVE = f"""
with order_level as (
    select
        order_id,
        store_id,
        date_day,
        max(case when promo_id = '{DEAL_PROMO_ID}' then 1 else 0 end) as took_deal,
        sum(case when promo_id is distinct from '{DEAL_PROMO_ID}'
                 then gross_margin else 0 end)                        as rest_of_basket
    from marts.fct_order_item
    group by 1, 2, 3
),
store_day as (
    select
        store_id, date_day,
        avg(case when took_deal = 1 then rest_of_basket end) as dealt,
        avg(case when took_deal = 0 then rest_of_basket end) as undealt
    from order_level
    group by 1, 2
)
select
    avg(dealt)            as dealt_mean,
    avg(undealt)          as undealt_mean,
    avg(dealt - undealt)  as naive_attach,
    count(*)              as store_days
from store_day
where dealt is not null and undealt is not null
"""


@dataclass(frozen=True)
class Effect:
    """One outcome, compared across the two arms."""

    metric: str
    exposed_mean: float
    holdout_mean: float
    diff: float
    ci_low: float
    ci_high: float
    n_exposed: int
    n_holdout: int

    @property
    def significant(self) -> bool:
        """The interval excludes zero."""
        return (self.ci_low > 0) or (self.ci_high < 0)

    @property
    def relative(self) -> float:
        return self.diff / self.holdout_mean if self.holdout_mean else float("nan")


def _welch(treated: pd.Series, control: pd.Series, metric: str) -> Effect:
    """Difference of means with a 95% Welch interval."""
    nt, nc = len(treated), len(control)
    mt, mc = float(treated.mean()), float(control.mean())
    vt, vc = float(treated.var(ddof=1)), float(control.var(ddof=1))
    se = math.sqrt(vt / nt + vc / nc)
    # 1.96 rather than a t quantile: both arms run to tens of thousands, where
    # the t and the normal agree to three decimal places.
    half = 1.96 * se
    return Effect(
        metric=metric,
        exposed_mean=mt,
        holdout_mean=mc,
        diff=mt - mc,
        ci_low=(mt - mc) - half,
        ci_high=(mt - mc) + half,
        n_exposed=nt,
        n_holdout=nc,
    )


def per_customer(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """One row per assigned customer, zeros included."""
    return con.execute(PER_CUSTOMER).df()


def itt_effects(frame: pd.DataFrame) -> list[Effect]:
    """The causal estimates: every assigned customer, whether they ordered or not."""
    treated = frame[frame["deal_arm"] == "exposed"]
    control = frame[frame["deal_arm"] == "holdout"]
    if treated.empty or control.empty:
        raise ValueError(
            "one arm is empty - this build predates the D1 holdout, so there is no "
            "control group to measure against. Re-simulate before reading this."
        )
    return [
        _welch(treated[c], control[c], c)
        for c in ("orders", "revenue", "margin", "nondeal_margin", "deal_margin")
    ]


def naive_attach(con: duckdb.DuckDBPyConnection) -> dict[str, float]:
    """The self-selected comparison, for contrast rather than for use."""
    row = con.execute(NAIVE).fetchone()
    return {
        "dealt_mean": float(row[0]),
        "undealt_mean": float(row[1]),
        "naive_attach": float(row[2]),
        "store_days": int(row[3]),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE)
    args = ap.parse_args(argv)

    con = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        naive = naive_attach(con)
        effects = itt_effects(per_customer(con))
    finally:
        con.close()

    print("\n  NAIVE - orders that took the deal against orders that did not")
    print(f"    rest of basket, dealt      {naive['dealt_mean']:>10.2f}")
    print(f"    rest of basket, undealt    {naive['undealt_mean']:>10.2f}")
    print(f"    difference                 {naive['naive_attach']:>10.2f}")
    print(f"    over {naive['store_days']:,} store-days. Self-selected: an upper bound.")
    print(f"    (the figure deal_slots.py uses is {NAIVE_ATTACH_IN_USE:.2f})")

    print("\n  CAUSAL - exposed arm against the randomised holdout, per customer")
    print(f"    {'metric':<18} {'exposed':>10} {'holdout':>10} {'diff':>10} {'95% CI':>22}")
    for e in effects:
        star = "" if e.significant else "   n.s."
        ci = f"[{e.ci_low:,.2f}, {e.ci_high:,.2f}]"
        print(
            f"    {e.metric:<18} {e.exposed_mean:>10,.2f} {e.holdout_mean:>10,.2f} "
            f"{e.diff:>10,.2f} {ci:>22}{star}"
        )
    print(f"\n    n = {effects[0].n_exposed:,} exposed, {effects[0].n_holdout:,} held out.")
    print("    Intent to treat: customers who never ordered are in, as zeros.")
    print("    Per ASSIGNED CUSTOMER - incidence plus attach. Not comparable to the")
    print("    naive figure above, which is per order and attach only.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
