"""Run the holdout with one of Policy B's components switched off (task S5.4).

The experiment says Policy B raises margin about 6% and writes off roughly twice
the value of the baseline. Neither figure says which of its four decisions is
responsible, and they pull in different directions: the newsvendor decides how
much stock exists, markdown and the deal rail decide how it clears, transfers
decide where it sits. The waste accumulation is currently unattributed, and
after the disposal-cost sweep came back negative this is the tool left.

Each run is the same holdout as S5.2 - same 180 days, same 45-day pre-period,
same treatment split from the same generator - with the treated stores running
Policy B minus one decision. Everything about the design is shared with
`simulator/holdout.py` rather than reimplemented, because the control group only
means anything if it is constructed identically.

**`full` is one of the settings.** It runs unablated Policy B, which reproduces
the S5.2 holdout and is the total that the four components have to add up to. A
run without it would leave the parts with nothing to be compared against.

    python -m simulator.ablation --seed 7 --component markdown
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulator.holdout import DEFAULT_DAYS, DEFAULT_PRE_DAYS, run_seed  # noqa: E402
from simulator.policies.ablation import COMPONENTS, AblatedPolicy  # noqa: E402
from simulator.policies.optimized import OptimizedPolicy  # noqa: E402

SETTINGS = ("full", *COMPONENTS)
OUT_DIR = ROOT / "data" / "ablation"


def run_component(
    seed: int,
    component: str,
    days: int = DEFAULT_DAYS,
    pre_days: int = DEFAULT_PRE_DAYS,
) -> pd.DataFrame:
    """One holdout world in which the treated stores run B minus `component`."""
    if component not in SETTINGS:
        raise SystemExit(f"unknown component {component!r} - expected one of {SETTINGS}")

    from simulator.config_loader import load_sim_config

    cfg = load_sim_config()

    def treated_policy(catalog):
        if component == "full":
            return OptimizedPolicy(cfg, catalog)
        return AblatedPolicy(cfg, catalog, component)

    frame = run_seed(seed, days, pre_days, treated_policy=treated_policy)
    frame["component"] = component
    return frame


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--component", required=True, choices=SETTINGS)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--pre-days", type=int, default=DEFAULT_PRE_DAYS)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    frame = run_component(args.seed, args.component, args.days, args.pre_days)
    args.out.mkdir(parents=True, exist_ok=True)
    destination = args.out / f"{args.component}_seed_{args.seed:03d}.parquet"
    frame.to_parquet(destination, index=False)

    post = frame[frame["post"]]
    totals = post.groupby("treated")[["units_expired", "writeoff_value", "revenue", "cogs"]].sum()
    print(f"\n  seed {args.seed}, treated stores running: {args.component}\n")
    print(totals.to_string())
    print(f"\n  {len(frame):,} rows -> {destination.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
