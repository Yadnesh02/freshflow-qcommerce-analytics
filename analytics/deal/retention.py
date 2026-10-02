"""What the rail buys in retention - and what it costs (task D3).

`reactivation_value` has been a declared parameter defaulting to **zero** since
S4.3. The deal rail's whole justification is that it brings customers back, and
the thing it exists to buy has been valued at nothing because nothing in the
repo could measure it. The store-level holdout could not: it randomises stores,
and retention is a customer-level outcome. D1's holdout can, because it
randomises the customer.

**The headline is negative, and the mechanism is the interesting part.** Exposed
customers churn MORE than held-out ones. Not because the deal is unattractive -
redemption genuinely lowers hazard, the simulator says so at 0.93 per redemption
- but because the rail concentrates demand onto a handful of dealt SKUs which
then run dry. Exposed customers take 1.2% more orders and 16% more
stockout-affected days, and `stockout_on_favourite_sku` is the single largest
term in the churn model. The rail buys redemptions and spends availability, and
in this build the second is worth more than the first.

That chain runs entirely through the simulator's pre-existing hazard logic. None
of it was added for this chapter.

**The ITT is the estimate. There is deliberately no CACE, and the reason is the
most interesting thing in this module.**

The obvious move is a Wald ratio: divide the ITT by the 6.25% redemption rate
and call it the effect on redeemers. The holdout's redemption rate is exactly
zero, so non-compliance is one-sided and the instrument looks ideal. It is not,
because the exclusion restriction fails by construction.

Exposure reaches churn through two channels, not one. Redemption lowers hazard,
which is the channel the Wald ratio assumes is the only one. But exposure also
lifts the whole demand surface on slot days for *every* exposed customer,
redeemer or not - that is what D1 built - and the extra ordering lands on a
handful of dealt SKUs which then run dry. So a customer who never touched the
deal still carries more stockout days because the rail was running near them,
and `stockout_on_favourite_sku` is the largest term in the churn model.

The data agrees: exposed customers who never redeemed carry 1.0094 stockout days
against the holdout's 0.9364. That comparison conditions on a post-treatment
variable and is biased on its own, which is why the argument above rests on the
mechanism rather than on it - but the two point the same way.

Dividing by compliance anyway would have attributed the entire arm-level effect
to the 6.25% who redeemed and reported roughly +17pp, which is both wrong and
wrong in the direction that makes the rail look worse than it is.

**MDE instead.** A null that does not say how small an effect it could have
ruled out is a shrug, not a finding, so the minimum detectable effect is
reported next to every estimate.

    python tasks.py deal-retention
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

# Behavioural retention window. 30 days rather than 90 because the validation
# build is 90 days long and a 90-day window would label everyone retained.
ACTIVE_WINDOW_DAYS = 30

# Conventional 80% power at 5% two-sided, for the minimum detectable effect.
Z_ALPHA, Z_BETA = 1.96, 0.84

OUTCOMES = f"""
-- Everything here is bounded by the assignment date, which the D4 burn-in made
-- necessary. The rail runs for the whole base during the burn-in, so customers
-- later assigned to the holdout DID redeem before the arms existed. Counting
-- those puts the control arm's redemption rate above zero and makes both the
-- compliance figure and the exclusion argument resting on it wrong.
with assign as (select max(assigned_date) as d from staging.stg_crm__deal_exposure),
bounds as (select max(date_day) as last_day from marts.fct_order_item),
redeemed as (
    select distinct i.customer_id
    from marts.fct_order_item i, assign a
    where i.promo_id = '{DEAL_PROMO_ID}' and i.date_day >= a.d
),
recent as (
    select distinct i.customer_id
    from marts.fct_order_item i, bounds b
    where i.date_day > b.last_day - {ACTIVE_WINDOW_DAYS}
),
stockouts as (
    select i.customer_id, count(distinct i.date_day) as stockout_days
    from marts.fct_order_item i
    join marts.agg_store_sku_day s
      on s.store_id = i.store_id and s.sku_id = i.sku_id and s.date_day = i.date_day
    cross join assign a
    where s.is_censored and i.date_day >= a.d
    group by 1
)
select
    e.deal_arm,
    e.customer_id,
    case when r.customer_id is null then 0 else 1 end as redeemed,
    case when c.churn_date is null then 0 else 1 end  as churned,
    case when n.customer_id is null then 0 else 1 end as active_recent,
    coalesce(s.stockout_days, 0)                      as stockout_days
from staging.stg_crm__deal_exposure e
join marts.dim_customer c using (customer_id)
left join redeemed  r on r.customer_id = e.customer_id
left join recent    n on n.customer_id = e.customer_id
left join stockouts s on s.customer_id = e.customer_id
"""


@dataclass(frozen=True)
class Estimate:
    """One outcome, exposed against held out."""

    outcome: str
    exposed: float
    holdout: float
    diff: float
    ci_low: float
    ci_high: float
    n_exposed: int
    n_holdout: int

    @property
    def significant(self) -> bool:
        return (self.ci_low > 0) or (self.ci_high < 0)

    @property
    def mde(self) -> float:
        """Smallest true effect this design would catch 80% of the time."""
        se = (self.ci_high - self.ci_low) / (2 * Z_ALPHA)
        return (Z_ALPHA + Z_BETA) * se


def _compare(frame: pd.DataFrame, column: str) -> Estimate:
    treated = frame.loc[frame["deal_arm"] == "exposed", column]
    control = frame.loc[frame["deal_arm"] == "holdout", column]
    nt, nc = len(treated), len(control)
    mt, mc = float(treated.mean()), float(control.mean())
    se = math.sqrt(treated.var(ddof=1) / nt + control.var(ddof=1) / nc)
    half = Z_ALPHA * se
    return Estimate(column, mt, mc, mt - mc, (mt - mc) - half, (mt - mc) + half, nt, nc)


def outcomes(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    frame = con.execute(OUTCOMES).df()
    arms = set(frame["deal_arm"].unique())
    if arms != {"exposed", "holdout"}:
        raise ValueError(
            "one arm is missing - this build predates the D1 holdout, so retention has no "
            "control group. Re-simulate before reading this."
        )
    return frame


def wald_if_exclusion_held(itt: Estimate, compliance: float) -> float:
    """What a CACE would say here, and must not be reported as one.

    Kept so the number is visible next to the reason it is not usable, rather
    than absent and therefore liable to be recomputed by the next person who
    notices the holdout never redeems. See the module docstring: exposure moves
    churn through availability as well as through redemption, so this ratio
    attributes an arm-wide effect to the 6.25% who took the deal.
    """
    if compliance <= 0:
        raise ValueError("nobody redeemed - the ratio is undefined")
    return itt.diff / compliance


def exclusion_fails(frame: pd.DataFrame) -> tuple[float, float]:
    """Stockout exposure for exposed non-redeemers against the holdout.

    Evidence that exposure has a channel other than redemption. Conditioning on
    non-redemption is conditioning on a post-treatment variable, so this is a
    diagnostic rather than an estimate - the real argument is the mechanism.
    """
    never = frame[(frame["deal_arm"] == "exposed") & (frame["redeemed"] == 0)]
    held = frame[frame["deal_arm"] == "holdout"]
    return float(never["stockout_days"].mean()), float(held["stockout_days"].mean())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE)
    args = ap.parse_args(argv)

    con = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        frame = outcomes(con)
    finally:
        con.close()

    exposed = frame[frame["deal_arm"] == "exposed"]
    held = frame[frame["deal_arm"] == "holdout"]
    compliance = float(exposed["redeemed"].mean())
    contamination = float(held["redeemed"].mean())

    print()
    print(f"  {len(exposed):,} exposed, {len(held):,} held out")
    print(f"  redemption rate   exposed {compliance:>7.2%}   holdout {contamination:>7.2%}")
    if contamination > 0:
        print("  WARNING: the holdout redeemed. The Wald ratio below is not valid.")
    print()

    print(f"    {'outcome':<16} {'exposed':>9} {'holdout':>9} {'diff':>9} {'95% CI':>20}")
    estimates = {}
    for column in ("churned", "active_recent", "stockout_days"):
        e = _compare(frame, column)
        estimates[column] = e
        ci = f"[{e.ci_low:+.4f}, {e.ci_high:+.4f}]"
        flag = "" if e.significant else "   n.s."
        print(
            f"    {column:<16} {e.exposed:>9.4f} {e.holdout:>9.4f} {e.diff:>+9.4f} {ci:>20}{flag}"
        )

    churn = estimates["churned"]
    print()
    print(f"    ITT on churn           {churn.diff:>+8.4f}  ({churn.diff * 100:+.2f} pp)")
    print(f"    minimum detectable     {churn.mde:>8.4f}  ({churn.mde * 100:.2f} pp at 80% power)")

    never_so, held_so = exclusion_fails(frame)
    print()
    print("    No CACE is reported. Exposure moves churn through availability as well")
    print("    as through redemption, so the exclusion restriction fails by construction.")
    print(f"      exposed non-redeemers carry {never_so:.4f} stockout days")
    print(f"      the holdout carries         {held_so:.4f}")
    print(
        f"      a Wald ratio would have said {wald_if_exclusion_held(churn, compliance) * 100:+.2f}"
        f" pp, attributing an arm-wide"
    )
    print(f"      effect to the {compliance:.2%} who redeemed.")

    print()
    if churn.diff > 0 and churn.significant:
        print("    The rail costs retention rather than buying it. The mechanism is")
        print("    availability: exposed customers take more stockout-affected days, and")
        print("    stockout_on_favourite_sku is the largest term in the churn model.")
        print("    reactivation_value should not be set positive on this evidence.")
    elif churn.significant:
        print("    The rail buys retention. reactivation_value can be set from this.")
    else:
        print(f"    No detectable effect either way, within +/-{churn.mde * 100:.2f} pp.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
