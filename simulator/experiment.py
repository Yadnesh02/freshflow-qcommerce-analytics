"""Run one seed through both policy arms and record what each did (task S5.1).

One seed, two arms, the same days, and a row per arm per day. Thirty of these
runs make the experiment; `readout.py` aggregates them.

**Why this file runs one seed rather than looping over thirty.** The arms are
independent given a seed, so the loop belongs in a job matrix rather than in
Python: `.github/workflows/experiment.yml` fans out one runner per seed. That
turns three hours of serial simulation into a few minutes, and it costs nothing
because the repository is public. It is also the reason this module writes one
parquet per seed rather than appending to a shared file - thirty runners cannot
share a writer, and a merge step that concatenates thirty small files is simpler
than any locking scheme.

**What the harness needs, and does not need.** It reads
`simulator/config/policy_bundle.json`, which is committed, and nothing else. No
warehouse, no marts, no 1.8 GB download. That is a consequence of S4.6's
decision that Policy B reads a parameter bundle rather than the `rec_*` tables:
those were fitted on the whole year, so a policy reading its own row on day 120
would be reading day 300, and the experiment would measure look-ahead rather
than a policy.

**Common random numbers, and what the phrase can honestly mean here.** It cannot
mean the arms see identical demand: Policy B changes prices, demand is
price-elastic, so identical demand would prove only that the policy did nothing.
It means the two arms draw from the *same underlying random numbers*, so that a
difference between them is the policy rather than the draw. Since `04737af` each
component derives its generator from `(seed, day, component)`, which makes that
structural - the demand stream on day 120 is the same object no matter what the
policy did on day 119. Before that change the arms shared draws for exactly one
day, and every day after was contaminated.

**Why this lives in `simulator/` and not `analytics/`.** `analytics/` may never
import from `simulator/` - it is the rule the project's credibility rests on,
because it is what makes "you generated the data, so of course it worked" a
design decision rather than an admission. Running two policy arms *is* simulator
work: it needs `SimulationRun`, the config and the policies. Only the analysis of
what came out belongs on the far side of that line, and that is
`analytics/experiment/readout.py`, which reads these parquet files and imports
nothing from here. `tests/test_import_boundary.py` caught this file on the wrong
side of it, which is exactly the objection the rule exists to answer.

    python -m simulator.experiment --seed 7 --days 90 --out data/experiment
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
from simulator.run import SimulationRun  # noqa: E402

ARMS = ("baseline", "optimized")
DEFAULT_DAYS = 90
OUT_DIR = ROOT / "data" / "experiment"


def run_arm(seed: int, days: int, policy: str) -> pd.DataFrame:
    """One arm of one seed. Returns the per-day summary.

    The bronze feeds go to a temporary directory and are thrown away. The
    experiment reads the day counters, not the event stream, and thirty seeds
    times two arms times a year of parquet would be tens of gigabytes of files
    nothing downstream opens.
    """
    scratch = Path(tempfile.mkdtemp(prefix=f"ff-exp-{policy}-{seed}-"))
    try:
        run = SimulationRun(
            load_sim_config(),
            seed=seed,
            days=days,
            out_dir=scratch,
            policy_name=policy,
            quiet=True,
        )
        frame = run.run()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return frame.assign(seed=seed, arm=policy)


def common_random_numbers_hold(seed: int, day_ordinal: int) -> bool:
    """Do both arms draw the same numbers for the same component on the same day?

    Checked by construction rather than by simulation: two runs differing only
    in policy must produce identical generators for the exogenous components.
    Cheap enough to assert on every seed, which is the point - a regression in
    the substream layout would otherwise surface as a quietly noisier estimate
    rather than as a failure.
    """
    cfg = load_sim_config()
    runs = [
        SimulationRun(cfg, seed=seed, days=1, out_dir=Path(tempfile.gettempdir()), policy_name=arm)
        for arm in ARMS
    ]
    exogenous = (
        SimulationRun.DEMAND,
        SimulationRun.BASKETS,
        SimulationRun.FULFIL,
        SimulationRun.CLICKS,
        SimulationRun.SUPPLY,
    )
    return all(
        (
            runs[0]._substream(day_ordinal, component).random(8)
            == runs[1]._substream(day_ordinal, component).random(8)
        ).all()
        for component in exogenous
    )


def run_seed(seed: int, days: int) -> pd.DataFrame:
    """Both arms of one seed, stacked."""
    return pd.concat([run_arm(seed, days, arm) for arm in ARMS], ignore_index=True)


def summarise(frame: pd.DataFrame) -> pd.DataFrame:
    """Totals per arm, for the run log. The readout does the real aggregation."""
    return frame.groupby("arm", as_index=False).agg(
        units_demanded=("units_demanded", "sum"),
        units_sold=("units_sold", "sum"),
        units_expired=("units_expired", "sum"),
        writeoff_value=("writeoff_value", "sum"),
        revenue=("revenue", "sum"),
        cogs=("cogs", "sum"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    first_day = SimulationRun(
        load_sim_config(), seed=args.seed, days=1, out_dir=Path(tempfile.gettempdir())
    )
    probe_ordinal = min(first_day.demand.factors).toordinal()
    if not common_random_numbers_hold(args.seed, probe_ordinal):
        print(
            "\n\033[31mcommon random numbers do not hold\033[0m\n"
            "  the two arms draw different numbers for the same component on the same day,\n"
            "  so any difference between them is partly the draw rather than the policy.\n"
            "  See SimulationRun._substream - this is meant to be structural.",
            file=sys.stderr,
        )
        return 1

    print(f"\n  seed {args.seed}, {args.days} days, arms {' and '.join(ARMS)}")
    frame = run_seed(args.seed, args.days)

    args.out.mkdir(parents=True, exist_ok=True)
    destination = args.out / f"seed_{args.seed:03d}.parquet"
    frame.to_parquet(destination, index=False)

    totals = summarise(frame)
    print()
    print(totals.to_string(index=False))
    # relative_to raises when --out points outside the repo, which it does on a
    # runner writing to a matrix-scoped directory, and losing the run to a
    # ValueError in a print statement would be a poor trade for a shorter path.
    try:
        shown = destination.relative_to(ROOT)
    except ValueError:
        shown = destination
    print(f"\n  {len(frame):,} rows -> {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
