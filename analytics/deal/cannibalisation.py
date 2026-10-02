"""Did the rail sell units it would have sold anyway? (task D2)

The deal P&L has a term nobody has measured. `deal_slots.py` nets the subsidy
against baseline units at their normal price, which charges the rail only for
the units it actually moved. It never asks the other question: on the days
*around* a slot, did the SKU sell less than it otherwise would have?

Two mechanisms, pulling opposite ways in time:

  pull-forward   someone who would have bought on Thursday buys on Tuesday
                 because Tuesday is Rs 11. The units are not incremental, they
                 are early, and the days after a slot should sag.

  wait-for-it    a SKU that is dealt often teaches its buyers to wait, and the
                 days *before* a slot sag instead. This one is slower to appear
                 and more expensive when it does, because it depresses the
                 baseline the rail is then measured against.

**Why an event study rather than a regression.** The question is about shape -
whether a dip sits before the slot, after it, or not at all - and a single
coefficient would average the two mechanisms into one number that could be zero
while both are large. Reporting by relative day keeps them apart.

**The event is a block, not a day.** The rail does not run for a day and stop:
695 of 710 store-SKU slots run for seven consecutive days. The first version of
this module looked for isolated slot days and found none, because every day of a
block sits inside another day's window. Relative day is therefore measured from
the edges - negative from the day the block opens, positive from the day it
closes - and the block is reported as one period rather than smeared across the
window it occupies.

**The baseline is the same store-SKU on clean days**, not other SKUs and not
other stores. A SKU that gets a slot is not a random SKU: it is cheap, it is
deal-eligible, and the allocator picked it. Comparing it to anything but itself
would measure the selection instead of the slot.

**Slots are read from fct_price_history, not from sales.** The price table
records every slot that ran, including the ones that sold nothing; taking slot
days from order lines would silently drop exactly the failures worth seeing.

    python tasks.py deal-cannibalisation
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

DEAL_PROMO_ID = "PROMO-DEAL11"
WINDOW = 7

EVENTS = f"""
with blocks as (
    select
        store_id,
        sku_id,
        effective_from_date                              as block_start,
        coalesce(effective_to_date, effective_from_date) as block_end
    from marts.fct_price_history
    where promo_id = '{DEAL_PROMO_ID}'
),
clean as (
    -- store-SKUs the rail visited exactly once. Where it ran twice the windows
    -- overlap and a day is both "after" one block and "before" the next, which
    -- makes its relative day meaningless. 13 of 710 pairs are dropped this way.
    select b.*
    from blocks b
    where 1 = (
        select count(*) from blocks o
        where o.store_id = b.store_id and o.sku_id = b.sku_id
    )
)
select
    c.store_id,
    c.sku_id,
    c.block_start,
    c.block_end,
    a.date_day,
    case
        when a.date_day < c.block_start then date_diff('day', c.block_start, a.date_day)
        when a.date_day > c.block_end   then date_diff('day', c.block_end, a.date_day)
        else 0
    end                                              as rel_day,
    a.date_day between c.block_start and c.block_end as in_block,
    a.units_sold,
    a.is_censored
from clean c
join marts.agg_store_sku_day a
  on a.store_id = c.store_id and a.sku_id = c.sku_id
 and a.date_day between c.block_start - {WINDOW} and c.block_end + {WINDOW}
"""

BASELINE = f"""
with blocks as (
    select
        store_id,
        sku_id,
        effective_from_date                              as block_start,
        coalesce(effective_to_date, effective_from_date) as block_end
    from marts.fct_price_history
    where promo_id = '{DEAL_PROMO_ID}'
)
select
    a.store_id,
    a.sku_id,
    avg(a.units_sold) as baseline_units,
    count(*)          as clean_days
from marts.agg_store_sku_day a
where exists (select 1 from blocks b where b.store_id = a.store_id and b.sku_id = a.sku_id)
  and not exists (
        select 1 from blocks b
        where b.store_id = a.store_id and b.sku_id = a.sku_id
          and a.date_day between b.block_start - {WINDOW} and b.block_end + {WINDOW}
  )
  and not a.is_censored
group by 1, 2
having count(*) >= {WINDOW}
"""


def event_panel(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Every clean slot block's window, joined to its own store-SKU baseline."""
    events = con.execute(EVENTS).df()
    base = con.execute(BASELINE).df()
    if events.empty or base.empty:
        raise ValueError(
            f"no usable {DEAL_PROMO_ID} blocks in this build - either the rail never ran, "
            f"or no store-SKU has {WINDOW} clean days to form a baseline from"
        )
    panel = events.merge(base, on=["store_id", "sku_id"], how="inner")
    # A censored day ran out of stock, so its sales are a supply fact rather
    # than a demand one. Leaving them in would read a stockout as a dip.
    return panel[~panel["is_censored"]].copy()


def by_relative_day(panel: pd.DataFrame) -> pd.DataFrame:
    """Mean units against the store-SKU's own baseline, per day around the block."""
    panel = panel.assign(lift=panel["units_sold"] - panel["baseline_units"])
    out = (
        panel.groupby(["in_block", "rel_day"])
        .agg(
            mean_units=("units_sold", "mean"),
            baseline=("baseline_units", "mean"),
            lift=("lift", "mean"),
            sd=("lift", "std"),
            n=("lift", "size"),
        )
        .reset_index()
    )
    out["se"] = out["sd"] / out["n"].pow(0.5)
    out["ci_low"] = out["lift"] - 1.96 * out["se"]
    out["ci_high"] = out["lift"] + 1.96 * out["se"]
    return out.sort_values(["in_block", "rel_day"]).reset_index(drop=True)


def summarise(panel: pd.DataFrame) -> dict[str, float]:
    """Uptake and the sag either side, per block.

    Per block rather than per day, because a block is the unit the allocator
    actually chooses - `rec_deal_slot` picks a SKU for a run, not for a Tuesday.
    """
    panel = panel.assign(lift=panel["units_sold"] - panel["baseline_units"])
    blocks = panel[["store_id", "sku_id", "block_start"]].drop_duplicates().shape[0]
    if not blocks:
        return dict.fromkeys(
            ["blocks", "in_block_lift", "pre_window_lift", "post_window_lift"], float("nan")
        )
    during = panel[panel["in_block"]]
    before = panel[~panel["in_block"] & panel["rel_day"].between(-WINDOW, -1)]
    after = panel[~panel["in_block"] & panel["rel_day"].between(1, WINDOW)]
    return {
        "blocks": float(blocks),
        "in_block_lift": float(during["lift"].sum() / blocks),
        "pre_window_lift": float(before["lift"].sum() / blocks),
        "post_window_lift": float(after["lift"].sum() / blocks),
        "net_cannibalisation": float((before["lift"].sum() + after["lift"].sum()) / blocks),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE)
    args = ap.parse_args(argv)

    con = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        panel = event_panel(con)
    finally:
        con.close()

    daily = by_relative_day(panel)
    summary = summarise(panel)

    header = f"{'day':>8} {'units':>9} {'baseline':>9} {'lift':>9} {'95% CI':>20} {'n':>7}"
    print()
    print(f"  {summary['blocks']:,.0f} slot blocks, {len(panel):,} uncensored store-SKU-days")
    print()
    print(f"    {header}")
    for _, r in daily.iterrows():
        if r["in_block"]:
            label, mark = "in block", "  <- the slot"
        else:
            label, mark = f"{int(r['rel_day']):+d}", ""
        ci = f"[{r['ci_low']:+.2f}, {r['ci_high']:+.2f}]"
        print(
            f"    {label:>8} {r['mean_units']:>9.2f} {r['baseline']:>9.2f} "
            f"{r['lift']:>+9.2f} {ci:>20} {int(r['n']):>7,}{mark}"
        )

    print()
    print(f"    during the block       {summary['in_block_lift']:>+8.2f} units per block")
    print(f"    the {WINDOW} days before     {summary['pre_window_lift']:>+8.2f} units per block")
    print(f"    the {WINDOW} days after      {summary['post_window_lift']:>+8.2f} units per block")
    print(f"    {'-' * 42}")
    print(f"    net cannibalisation    {summary['net_cannibalisation']:>+8.2f} units per block")

    lift = summary["in_block_lift"]
    if lift and not math.isnan(lift):
        share = -summary["net_cannibalisation"] / lift
        print()
        print(f"    {share:.1%} of the block's lift is paid for out of the days around it.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
