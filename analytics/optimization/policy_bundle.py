"""Export what Sprint 4 learned, in the form Policy B can actually use (S4.6).

This is the join between the two halves of the project, and the shape of it is
the argument.

**Policy B cannot read the rec_* tables, and that is the point.** Those tables
are recommendations computed on a warehouse built from a full year of emitted
events. A policy running inside the simulation on day 120 that looked them up
would be reading its own future - the recommendation for day 120 was computed
knowing what happened on day 300. Every number the Sprint 5 experiment produced
would then be a measurement of look-ahead rather than of the policy. So what
crosses the boundary is not decisions but *fitted parameters*: the elasticity by
category and freshness band, the demand dispersion, the lead-time distribution,
and the knobs each optimiser was tuned with. Policy B applies those rules to
whatever it can see that morning, exactly as the baseline applies its ladder.

**And it must use the elasticity it estimated, never the one the simulator
knows.** `catalog["elasticity"]` is ground truth - the actual coefficient the
demand engine draws from. A policy reading it would score beautifully and prove
nothing, because no real business has that column. Policy B reads the estimates
from `mart_price_elasticity`, which were fitted from emitted events and are
wrong in the specific ways S4.1 documented: nine of twenty-three cells could not
be identified at all. `test_the_bundle_never_carries_ground_truth` checks the
exported file for the column by name.

**Lead time is exported per category, not per store-SKU.** Per store-SKU would
be 21,000 numbers and most of them estimated from a handful of orders; per
category is 11 numbers each backed by thousands. The baseline's single assumed
2.5 days is naivety #1 on its list, and beating it does not require perfect
resolution - it requires noticing that dairy and masala are not the same.

The bundle is written as JSON rather than parquet so it can be read in a diff.
When the Sprint 5 ablation asks which component drove the gain, this file is
what distinguishes the arms, and it should be legible.

    python tasks.py policy-bundle
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import duckdb

from analytics.expiry_risk import fit_dispersion
from analytics.optimization import deal_slots, markdown, newsvendor, transfers

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)
BUNDLE_PATH = ROOT / "simulator" / "config" / "policy_bundle.json"

# Ground truth the simulator holds. If any of these ever appear in the bundle,
# the experiment is measuring the data-generating process rather than a policy.
FORBIDDEN_KEYS = {"elasticity_true", "popularity_weight", "hour_curve", "demand_index"}


ELASTICITY_SQL = """
select
    est.l1_category,
    est.dte_band,
    bands.min_days,
    bands.max_days,
    est.elasticity_raw,
    est.is_identified,
    est.elasticity as elasticity_reported
from marts.mart_price_elasticity as est
inner join marts.dim_dte_band as bands on bands.dte_band = est.dte_band
order by est.l1_category, bands.sort_order
"""

LEAD_TIME_SQL = """
select
    products.l1_category,
    quantile_cont(purchase.lead_time_days, 0.90) as lead_days_p90,
    avg(purchase.lead_time_days) as lead_days_mean,
    count(*) as observations
from staging.stg_wms__purchase_orders as purchase
inner join marts.dim_product as products on products.sku_id = purchase.sku_id
where purchase.lead_time_days is not null
group by products.l1_category
order by products.l1_category
"""


def build(con: duckdb.DuckDBPyConnection) -> dict:
    """Assemble the bundle from the marts Sprint 4 produced."""
    con.execute("set memory_limit = '4GB'")

    elasticity = [
        {
            "l1_category": row[0],
            "dte_band": row[1],
            "min_days": int(row[2]),
            "max_days": int(row[3]),
            "elasticity_raw": float(row[4]),
            "is_identified": bool(row[5]),
            # what S4.2 is willing to act on: the cell's own slope, or nothing
            "elasticity_usable": float(row[4]) if row[5] else None,
        }
        for row in con.execute(ELASTICITY_SQL).fetchall()
    ]

    lead_times = {
        row[0]: {
            "p90": float(row[1]),
            "mean": float(row[2]),
            "observations": int(row[3]),
        }
        for row in con.execute(LEAD_TIME_SQL).fetchall()
    }
    pooled = con.execute(
        "select quantile_cont(lead_time_days, 0.90) from staging.stg_wms__purchase_orders "
        "where lead_time_days is not null"
    ).fetchone()[0]

    identified = sum(1 for cell in elasticity if cell["is_identified"])

    return {
        "schema_version": 1,
        "provenance": {
            "elasticity_cells": len(elasticity),
            "elasticity_identified": identified,
            "lead_time_categories": len(lead_times),
            "note": (
                "Fitted parameters only. No per-day recommendations and no simulator "
                "ground truth: a policy reading either would be scoring its own future."
            ),
        },
        "demand": {
            "dispersion_k": float(fit_dispersion(con)),
            "distribution": "negative_binomial",
        },
        "elasticity": elasticity,
        "lead_time_days": {
            "by_category": lead_times,
            "pooled_p90": float(pooled),
        },
        "newsvendor": {
            "review_period_days": newsvendor.REVIEW_PERIOD_DAYS,
            "lead_time_quantile": newsvendor.LEAD_TIME_QUANTILE,
            "shelf_life_cap_quantile": newsvendor.SHELF_LIFE_CAP_QUANTILE,
            "stockout_retention_cost": newsvendor.DEFAULT_STOCKOUT_RETENTION_COST,
        },
        "markdown": {
            "depth_grid": [float(d) for d in markdown.DEPTH_GRID],
            "budget_per_store_day": markdown.DEFAULT_BUDGET_PER_STORE_DAY,
            "disposal_cost_per_unit": 0.0,
            "note": (
                "At the fitted elasticities the optimiser holds price nearly "
                "everywhere: while stock is short of demand the objective is revenue, "
                "which falls with price whenever |elasticity| < 1, and no band reaches 1."
            ),
        },
        "deal_slot": {
            "deal_price": deal_slots.DEAL_PRICE,
            "slots_per_store_day": deal_slots.DEFAULT_SLOTS_PER_STORE_DAY,
            "private_label_floor": deal_slots.PRIVATE_LABEL_FLOOR,
            "uplift_multiplier": deal_slots.DEAL_UPLIFT_MULTIPLIER,
            "incremental_basket_margin": deal_slots.INCREMENTAL_BASKET_MARGIN,
            "reactivation_rate": deal_slots.REACTIVATION_RATE,
            "reactivation_value": deal_slots.DEFAULT_REACTIVATION_VALUE,
            "min_on_hand_units": deal_slots.MIN_ON_HAND_UNITS,
            "min_days_to_expiry": deal_slots.MIN_DAYS_TO_EXPIRY,
        },
        "transfers": {
            "speed_kmh": transfers.DEFAULT_SPEED_KMH,
            "handling_hours": transfers.DEFAULT_HANDLING_HOURS,
            "cost_per_km": transfers.DEFAULT_COST_PER_KM,
            "fixed_trip_cost": transfers.DEFAULT_FIXED_TRIP_COST,
            "min_transfer_units": transfers.MIN_TRANSFER_UNITS,
        },
    }


def write_bundle(bundle: dict, path: Path = BUNDLE_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def report(bundle: dict, path: Path) -> int:
    prov = bundle["provenance"]
    print(f"\n  wrote {path.relative_to(ROOT)}  ({path.stat().st_size / 1024:.1f} KB)\n")
    print(f"    elasticity cells        {prov['elasticity_cells']:>6}")
    print(f"      of which identified   {prov['elasticity_identified']:>6}")
    print(f"    lead-time categories    {prov['lead_time_categories']:>6}")
    print(f"    dispersion k            {bundle['demand']['dispersion_k']:>6.3f}")
    print(f"    pooled lead time p90    {bundle['lead_time_days']['pooled_p90']:>6.1f} days")

    print("\n  lead time by category, against the baseline's single assumed 2.5 days:")
    for category, stats in sorted(
        bundle["lead_time_days"]["by_category"].items(), key=lambda kv: kv[1]["p90"]
    ):
        print(
            f"    {category:<26}{stats['p90']:>5.1f}  (mean {stats['mean']:.2f}, "
            f"n={stats['observations']:,})"
        )

    leaked = FORBIDDEN_KEYS & set(json.dumps(bundle).split('"'))
    print(f"\n  ground-truth columns in the bundle: {sorted(leaked) if leaked else 'none'}")
    return 1 if leaked else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Sprint 4's fitted policy parameters.")
    parser.add_argument("--warehouse", default=str(WAREHOUSE))
    parser.add_argument("--out", default=str(BUNDLE_PATH))
    args = parser.parse_args()

    warehouse = Path(args.warehouse)
    if not warehouse.exists():
        print(f"\n\033[31mno warehouse at {warehouse}\033[0m\n  run `python tasks.py build`")
        return 1

    con = duckdb.connect(str(warehouse))
    try:
        bundle = build(con)
        path = write_bundle(bundle, Path(args.out))
        return report(bundle, path)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
