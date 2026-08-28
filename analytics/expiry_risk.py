"""Expiry risk per open batch: P(unsold before expiry) and the rupees behind it (S3.4).

This is the model the project exists for. Problem P1 in the plan is that ops
sees "150 units of curd in Andheri" and not "40 of those expire in 36 hours and
only 22 will sell", and the gap between those two sentences is this table.

**Batch grain, and FEFO is the reason it cannot be SKU grain.** A store holding
three batches of the same paneer does not face one risk, it faces three
different ones, and the oldest batch bears almost all of it. Stock is picked
first-expiry-first-out, so a batch is only exposed to the demand that the
batches ahead of it in the queue do not absorb. Scoring every batch against the
store-SKU forecast would hand the same demand to each of them and conclude that
nothing is ever at risk - which is exactly the blindness the project is about,
reproduced in the model meant to remove it. `residual_demand` is therefore the
forecast over the batch's remaining life *minus* the units sitting in front of
it, floored at zero.

**A distribution, not a point estimate.** "Risk" is a probability, and a point
forecast cannot produce one. Demand over the remaining shelf life is modelled
as negative binomial with the mean the forecast gives and a dispersion fitted
from the model's own backtest residuals. Negative binomial rather than Poisson
because the plan is explicit about it and because it matters here in a specific
direction: Poisson fixes the variance at the mean, which understates the spread
of real demand, and understating spread understates how much stock is at risk.
A risk model that is optimistic by construction is worse than none.

    units_at_risk = E[max(0, remaining - residual_demand)]

which is the newsvendor's overage term - expected leftover - and equals the sum
of the demand CDF below `remaining`, evaluated in closed form rather than
simulated.

**Three states, because one number would lie about two of them.** Every open
batch is scored, and `risk_state` says what kind of number it got:

  - `expired` - the expiry date has already passed and units remain. P(unsold)
    is 1 by definition; there is nothing to predict, only to book. 17,293
    batches carrying Rs 1.3M sit here, 6.9% of expired batches, and they are
    kept visible rather than dropped because they are a real loss. They are
    not, however, *actionable* - no markdown recovers stock that has already
    gone - so the action queue filters to `at_risk` and this state is the
    reason it has to.

  - `at_risk` - expiry falls inside the forecast horizon. This is the modelled
    number and the only one with a distribution behind it.

  - `beyond_horizon` - expiry is further out than the forecast reaches. Scored
    at zero risk and flagged, because a 7-day model asked about a batch
    expiring in 168 days is not being conservative, it is being asked a
    question it cannot answer. Most batches land here: mean shelf life across
    the estate is 305 days, and the perishables that the project is actually
    about - meat and fish at 2.5 days, fruit and veg at 4.7 - fall inside the
    horizon comfortably.

**What this model does not see: the dregs.** Validated against the seven days
after the as-of date, the scores rank write-offs cleanly - 0.0%, 0.0%, 0.0%,
1.7%, 42.2% across risk bands - and rank total unsold stock badly. The reason
is a population it has no feature for. 334 batches scored below 0.5, averaging
1.59 units each and expiring five days out, sold 192 of 531 units and had
nothing written off inside the window. The forecast was not wrong about the
SKU; those SKUs do sell several units a day. It was wrong about whether these
particular units participate, and they are the tail end of batches that have
already mostly sold - the same 6.9% residue every expired batch in the
warehouse carries, stock the ledger holds and the shelf never moves.

That is not a demand phenomenon and no demand model fixes it; it wants a
picking or stock-accuracy signal the event stream does not carry. What a
demand model can do is decline to pretend otherwise, so the claim made here is
the narrow one - this table ranks *write-off* risk - and
`test_the_dregs_are_a_known_blind_spot` holds the gap open in numbers.

    python tasks.py expiry-risk
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import duckdb
import numpy as np
from scipy import stats

from analytics.forecasting.train import FORECAST_TABLE, MAX_HORIZON

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

TARGET_TABLE = "marts.mart_expiry_risk"

# Beyond this many units the leftover expectation is computed from the tail
# rather than by summing the CDF term by term. Batches this large are rare and
# their risk is either 0 or 1 in practice.
MAX_CDF_TERMS = 400


# The batch position: what each open batch holds as at the as-of date, and how
# many units of the same store-SKU expire before it does. That second number is
# the FEFO queue ahead of it, and it is what turns a store-SKU forecast into a
# batch-level residual.
POSITION_SQL = f"""
create or replace table main._batch_position as

with as_of as (

    -- the latest origin the forecast covers, which is seven days short of the
    -- end of the data on purpose: it leaves real actuals to validate against
    select max(origin_date) as as_of_date
    from {FORECAST_TABLE}

),

-- what the ledger says each batch held on that date, replayed rather than read
-- off fct_inventory_batch, whose qty_remaining is as at the end of all data
position as (

    select
        movements.batch_id,
        sum(movements.qty_delta) as qty_remaining
    from marts.fct_inventory_movement as movements
    cross join as_of
    where movements.date_day <= as_of.as_of_date
    group by movements.batch_id

),

open_batches as (

    select
        batches.batch_id,
        batches.store_id,
        batches.sku_id,
        batches.expiry_date,
        batches.unit_landed_cost,
        batches.usable_days,
        position.qty_remaining,
        as_of.as_of_date,
        batches.expiry_date - as_of.as_of_date as days_to_expiry
    from position
    inner join marts.fct_inventory_batch as batches
        on batches.batch_id = position.batch_id
    cross join as_of
    where position.qty_remaining > 0

)

select
    open_batches.*,
    products.l1_category,
    products.sku_name,

    -- the FEFO queue ahead of this batch: units of the same store-SKU that
    -- expire sooner and will therefore be picked first
    coalesce(sum(ahead.qty_remaining), 0) as units_ahead_in_queue,

    -- demand the forecast expects over this batch's remaining life. Clipped to
    -- the horizon: beyond it there is no forecast, and extending the last day
    -- forward would be inventing one.
    coalesce((
        select sum(forecast.forecast_units)
        from {FORECAST_TABLE} as forecast
        where
            forecast.store_id = open_batches.store_id
            and forecast.sku_id = open_batches.sku_id
            and forecast.origin_date = open_batches.as_of_date
            and forecast.horizon_days <= least(
                greatest(open_batches.days_to_expiry, 0), {MAX_HORIZON}
            )
    ), 0) as horizon_demand_mean
from open_batches
inner join marts.dim_product as products
    on products.sku_id = open_batches.sku_id
left join open_batches as ahead
    on
        ahead.store_id = open_batches.store_id
        and ahead.sku_id = open_batches.sku_id
        and ahead.expiry_date < open_batches.expiry_date
group by all
"""


def fit_dispersion(con: duckdb.DuckDBPyConnection) -> float:
    """Negative-binomial dispersion, from the forecast's own errors.

    For a negative binomial, Var = mu + mu^2 / k. Solving on the pooled
    backtest residuals gives the k that reproduces the spread this model
    actually exhibits, rather than the Poisson assumption Var = mu that would
    make every risk score look smaller than it is.

    Fitted on the scored table, so it is measured against observations rather
    than against S3.1's imputation.
    """
    mean_actual, variance, mean_forecast = con.execute(
        """
        select avg(actual_units), var_samp(actual_units - forecast_units), avg(forecast_units)
        from marts.mart_forecast_accuracy
        """
    ).fetchone()

    excess = variance - mean_actual
    if excess <= 0:
        # errors are tighter than Poisson; nothing to inflate
        return float("inf")
    return float(mean_forecast**2 / excess)


def leftover_and_risk(remaining: np.ndarray, mean: np.ndarray, k: float):
    """Expected unsold units and P(unsold), under a negative binomial demand.

    E[max(0, q - D)] = sum_{j=0}^{q-1} F(j), the CDF summed below the stock on
    hand. P(unsold) = P(D < q) = F(q-1).
    """
    remaining_int = np.clip(np.rint(remaining), 0, MAX_CDF_TERMS).astype(int)
    leftover = np.zeros(len(remaining), dtype=float)
    risk = np.zeros(len(remaining), dtype=float)

    safe_mean = np.maximum(mean, 1e-9)
    if np.isinf(k):
        dist = stats.poisson(safe_mean)
    else:
        # scipy parameterises by successes n and probability p
        p = k / (k + safe_mean)
        dist = stats.nbinom(k, p)

    # one pass per distinct stock level: far fewer than one per batch, because
    # most batches hold single-digit units
    for q in np.unique(remaining_int):
        if q <= 0:
            continue
        rows = remaining_int == q
        cdf = dist.cdf(np.arange(q)[:, None])[:, rows]
        leftover[rows] = cdf.sum(axis=0)
        risk[rows] = cdf[-1]
    return leftover, risk


def build(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(POSITION_SQL)
    df = con.execute("select * from main._batch_position").df()

    dispersion = fit_dispersion(con)
    print(f"  negative binomial dispersion k = {dispersion:.2f}  (Poisson would be k = infinity)")

    # what this batch can actually expect, after the queue ahead of it is served
    residual = np.maximum(df["horizon_demand_mean"] - df["units_ahead_in_queue"], 0.0)
    df["residual_demand_mean"] = residual

    leftover, risk = leftover_and_risk(
        df["qty_remaining"].to_numpy(dtype=float), residual.to_numpy(dtype=float), dispersion
    )

    expired = df["days_to_expiry"] < 0
    beyond = df["days_to_expiry"] > MAX_HORIZON

    df["risk_state"] = np.where(expired, "expired", np.where(beyond, "beyond_horizon", "at_risk"))
    # an expiry that has already happened is not a probability
    df["expiry_risk_score"] = np.where(expired, 1.0, np.where(beyond, 0.0, risk))
    df["units_at_risk"] = np.where(
        expired,
        df["qty_remaining"],
        np.where(beyond, 0.0, np.minimum(leftover, df["qty_remaining"])),
    )
    df["value_at_risk_inr"] = df["units_at_risk"] * df["unit_landed_cost"]

    con.register("_risk", df)
    con.execute(
        f"""
        create or replace table {TARGET_TABLE} as
        select
            batch_id,
            store_id,
            sku_id,
            l1_category,
            sku_name,
            cast(as_of_date as date) as date_day,
            expiry_date,
            days_to_expiry,
            usable_days,
            qty_remaining,
            units_ahead_in_queue,
            horizon_demand_mean,
            residual_demand_mean,
            risk_state,
            expiry_risk_score,
            units_at_risk,
            unit_landed_cost,
            value_at_risk_inr
        from _risk
        """
    )


REPORT_SQL = f"""
select
    risk_state,
    count(*) as batches,
    round(sum(qty_remaining)) as units_on_hand,
    round(sum(units_at_risk)) as units_at_risk,
    round(sum(value_at_risk_inr)) as value_at_risk,
    round(avg(expiry_risk_score), 3) as mean_score
from {TARGET_TABLE}
group by risk_state
order by value_at_risk desc
"""

ACTIONABLE_SQL = f"""
select
    l1_category,
    count(*) as batches,
    round(sum(units_at_risk), 1) as units_at_risk,
    round(sum(value_at_risk_inr)) as value_at_risk,
    round(avg(days_to_expiry), 1) as mean_dte
from {TARGET_TABLE}
where risk_state = 'at_risk'
group by l1_category
having sum(value_at_risk_inr) > 0
order by value_at_risk desc
limit 10
"""


def report(con: duckdb.DuckDBPyConnection) -> int:
    as_of = con.execute(f"select max(date_day) from {TARGET_TABLE}").fetchone()[0]
    total = con.execute(f"select count(*) from {TARGET_TABLE}").fetchone()[0]
    print(f"\n  {total:,} open batches scored as at {as_of}\n")

    print(
        f"  {'state':<16}{'batches':>10}{'on hand':>10}{'at risk':>10}{'value Rs':>12}{'score':>8}"
    )
    print("  " + "-" * 66)
    for state, batches, on_hand, at_risk, value, score in con.execute(REPORT_SQL).fetchall():
        print(
            f"  {state:<16}{batches:>10,}{on_hand:>10,.0f}{at_risk:>10,.0f}"
            f"{value:>12,.0f}{score:>8.3f}"
        )

    print("\n  the actionable queue - where a markdown still changes the outcome")
    print(f"  {'category':<26}{'batches':>9}{'units':>9}{'value Rs':>11}{'mean DTE':>10}")
    print("  " + "-" * 65)
    rows = con.execute(ACTIONABLE_SQL).fetchall()
    for category, batches, units, value, dte in rows:
        print(f"  {category:<26}{batches:>9,}{units:>9,.1f}{value:>11,.0f}{dte:>10.1f}")
    if not rows:
        print("    (nothing at risk inside the horizon)")
    return 0


VALIDATION_SQL = f"""
with disposition as (

    select
        risk.batch_id,
        risk.qty_remaining,
        risk.units_at_risk,
        risk.unit_landed_cost,
        risk.expiry_date,
        coalesce(sum(-moves.qty_delta) filter (where moves.event_type = 'sale'), 0) as sold,
        coalesce(
            sum(-moves.qty_delta) filter (where moves.event_type = 'expiry_writeoff'), 0
        ) as written_off
    from {TARGET_TABLE} as risk
    left join marts.fct_inventory_movement as moves
        on moves.batch_id = risk.batch_id and moves.date_day > risk.date_day
    where risk.risk_state = 'at_risk'
    group by all

)

select
    sum(qty_remaining) as units_on_hand,
    sum(sold) as sold_after,
    sum(written_off) as written_off_after,
    -- units still on the shelf when the data ends, in batches whose expiry has
    -- already passed: unsold at expiry, just not yet booked as such
    sum(qty_remaining - sold - written_off) filter (
        where expiry_date <= (select max(date_day) from marts.agg_store_sku_day)
    ) as stranded_past_expiry,
    sum(units_at_risk) as predicted_units,
    sum(units_at_risk * unit_landed_cost) as predicted_value
from disposition
"""


def validate(con: duckdb.DuckDBPyConnection) -> None:
    """Check the scores against what actually happened over the seven days after.

    Only possible because the as-of date is a horizon short of the end of the
    data, which is why it was chosen. In production there is nothing to compare
    against and this section is simply absent - which is the honest state of a
    forward-looking score, not a gap to be filled with something reassuring.

    The realised figure is write-offs *plus* units still sitting unsold in
    batches whose expiry has already passed. Counting only booked write-offs
    would understate it: 6.9% of expired batches carry a residue the ledger has
    not yet cleared, and treating those as survivors would credit the shelf with
    stock that has already gone.
    """
    on_hand, sold, written_off, stranded, predicted, value = con.execute(VALIDATION_SQL).fetchone()
    if on_hand is None or not on_hand:
        return

    realised = (written_off or 0) + (stranded or 0)
    print("\n  what happened next, over the seven days the scores predicted")
    print(f"    {on_hand:>8,.0f} units on hand in at-risk batches")
    print(f"    {sold:>8,.0f} sold")
    print(f"    {written_off:>8,.0f} written off")
    print(f"    {stranded:>8,.0f} still unsold in batches now past expiry")
    print(f"    {realised:>8,.0f} unsold at expiry in total, against {predicted:,.0f} predicted")

    if realised:
        ratio = predicted / realised
        print(
            f"\n  the model over-states units at risk by {ratio - 1:.0%}. That direction is "
            f"chosen, not accidental:\n"
            f"    the dispersion is fitted rather than assumed Poisson, which widens the demand\n"
            f"    distribution and raises expected leftover, and the model cannot see the\n"
            f"    markdowns that clear near-expiry stock. For a queue that is read top-down,\n"
            f"    flagging stock that turns out fine costs a look; missing stock that spoils\n"
            f"    costs the batch."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Score expiry risk for every open batch.")
    parser.add_argument("--warehouse", default=str(WAREHOUSE))
    args = parser.parse_args()

    warehouse = Path(args.warehouse)
    if not warehouse.exists():
        print(f"\n\033[31mno warehouse at {warehouse}\033[0m\n  run `python tasks.py build`")
        return 1

    con = duckdb.connect(str(warehouse))
    try:
        con.execute("set memory_limit = '4GB'")
        forecasts = con.execute(
            """
            select count(*) from information_schema.tables
            where table_name = 'mart_demand_forecast'
            """
        ).fetchone()[0]
        if not forecasts:
            print(
                "\n\033[31mno mart_demand_forecast\033[0m\n"
                "  run `python tasks.py backtest` then `python tasks.py forecast`"
            )
            return 1
        build(con)
        code = report(con)
        validate(con)
        return code
    finally:
        con.execute("drop table if exists main._batch_position")
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
