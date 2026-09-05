"""Where the customer-value conclusion crosses over (task S5.3).

An *analysis* sensitivity, not a simulation one, and the distinction is the
point. Every other sweep in S5.3 changes what Policy B decides and so needs the
world run again. This one changes nothing anybody does: it recomputes
contribution from a world that already exists, at a delivery cost the event
stream never carried. So it costs one query rather than eighty runs, and putting
it in `simulator/sweep.py` would have implied otherwise.

**The conclusion under test.** `mart_customer_360` reports contribution by
discount-dependency band, and the README states that the least
discount-dependent customers are the most valuable. Contribution is
`gross_margin - subsidy - orders x delivery_cost`, and the delivery cost is a
single declared number - Rs 42 in `transform/dbt_project.yml` - that the feed
cannot supply. Bands differ in orders per customer, so the ranking between them
is a function of that number, and there is some value at which it reverses.

**Reported as a crossover rather than a p-value.** There is no sampling here:
every customer is observed and the arithmetic is exact at each cost. The
question is not whether the ranking differs by more than noise, it is at what
assumed cost it changes - and quoting a confidence interval would dress a
declared parameter up as a measurement.

    python tasks.py delivery-sweep
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)
DBT_PROJECT = ROOT / "transform" / "dbt_project.yml"

# Rs 0 to Rs 70, the range the plan asks for, at a fine enough step to locate a
# crossover to the rupee without pretending to more precision than a declared
# parameter deserves.
COSTS = tuple(range(0, 71, 2))

CONTRIBUTION = """
    select
        ddi_band,
        count(*)                                             as customers,
        sum(orders_90d)                                      as orders,
        sum(gross_margin_90d - subsidy_90d) / nullif(count(*), 0)
            - ? * sum(orders_90d) / nullif(count(*), 0)      as contribution_per_customer
    from marts.mart_customer_360
    where ddi_band is not null
    group by ddi_band
    order by ddi_band
"""


def declared_cost() -> float:
    """The value in force, read from dbt_project.yml rather than restated here."""
    for line in DBT_PROJECT.read_text(encoding="utf-8").splitlines():
        if "assumed_delivery_cost_per_order" in line:
            return float(line.split(":", 1)[1].strip())
    raise SystemExit("assumed_delivery_cost_per_order is not in transform/dbt_project.yml")


def curve(con: duckdb.DuckDBPyConnection) -> dict[float, dict[str, float]]:
    """Contribution per customer, per band, at each assumed delivery cost."""
    out: dict[float, dict[str, float]] = {}
    for cost in COSTS:
        rows = con.execute(CONTRIBUTION, [float(cost)]).fetchall()
        out[float(cost)] = {band: value for band, _customers, _orders, value in rows}
    return out


def crossover(curve_by_cost: dict[float, dict[str, float]], high: str, low: str) -> float | None:
    """The first cost at which `high` stops out-earning `low`.

    Returns None when the ranking never reverses in range, which is a different
    answer from "it reverses at the top of the range" and has to read that way.
    """
    ordered = sorted(curve_by_cost)
    initial = curve_by_cost[ordered[0]]
    if high not in initial or low not in initial:
        return None
    ahead = initial[high] > initial[low]
    for cost in ordered:
        band = curve_by_cost[cost]
        if (band[high] > band[low]) != ahead:
            return cost
    return None


def report(con: duckdb.DuckDBPyConnection) -> int:
    curve_by_cost = curve(con)
    bands = sorted({b for values in curve_by_cost.values() for b in values})
    if not bands:
        print("\n  mart_customer_360 has no ddi_band rows - run `python tasks.py build`")
        return 1

    declared = declared_cost()
    print("\n  contribution per customer over 90 days, by discount-dependency band")
    print(f"  the declared assumption is Rs {declared:g}/order\n")
    header = "".join(f"{b:>18}" for b in bands)
    print(f"  {'delivery cost':<16}{header}")
    for cost in sorted(curve_by_cost):
        if cost % 10 and cost != declared:
            continue
        marker = "  <- declared" if cost == declared else ""
        cells = "".join(f"{curve_by_cost[cost][b]:>18,.2f}" for b in bands)
        print(f"  Rs {cost:<13g}{cells}{marker}")

    print("\n  crossovers - the cost at which one band stops out-earning another")
    found = False
    for i, high in enumerate(bands):
        for low in bands[i + 1 :]:
            point = crossover(curve_by_cost, high, low)
            if point is not None:
                found = True
                side = "below" if point > declared else "above"
                print(
                    f"    {high} vs {low}: reverses at Rs {point:g} - "
                    f"the declared Rs {declared:g} sits {side} it"
                )
    if not found:
        print(f"    none within Rs {min(COSTS)}-{max(COSTS)}: the ranking holds across the range")
    print(
        "\n  No confidence interval, on purpose: every customer is observed and the arithmetic is\n"
        "  exact at each cost. The uncertainty is in the assumption, not in the estimate, and an\n"
        "  interval here would dress a declared parameter up as a measurement."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--warehouse", type=Path, default=WAREHOUSE)
    args = parser.parse_args(argv)
    if not args.warehouse.exists():
        print(f"\n  no warehouse at {args.warehouse} - run `python tasks.py build` first")
        return 1
    con = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        con.execute("set enable_progress_bar=false")
        return report(con)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
