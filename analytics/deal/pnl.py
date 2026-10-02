"""What the deal rail costs on its own line (task D2).

**This module exists because a number lived in a docstring with no query behind
it.** `analytics/optimization/deal_slots.py` stated the rail at minus Rs 426,683
a year on 12,153 units and Rs 162,532 of revenue. That is Rs 13.37 a unit on an
Rs 11 deal, which cannot be true for deal-priced lines, and no definition of
revenue on the warehouse reproduces it. The figure was prose, so nothing caught
it; D2's first job is to make the same claim executable.

**The definition, and why each half of it.**

    lines     promo_id = 'PROMO-DEAL11'. The promo tag is set by the price that
              actually bound, so a line where a markdown and the slot stacked
              belongs to the slot - which is the attribution
              `assert_stacked_promotions_attribute_to_the_price_setter` already
              enforces upstream.

    units     signed_qty, not units. A P&L nets returns; `units` is unsigned and
              counts a returned unit twice, once out and once back.

    revenue   net_revenue, which is what the customer paid. gross_revenue on
              these lines is the base price the rail did NOT charge - using it
              turns a Rs 437k loss into a Rs 231k profit, which is the most
              expensive available way to get this wrong.

    cogs      landed cost of the units that moved.

**The invariant that would have caught the original error.** On deal-priced
lines, revenue divided by net units must equal the deal price. It is Rs 11.00 to
the paisa here, and it was Rs 13.37 in the figure this replaces. One assertion
is the whole difference between a number that can rot and one that cannot.

Nothing here is the rail's verdict. This is the cost side only. What it earns
back is `attach.py` and `cannibalisation.py`, and the rail is not judged until
those land.

    python tasks.py deal-pnl
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

DEAL_PROMO_ID = "PROMO-DEAL11"
DEAL_PRICE = 11.0

# The realised price may miss the deal price by rounding on a returned line, but
# not by more than this. Wider and the promo tag is being set on lines the rail
# did not price; the original Rs 13.37 would have tripped it by a mile.
PRICE_TOLERANCE = 0.05


@dataclass(frozen=True)
class DealPnL:
    """The rail's standalone cost. Every field traceable to one statement."""

    lines: int
    units: int
    revenue: float
    cogs: float
    first_day: str
    last_day: str

    @property
    def margin(self) -> float:
        return self.revenue - self.cogs

    @property
    def price_per_unit(self) -> float:
        return self.revenue / self.units if self.units else 0.0

    def check(self) -> None:
        """The self-check the prose version did not have."""
        drift = abs(self.price_per_unit - DEAL_PRICE)
        if drift > PRICE_TOLERANCE:
            raise ValueError(
                f"deal lines realise Rs {self.price_per_unit:.2f} a unit against a "
                f"Rs {DEAL_PRICE:.2f} deal price - off by Rs {drift:.2f}. Either the promo tag "
                f"is on lines the rail did not price, or the revenue measure is not what the "
                f"customer paid."
            )


QUERY = f"""
select
    count(*)                      as lines,
    sum(signed_qty)               as units,
    sum(net_revenue)              as revenue,
    sum(cogs)                     as cogs,
    cast(min(date_day) as varchar) as first_day,
    cast(max(date_day) as varchar) as last_day
from marts.fct_order_item
where promo_id = '{DEAL_PROMO_ID}'
"""


def standalone_pnl(con: duckdb.DuckDBPyConnection) -> DealPnL:
    """The rail's cost over whatever window the warehouse holds."""
    row = con.execute(QUERY).fetchone()
    if row is None or row[1] is None:
        raise ValueError(
            f"no {DEAL_PROMO_ID} lines in this build - the rail never ran, or the promo tag moved"
        )
    pnl = DealPnL(
        lines=int(row[0]),
        units=int(row[1]),
        revenue=float(row[2]),
        cogs=float(row[3]),
        first_day=str(row[4]),
        last_day=str(row[5]),
    )
    pnl.check()
    return pnl


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE)
    args = ap.parse_args(argv)

    con = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        pnl = standalone_pnl(con)
    finally:
        con.close()

    print(f"\n  The {DEAL_PROMO_ID} rail, {pnl.first_day} to {pnl.last_day}\n")
    print(f"    lines            {pnl.lines:>14,}")
    print(f"    units (net)      {pnl.units:>14,}")
    print(f"    revenue          {pnl.revenue:>14,.0f}")
    print(f"    cogs             {pnl.cogs:>14,.0f}")
    print(f"    {'-' * 31}")
    print(f"    margin           {pnl.margin:>14,.0f}")
    print(f"\n    realised price   {pnl.price_per_unit:>14.2f}  (deal price {DEAL_PRICE:.2f})")
    print("\n  Cost only. What the rail earns back is attach.py and cannibalisation.py.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
