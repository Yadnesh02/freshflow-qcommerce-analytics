"""Run one world with a randomised store-level holdout (task S5.2).

One simulation, not two. Half the stores switch to Policy B on day 46; the rest
stay on the baseline for all 180 days. That is the difference from
`simulator/experiment.py`, which runs the same world twice under two different
policies - and the difference is the whole reason both exist. S5.1 answers "what
would Policy B do if we ran it everywhere", which is clean and unobservable.
This answers "what happens if we roll it out to half the estate", which is the
question an operator actually faces and the only one with a control group in it.

**Why 180 days, 45 before the switch and 135 after.** S5.1's 30-seed run showed
the effect had not stabilised by day 90: the expiry advantage ran -61.9% in the
first fortnight and +16.0% in the last, still moving. A 90-day holdout would
therefore measure mostly transient. 135 post-period days give the system time to
reach whatever it settles at, and 45 pre-period days give the parallel-trends
check enough observations to be worth running - a handful of days would let any
two groups look parallel.

**What the output is.** One row per store per day, carrying the treatment flag
and the period, which is exactly the shape a difference-in-differences consumes.
The estimator lives in `analytics/experiment/did.py`, on the far side of the
import boundary: this file generates, that one analyses, and nothing in
`analytics/` may import from here.

    python -m simulator.holdout --seed 7 --out data/holdout
"""

from __future__ import annotations

import argparse
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
from simulator.run import SimulationRun  # noqa: E402

DEFAULT_DAYS = 180
DEFAULT_PRE_DAYS = 45
OUT_DIR = ROOT / "data" / "holdout"


def run_seed(
    seed: int,
    days: int = DEFAULT_DAYS,
    pre_days: int = DEFAULT_PRE_DAYS,
    treated_policy=None,
) -> pd.DataFrame:
    """One holdout world. Returns per-store, per-day outcomes.

    `treated_policy` is a callable taking the catalogue and returning whatever
    the treated stores should run - Policy B by default, and one of S5.4's
    ablations otherwise. Passed in rather than branched on here so the holdout
    construction has exactly one implementation: the switch date, the treatment
    split and the per-store framing are the parts that must be identical across
    every design that compares against this control, and a second copy of them
    is a second thing to keep in step.
    """
    cfg = load_sim_config()
    scratch = Path(tempfile.mkdtemp(prefix=f"ff-holdout-{seed}-"))
    try:
        # A throwaway run to learn the calendar and the catalogue before the real
        # one is constructed - the switch date has to be an actual simulated day,
        # and the policy needs the catalogue the run will use.
        probe = SimulationRun(cfg, seed=seed, days=1, out_dir=scratch, quiet=True)
        calendar = sorted(probe.demand.factors)[:days]
        if len(calendar) <= pre_days:
            raise SystemExit(f"{days} days is not longer than the {pre_days}-day pre-period")
        switch_date = calendar[pre_days]

        treated = assign_treatment(probe.S, seed=seed)
        treated_ids = {probe.store_ids[i] for i in treated}

        policy = HoldoutPolicy(cfg, probe.catalog, treated, switch_date)
        if treated_policy is not None:
            policy.optimized = treated_policy(probe.catalog)

        run = SimulationRun(
            cfg,
            seed=seed,
            days=days,
            out_dir=scratch,
            quiet=True,
            policy=policy,
        )
        run.run()
        frame = pd.DataFrame(run.store_summary)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    frame["seed"] = seed
    frame["treated"] = frame["store_id"].isin(treated_ids)
    frame["post"] = frame["date"] >= switch_date
    frame["switch_date"] = switch_date
    # Day index relative to the switch: negative in the pre-period, zero on the
    # day of the switch. The trends check reads this rather than raw dates, so
    # seeds with different calendars still stack.
    day_of = {day: i - pre_days for i, day in enumerate(calendar)}
    frame["day_rel"] = frame["date"].map(day_of)
    return frame


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--pre-days", type=int, default=DEFAULT_PRE_DAYS)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    frame = run_seed(args.seed, args.days, args.pre_days)
    args.out.mkdir(parents=True, exist_ok=True)
    destination = args.out / f"seed_{args.seed:03d}.parquet"
    frame.to_parquet(destination, index=False)

    groups = frame.groupby(["post", "treated"])[
        ["units_expired", "writeoff_value", "revenue"]
    ].sum()
    stores = frame.groupby("treated")["store_id"].nunique()
    switch = frame["switch_date"].iloc[0]
    print(
        f"\n  seed {args.seed}: {args.days} days, switch on {switch}"
        f"\n  {int(stores.get(True, 0))} treated stores, {int(stores.get(False, 0))} holdout\n"
    )
    print(groups.to_string())
    try:
        shown = destination.relative_to(ROOT)
    except ValueError:
        shown = destination
    print(f"\n  {len(frame):,} rows -> {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
