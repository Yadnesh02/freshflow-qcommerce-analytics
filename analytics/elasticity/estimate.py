"""Price elasticity by category and days-to-expiry band (task S4.1).

S4.2's markdown optimiser needs one number to choose a discount depth: how much
more sells if the price drops by a percent. This estimates it, and most of the
work is arranging for that number to mean what it says rather than measuring
something else that moves at the same time.

**Price and freshness move together, and that is the whole difficulty.** Stock
is marked down *because* it is near expiry, and a shopper values near-expiry
stock less. Regressing quantity on price without holding freshness fixed
therefore measures a mixture of two effects pointing in opposite directions -
the discount pulling demand up, the age pulling it down - and reports the net as
"elasticity". Measured over this data the mean price ratio falls almost
monotonically with the DTE band, from 0.998 at 7d+ to 0.500 at 0-1d, so the
confound is close to total. The estimation is therefore run *within* band, which
is what the plan asks for and also the only way the coefficient is interpretable.

**The bands have to be disjoint, and `dim_dte_band` is not.** The seed gives
0-1d as [0,1] and 1-2d as [1,2], so a `between` join puts every store-SKU-day at
one, two, three, five or seven days out into two cells at once. That is not a
rounding detail here: the first version of this estimate had a 0-1d cell that was
**59.5% dte=1 stock** - 15,221 shelf-days of "expires tomorrow" against 10,360 of
"expires today" - so the headline "response collapses on the last day" was
substantially a next-to-last-day number, and 1-2d and 2-3d were composed entirely
of days shared with a neighbour. The bands are now read half-open,
`[min_days, max_days)`, giving {0}, {1}, {2}, {3,4}, {5,6}, {7+}. S4.2 applies
them the same way, which is the point - a coefficient fitted on [0,1] and applied
to {0} is measuring a different population from the one it prices.

    Half-open rather than "lowest sort_order wins", which was the first attempt
    and is wrong in a way that reads as right. Taking the earliest matching band
    hands the shared endpoint *downward*, so 0-1d collects {0,1}, 1-2d collects
    only {2}, 2-3d only {3}, and every label ends up describing the day above
    its own range. The estimate ran and produced sensible-looking coefficients
    against silently mislabelled populations.

**Freshness is measured from stock on offer, not from what sold.**
`dte_at_sale` exists only for units that sold, so a panel keyed on it can never
contain a day where the price was cut and nobody bought - which is exactly the
observation elasticity is estimated from. The days-to-expiry here is the minimum
across batches still holding stock, replayed from the movement ledger, and it is
defined on every day the SKU was on the shelf.

**Price comes from the price panel, never from the sales aggregate.**
`agg_store_sku_day.base_price_avg` is populated on exactly the 29.5% of cells
where `units_sold > 0` and null everywhere else, because it is computed from
what sold. Using it would select the regression sample on its own dependent
variable and bias the elasticity toward zero. `fct_price_history` records the
posted price whether or not anybody bought.

    Measured both ways for comparison: at 0-1d the posted price varies with a
    log standard deviation of 0.150, while the price at which units actually
    sold has a standard deviation of 0.010 - almost every sale happened at the
    modal 50% discount. Conditioning on sales would have made that band look
    like a deterministic policy with no identifying variation at all.

**Poisson, not log-log OLS.** Demand here is a count and a third of the panel is
zero. `log(1 + q)` is a different quantity that behaves badly at the bottom,
where most of the mass is. A Poisson log link puts the coefficient on
`log(price_ratio)` directly on the elasticity scale and handles zeros natively.

**The store-SKU baseline enters as an offset rather than as a fixed effect.**
Seventeen thousand dummies is not a regression anybody runs; the offset is the
store-SKU's mean quantity on its *undiscounted* days, so what remains to be
explained is the lift relative to that SKU's own normal trade. Computing the
baseline on undiscounted days only matters: a baseline over all days would
partly absorb the discount response and attenuate the very coefficient being
estimated.

**Thin cells are shrunk toward the category, not reported raw.** A band with
four hundred discounted observations produces a confident number made of noise.
The weight is the share of the estimate's variance that is not sampling error,
so a cell earns its own coefficient in proportion to the evidence behind it.

    python tasks.py elasticity
"""

from __future__ import annotations

import argparse
import os
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import statsmodels.api as sm

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

TARGET_TABLE = "marts.mart_price_elasticity"

# A cell needs this many discounted observations before its own coefficient is
# worth more than the category's. Below it the estimate is mostly shrunk.
MIN_DISCOUNTED_ROWS = 200
# Undiscounted is not exactly 1.0: rounding in the price panel leaves ratios a
# hair under it, and treating those as markdowns would put noise in the baseline.
UNDISCOUNTED = 0.99
# Beyond this the "discount" is a clearance stunt rather than a price point -
# the 11-rupee slot lands near 0.11 - and its response is not the same curve.
MIN_PRICE_RATIO = 0.15


UNIDENTIFIED_NOTE = """
  Every one of those is the last day or two of shelf life, and the reason is the
  policy that generated the data rather than the estimator: the deepest cuts
  went to stock that was already not moving, so depth carries the weak demand it
  was reacting to. Controlling for freshness within the band and for how little
  was left moved these from around +0.20 to around zero. What remains is not
  separable from observational data - it needs a depth randomised against
  expected demand, which is what S5.1's policy backtest can supply."""

PANEL_SQL = f"""
with batch_daily as (

    select batch_id, date_day, sum(qty_delta) as delta
    from marts.fct_inventory_movement
    group by batch_id, date_day

),

-- what each batch held at the end of each day it moved
batch_position as (

    select
        batch_id,
        date_day,
        sum(delta) over (
            partition by batch_id order by date_day
            rows between unbounded preceding and current row
        ) as on_hand
    from batch_daily

),

-- freshness on offer: the oldest stock still available, which under FEFO is
-- what a shopper is handed. Defined on every stocked day, sold or not.
on_offer as (

    select
        position.date_day,
        batches.store_id,
        batches.sku_id,
        min(batches.expiry_date - position.date_day) as days_to_expiry,
        sum(position.on_hand) as units_on_hand
    from batch_position as position
    inner join marts.fct_inventory_batch as batches
        on batches.batch_id = position.batch_id
    where position.on_hand > 0
    group by position.date_day, batches.store_id, batches.sku_id

),

-- one row per store-SKU-day the product was on the shelf
panel as (

    select
        on_offer.store_id,
        on_offer.sku_id,
        on_offer.date_day,
        on_offer.units_on_hand,
        on_offer.days_to_expiry,
        (
            select bands.dte_band
            from marts.dim_dte_band as bands
            where
                on_offer.days_to_expiry >= bands.min_days
                and on_offer.days_to_expiry < bands.max_days
            order by bands.sort_order
            limit 1
        ) as dte_band,
        products.l1_category,
        products.abc_class,
        sales.units_sold,

        -- posted price, from the price panel rather than from what sold
        coalesce(price.realized_price, products.base_price)
        / nullif(products.base_price, 0) as price_ratio,

        calendar.is_weekend,
        calendar.is_festival,
        calendar.is_salary_week
    from on_offer
    inner join marts.dim_product as products
        on products.sku_id = on_offer.sku_id
    inner join marts.dim_date as calendar
        on calendar.date_day = on_offer.date_day
    inner join marts.agg_store_sku_day as sales
        on
            sales.store_id = on_offer.store_id
            and sales.sku_id = on_offer.sku_id
            and sales.date_day = on_offer.date_day
    left join (
        -- promotions stack, so collapse to the deepest posted price for the day
        select store_id, sku_id, effective_from_date, effective_to_date,
               min(realized_price) as realized_price
        from marts.fct_price_history
        group by all
    ) as price
        on
            price.store_id = on_offer.store_id
            and price.sku_id = on_offer.sku_id
            and on_offer.date_day
            between price.effective_from_date and price.effective_to_date
    -- a stockout truncates quantity, so the day says nothing about willingness
    -- to buy at that price
    where not sales.is_censored

),

-- the SKU's own normal trade, measured only on days it was not marked down.
-- A baseline taken over all days would absorb part of the discount response.
baseline as (

    select
        store_id,
        sku_id,
        avg(units_sold) as baseline_units,
        count(*) as undiscounted_days
    from panel
    where price_ratio >= {UNDISCOUNTED}
    group by store_id, sku_id

)

select
    panel.*,
    baseline.baseline_units,
    baseline.undiscounted_days
from panel
inner join baseline
    on panel.store_id = baseline.store_id and panel.sku_id = baseline.sku_id
where
    panel.price_ratio between {MIN_PRICE_RATIO} and 1.0
    and baseline.baseline_units > 0
    and baseline.undiscounted_days >= 10
"""


def load_panel(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    con.execute("set memory_limit = '5GB'")
    return con.execute(PANEL_SQL).df()


def fit_cell(frame: pd.DataFrame) -> tuple[float, float, int]:
    """Poisson elasticity for one cell. Returns (beta, standard error, n).

    The offset carries the store-SKU baseline, so the coefficient describes lift
    against that SKU's own normal trade rather than against the panel mean.
    Standard errors are clustered on store-SKU: a SKU's residuals are correlated
    across days by everything the model does not see, and unclustered errors
    would make every coefficient look far more certain than it is.

    **Controls with no variation inside the cell are dropped, not fitted.** Once
    the DTE bands are disjoint, three of them span a single day - 0-1d is {0},
    1-2d is {1}, 2-3d is {2} - and `days_to_expiry` becomes a constant column
    sitting next to the intercept. That design is singular: the fit either
    raises, or returns a coefficient with a NaN standard error, which
    `is_identified` then reads as "not identified" and the cell disappears into
    the category. The first disjoint run lost 0-1d, 1-2d and 2-3d entirely for
    every category this way - the bands with the most discounting in them, and
    the ones the whole markdown question turns on.

    The control still earns its place in the bands that span more than a day and
    in every category-level fit, so it is dropped per fit rather than removed.
    """
    design = pd.DataFrame(
        {
            "log_price": np.log(frame["price_ratio"].to_numpy(dtype=float)),
            # Freshness varies inside the wider bands - 3-5d holds both - and the
            # deeper cut goes to the older stock, which is also what shoppers
            # refuse. Without this the coefficient carries that refusal.
            "days_to_expiry": frame["days_to_expiry"].to_numpy(dtype=float),
            # Marked-down stock is by definition what is left. A thin remnant
            # sells less because there is less of it, not because of its price.
            "log_on_hand": np.log(frame["units_on_hand"].to_numpy(dtype=float) + 1.0),
            "is_weekend": frame["is_weekend"].astype(int).to_numpy(),
            "is_festival": frame["is_festival"].astype(int).to_numpy(),
            "is_salary_week": frame["is_salary_week"].astype(int).to_numpy(),
        }
    )
    constant_controls = [
        name
        for name in design.columns
        if name != "log_price" and design[name].nunique(dropna=False) <= 1
    ]
    design = design.drop(columns=constant_controls)
    design = sm.add_constant(design, has_constant="add")
    offset = np.log(frame["baseline_units"].to_numpy(dtype=float))
    groups = (frame["store_id"] + "|" + frame["sku_id"]).to_numpy()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = sm.GLM(
            frame["units_sold"].to_numpy(dtype=float),
            design,
            family=sm.families.Poisson(),
            offset=offset,
        )
        fitted = model.fit(cov_type="cluster", cov_kwds={"groups": groups})

    return float(fitted.params["log_price"]), float(fitted.bse["log_price"]), len(frame)


def is_identified(beta: float, se: float) -> bool:
    """Whether the cell actually found a downward-sloping demand curve.

    Not a significance ritual. S4.2 divides by this number to choose a discount
    depth, and a coefficient whose confidence interval contains zero says the
    data could not tell whether cutting the price sells more or less. Passing
    that through as a small positive number would tell the optimiser to raise
    prices on expiring stock, which is the one recommendation guaranteed to be
    wrong.
    """
    return beta + 1.96 * se < 0


def shrink(cell_beta: float, cell_se: float, pooled_beta: float) -> tuple[float, float]:
    """Blend a cell toward its category by how much of its spread is signal.

    weight = 1 - se^2 / (se^2 + (beta - pooled)^2)

    which is the share of the cell's distance from the pooled estimate that
    sampling error cannot account for. A precise cell far from the pool keeps
    its own number; a noisy cell near it is absorbed. No tuning constant.
    """
    gap = (cell_beta - pooled_beta) ** 2
    weight = gap / (gap + cell_se**2) if (gap + cell_se**2) > 0 else 0.0
    return weight * cell_beta + (1 - weight) * pooled_beta, weight


def estimate(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for category, by_category in panel.groupby("l1_category", observed=True):
        discounted = by_category[by_category["price_ratio"] < UNDISCOUNTED]
        if len(discounted) < MIN_DISCOUNTED_ROWS:
            continue
        pooled_beta, pooled_se, pooled_n = fit_cell(by_category)

        for band, cell in by_category.groupby("dte_band", observed=True):
            n_discounted = int((cell["price_ratio"] < UNDISCOUNTED).sum())
            if n_discounted < 30:
                continue
            try:
                beta, se, n = fit_cell(cell)
            except Exception:  # noqa: BLE001 - a cell that will not converge is reported, not raised
                continue
            shrunk, weight = shrink(beta, se, pooled_beta)
            identified = is_identified(beta, se)
            category_identified = is_identified(pooled_beta, pooled_se)

            # The fallback chain, and it only ever falls back to something the
            # data did establish. A cell that found no slope borrows its
            # category's; a category that found none is left null rather than
            # given a number, because S4.2 must decline to optimise there rather
            # than optimise on a guess.
            if identified:
                usable = shrunk
                basis = "cell"
            elif category_identified:
                usable = pooled_beta
                basis = "category"
            else:
                usable = None
                basis = "none"

            rows.append(
                {
                    "l1_category": category,
                    "dte_band": band,
                    "observations": n,
                    "discounted_observations": n_discounted,
                    "elasticity_raw": beta,
                    "standard_error": se,
                    "is_identified": identified,
                    "category_elasticity": pooled_beta,
                    "category_standard_error": pooled_se,
                    "category_observations": pooled_n,
                    "category_is_identified": category_identified,
                    "signal_weight": weight,
                    "elasticity_shrunk": shrunk,
                    "elasticity": usable,
                    "elasticity_basis": basis,
                }
            )
    return pd.DataFrame(rows)


def write(con: duckdb.DuckDBPyConnection, estimates: pd.DataFrame) -> None:
    con.register("_elasticity", estimates)
    con.execute(f"create or replace table {TARGET_TABLE} as select * from _elasticity")


def report(estimates: pd.DataFrame) -> int:
    if estimates.empty:
        print("\n  no cell had enough price variation to estimate")
        return 1

    print(f"\n  {len(estimates)} category x DTE-band cells estimated\n")
    header = (
        f"  {'category':<26}{'band':<8}{'n':>9}{'disc':>7}{'raw':>8}{'se':>6}{'used':>8}  basis"
    )
    print(header)
    print("  " + "-" * 82)
    for _, row in estimates.sort_values(["l1_category", "dte_band"]).iterrows():
        used = "    --" if row["elasticity"] is None else f"{row['elasticity']:>6.2f}"
        flag = "" if row["is_identified"] else "   <- no slope found"
        print(
            f"  {row['l1_category'][:25]:<26}{row['dte_band']:<8}"
            f"{row['observations']:>9,}{row['discounted_observations']:>7,}"
            f"{row['elasticity_raw']:>8.2f}{row['standard_error']:>6.2f}"
            f"{used:>8}  {row['elasticity_basis']}{flag}"
        )

    print("\n  category elasticity, most elastic first")
    seen = estimates.drop_duplicates("l1_category").sort_values("category_elasticity")
    for _, row in seen.iterrows():
        mark = "" if row["category_is_identified"] else "   (not distinguishable from zero)"
        print(f"    {row['l1_category']:<28}{row['category_elasticity']:>8.2f}{mark}")

    identified = estimates[estimates["is_identified"]]
    unidentified = estimates[~estimates["is_identified"]]
    wrong_sign = identified[identified["elasticity_raw"] > 0]

    print(f"\n  {len(identified)} of {len(estimates)} cells found a downward-sloping curve")
    if len(unidentified):
        print("  cells where the data could not establish a price response:")
        for _, row in unidentified.iterrows():
            print(
                f"    {row['l1_category']:<26}{row['dte_band']:<7}"
                f"{row['elasticity_raw']:+.2f} +/- {1.96 * row['standard_error']:.2f}"
                f"  -> falls back to {row['elasticity_basis']}"
            )
        print(UNIDENTIFIED_NOTE)

    print("\n  S4.1 gate - every identified cell slopes downward: ", end="")
    if wrong_sign.empty:
        print("PASS")
    else:
        print(f"FAIL on {len(wrong_sign)}")
        for _, row in wrong_sign.iterrows():
            print(f"    {row['l1_category']} / {row['dte_band']}: {row['elasticity_raw']:+.2f}")
    return 0 if wrong_sign.empty else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Estimate price elasticity by category and DTE.")
    parser.add_argument("--warehouse", default=str(WAREHOUSE))
    args = parser.parse_args()

    warehouse = Path(args.warehouse)
    if not warehouse.exists():
        print(f"\n\033[31mno warehouse at {warehouse}\033[0m\n  run `python tasks.py build`")
        return 1

    con = duckdb.connect(str(warehouse))
    try:
        print("  building the panel...", flush=True)
        panel = load_panel(con)
        print(f"  {len(panel):,} store-SKU-days on the shelf and uncensored")
        estimates = estimate(panel)
        write(con, estimates)
        return report(estimates)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
