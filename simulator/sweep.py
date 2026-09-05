"""Re-run the holdout with one parameter perturbed (task S5.3).

Sensitivity analysis here means two different things and they need separating,
because only one of them costs compute.

**Simulation sweeps** change what Policy B decides, so the world has to be run
again: the markdown disposal cost, the fitted elasticities, the forecast error,
the shelf life. Each setting is a fresh holdout run.

**Analysis sweeps** change how a figure is computed from a world that already
exists - the delivery cost per order, which reorders customer contribution by
discount-dependency band without any store behaving differently. Those belong in
`analytics/`, over the built warehouse, and are not in this file.

**The question this exists to answer.** The 180-day holdout found Policy B
expiring 76% more units than the baseline in its final month and writing off
314% more value, in all thirty seeds, with the cost per expired unit doubling.
The suspected cause is that Policy B has no price mechanism for clearing
expensive at-risk stock: at a zero disposal cost its markdown rule correctly
declines to cut anywhere, so expensive stock accumulates and dies, while the
baseline's crude flat ladder gives away margin and does clear it. If that is
right, the accumulation should shrink as the disposal cost rises. If it does
not, the cause is somewhere else and the sensitivity table has to say so.

**Rs 0 is deliberately one of the settings.** It reproduces the unperturbed
holdout, which is both a free control and a check that the perturbation
machinery changes nothing when it is asked to change nothing.

    python -m simulator.sweep --seed 7 --param disposal_cost --value 25
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulator.config_loader import load_sim_config  # noqa: E402
from simulator.policies.holdout import HoldoutPolicy, assign_treatment  # noqa: E402
from simulator.policies.optimized import BUNDLE_PATH, OptimizedPolicy  # noqa: E402
from simulator.run import SimulationRun  # noqa: E402

DEFAULT_DAYS = 180
DEFAULT_PRE_DAYS = 45
OUT_DIR = ROOT / "data" / "sweep"


def perturbed_bundle(param: str, value: float) -> dict:
    """The policy bundle with one knob moved.

    Only the bundle is perturbed, never the simulator's own configuration. The
    distinction matters: the bundle is what Policy B *believes*, and a
    sensitivity analysis asks what happens when a belief is wrong, not what
    happens in a different world. Changing the world too would confound the two.
    """
    bundle = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
    edited = copy.deepcopy(bundle)

    if param == "disposal_cost":
        edited["markdown"]["disposal_cost_per_unit"] = float(value)
    elif param == "elasticity_scale":
        # Scales every fitted coefficient. A policy that believes demand is 30%
        # more elastic than it is will cut price where it should not; the point
        # is how much that costs, not whether the belief is true.
        for cell in edited["elasticity"]:
            if cell.get("elasticity") is not None:
                cell["elasticity"] = float(cell["elasticity"]) * float(value)
    elif param == "lead_time_scale":
        pooled = edited["lead_time_days"]
        pooled["pooled_p90"] = float(pooled["pooled_p90"]) * float(value)
        for entry in pooled["by_category"].values():
            entry["p90"] = float(entry["p90"]) * float(value)
    elif param == "dispersion_scale":
        # Forecast error: a larger dispersion makes the newsvendor order for a
        # wider demand distribution, which is what "forecast error x1.5" means
        # to a policy that only sees a mean and a spread.
        edited["demand"]["dispersion_k"] = float(edited["demand"]["dispersion_k"]) * float(value)
    else:
        raise SystemExit(
            f"unknown parameter {param!r} - expected disposal_cost, elasticity_scale, "
            "lead_time_scale or dispersion_scale"
        )
    return edited


def run_setting(
    seed: int,
    param: str,
    value: float,
    days: int = DEFAULT_DAYS,
    pre_days: int = DEFAULT_PRE_DAYS,
) -> pd.DataFrame:
    """One holdout world under one perturbed belief."""
    cfg = load_sim_config()
    scratch = Path(tempfile.mkdtemp(prefix=f"ff-sweep-{param}-{seed}-"))
    try:
        bundle_path = scratch / "bundle.json"
        bundle_path.write_text(json.dumps(perturbed_bundle(param, value)), encoding="utf-8")

        probe = SimulationRun(cfg, seed=seed, days=1, out_dir=scratch, quiet=True)
        calendar = sorted(probe.demand.factors)[:days]
        switch_date = calendar[pre_days]
        treated = assign_treatment(probe.S, seed=seed)
        treated_ids = {probe.store_ids[i] for i in treated}

        policy = HoldoutPolicy(cfg, probe.catalog, treated, switch_date)
        # Only the treated arm's beliefs are perturbed. The holdout is the
        # baseline and has no bundle - perturbing it would change the control
        # group, and a control that moves with the treatment is not a control.
        policy.optimized = OptimizedPolicy(cfg, probe.catalog, bundle_path=bundle_path)

        run = SimulationRun(cfg, seed=seed, days=days, out_dir=scratch, quiet=True, policy=policy)
        run.run()
        frame = pd.DataFrame(run.store_summary)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    frame["seed"] = seed
    frame["param"] = param
    frame["value"] = float(value)
    frame["treated"] = frame["store_id"].isin(treated_ids)
    frame["post"] = frame["date"] >= switch_date
    day_of = {day: i - pre_days for i, day in enumerate(calendar)}
    frame["day_rel"] = frame["date"].map(day_of)
    return frame


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--param", required=True)
    parser.add_argument("--value", type=float, required=True)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--pre-days", type=int, default=DEFAULT_PRE_DAYS)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    frame = run_setting(args.seed, args.param, args.value, args.days, args.pre_days)
    args.out.mkdir(parents=True, exist_ok=True)
    destination = args.out / f"{args.param}_{args.value:g}_seed_{args.seed:03d}.parquet"
    frame.to_parquet(destination, index=False)

    post = frame[frame["post"]]
    totals = post.groupby("treated")[["units_expired", "writeoff_value", "revenue", "cogs"]].sum()
    print(f"\n  seed {args.seed}, {args.param} = {args.value:g}, post-period totals\n")
    print(totals.to_string())
    print(f"\n  {len(frame):,} rows -> {destination.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
