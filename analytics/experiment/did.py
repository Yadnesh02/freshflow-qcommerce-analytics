"""Difference-in-differences over the store-level holdout (task S5.2).

Reads what `simulator/holdout.py` wrote and answers two questions: were the two
groups on parallel paths before the switch, and how much did the treatment
change the outcome after it. Imports nothing from `simulator/` - this side of
the boundary analyses, the other side generates.

**One estimate per seed, then a confidence interval across seeds.** The obvious
alternative is to pool every store-day into one regression and cluster the
standard errors by store, and it is the wrong choice here for a reason worth
writing down: there are fourteen stores. Cluster-robust inference is asymptotic
in the number of clusters, and at fourteen it understates the standard error
badly - the classic result is that DiD on serially correlated panel data with
few clusters rejects a true null far more often than its nominal rate. Thirty
seeds are thirty genuinely independent worlds, each with its own randomised
split, so treating each seed's DiD as one observation and taking a t-interval
over the thirty is both simpler and honest about where the replication is.

**The parallel-trends check has to be able to fail.** A pre-period is only worth
having if the groups could be shown *not* to be alike, so the check tests
whether the difference between groups is changing over the pre-period - the
interaction of group and time - rather than whether the groups have the same
level. Two groups at different levels moving in parallel are exactly what DiD
handles; two groups converging are what it cannot.

    python tasks.py did
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
HOLDOUT_DIR = ROOT / "data" / "holdout"

METRICS = ("units_expired", "writeoff_value", "units_lost", "revenue", "margin")
# Below this the pre-period trends are not parallel enough to trust the estimate
# that follows. Not a p-value threshold on its own - see `trends_are_parallel`.
TRENDS_ALPHA = 0.05


@dataclass(frozen=True)
class Estimate:
    metric: str
    effect: float
    relative: float
    ci_low: float
    ci_high: float
    p_value: float
    seeds: int


def load(directory: Path = HOLDOUT_DIR) -> pd.DataFrame:
    """Read either the per-seed files or the combined panel.

    Both spellings exist and both are things a person will actually have.
    `holdout.yml` writes `seed_NNN.parquet` on each matrix runner and then
    uploads a merged `holdout.parquet` from the collecting job - and the merged
    one is what anybody downloading the artifact ends up with. The first version
    of this globbed only `seed_*`, so `tasks.py did` reported "no holdout runs"
    while staring at the file the workflow had just produced.
    """
    files = sorted(directory.glob("seed_*.parquet")) or sorted(directory.glob("*.parquet"))
    if not files:
        raise SystemExit(
            f"no holdout runs in {directory}\n"
            "  run `python -m simulator.holdout --seed N`, or download the\n"
            "  holdout-panel artifact from a holdout.yml run and unzip it there"
        )
    frame = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    frame["margin"] = frame["revenue"] - frame["cogs"]
    # The combined panel and the per-seed files can both be present after a
    # download; dropping exact duplicates keeps that from double-counting seeds
    # into a spuriously tight confidence interval.
    return frame.drop_duplicates(subset=["seed", "store_id", "day_rel"], ignore_index=True)


def seed_did(frame: pd.DataFrame, metric: str) -> float:
    """The 2x2 difference-in-differences for one seed, per store per day.

    Means rather than sums, because the groups need not be the same size - an
    odd store count splits seven and seven here but will not always - and a
    difference of totals would then confound the effect with the split.
    """
    cell = frame.groupby(["post", "treated"])[metric].mean()
    try:
        treated_change = cell[(True, True)] - cell[(False, True)]
        control_change = cell[(True, False)] - cell[(False, False)]
    except KeyError:  # a group is missing entirely
        return float("nan")
    return float(treated_change - control_change)


def seed_pre_trend_gap(frame: pd.DataFrame, metric: str) -> float:
    """Difference in the two groups' pre-period slopes, per day.

    Fitted on daily group means rather than raw store-days: the question is
    whether the *groups* were diverging, and a store-level fit would let one
    store's noise stand in for a trend.
    """
    pre = frame[~frame["post"]]
    if pre.empty:
        return float("nan")
    daily = pre.groupby(["day_rel", "treated"])[metric].mean().unstack()
    if daily.shape[1] < 2 or len(daily) < 3:
        return float("nan")
    x = daily.index.to_numpy(dtype=float)
    slope_treated = np.polyfit(x, daily[True].to_numpy(), 1)[0]
    slope_control = np.polyfit(x, daily[False].to_numpy(), 1)[0]
    return float(slope_treated - slope_control)


def trends_are_parallel(gaps: np.ndarray, alpha: float = TRENDS_ALPHA) -> tuple[bool, float]:
    """Across seeds, is the mean pre-period slope gap indistinguishable from zero?

    Tested across seeds rather than within one, and that is the point of having
    thirty. Within a single world the two groups will always differ a little and
    a test on fourteen stores has almost no power to say whether that matters -
    it would pass by being uninformative, which is the failure mode this project
    keeps finding. Across thirty independent splits, a systematic divergence
    shows up as a mean gap that is reliably non-zero.

    Note this is a test the design wants to *fail to reject*, which is a weak
    form of evidence: not rejecting is not the same as establishing parallelism.
    It is reported with the effect estimate so a reader can see the size of the
    residual gap rather than only its p-value.
    """
    finite = gaps[np.isfinite(gaps)]
    if len(finite) < 3:
        return False, float("nan")
    result = stats.ttest_1samp(finite, 0.0)
    return bool(result.pvalue > alpha), float(result.pvalue)


def estimate(frame: pd.DataFrame, metric: str) -> Estimate:
    """Mean per-seed DiD with a t-interval over seeds."""
    per_seed = np.array(
        [seed_did(g, metric) for _, g in frame.groupby("seed")],
        dtype=float,
    )
    finite = per_seed[np.isfinite(per_seed)]
    n = len(finite)
    mean = float(finite.mean())
    se = float(finite.std(ddof=1) / np.sqrt(n)) if n > 1 else float("nan")
    half = stats.t.ppf(0.975, n - 1) * se if n > 1 else float("nan")
    result = stats.ttest_1samp(finite, 0.0) if n > 1 else None

    # scaled against the control group's post-period level, which is what the
    # treated group would have looked like had nothing changed
    base = frame[frame["post"] & ~frame["treated"]][metric].mean()
    return Estimate(
        metric=metric,
        effect=mean,
        relative=100.0 * mean / base if base else float("nan"),
        ci_low=mean - half,
        ci_high=mean + half,
        p_value=float(result.pvalue) if result is not None else float("nan"),
        seeds=n,
    )


def report(frame: pd.DataFrame) -> int:
    seeds = frame["seed"].nunique()
    stores = frame["store_id"].nunique()
    # Counted per seed, then averaged. Pooling would say all fourteen stores are
    # treated, because the split is re-randomised for every seed and across
    # thirty of them every store is treated at least once. That re-randomisation
    # is deliberate - it stops the estimate being conditional on one particular
    # split - but it makes the pooled count meaningless.
    treated = frame.groupby("seed").apply(
        lambda g: g.loc[g["treated"], "store_id"].nunique(), include_groups=False
    )
    pre_days = frame[~frame["post"]]["day_rel"].nunique()
    post_days = frame[frame["post"]]["day_rel"].nunique()
    print(
        f"\n  holdout DiD: {seeds} seeds, {stores} stores, "
        f"{treated.mean():.0f} treated per seed (split re-randomised each seed), "
        f"{pre_days} pre-period days, {post_days} post-period days\n"
    )

    print("  parallel trends - pre-period slope gap per day, across seeds")
    print(f"  {'metric':<16} {'mean gap/day':>14} {'p':>10}   verdict")
    for metric in METRICS:
        gaps = np.array([seed_pre_trend_gap(g, metric) for _, g in frame.groupby("seed")])
        ok, p = trends_are_parallel(gaps)
        finite = gaps[np.isfinite(gaps)]
        mean_gap = finite.mean() if len(finite) else float("nan")
        verdict = "parallel" if ok else "\033[31mNOT PARALLEL\033[0m"
        print(f"  {metric:<16} {mean_gap:>14,.3f} {p:>10.4f}   {verdict}")

    print("\n  effect - mean per-seed DiD, per store per day, 95% CI over seeds")
    print(f"  {'metric':<16} {'effect':>14} {'vs control':>11} {'95% CI':>28} {'p':>10}")
    for metric in METRICS:
        e = estimate(frame, metric)
        ci = f"[{e.ci_low:>11,.2f}, {e.ci_high:>11,.2f}]"
        print(
            f"  {e.metric:<16} {e.effect:>14,.2f} {e.relative:>10.2f}% {ci:>28} {e.p_value:>10.2e}"
        )

    print(
        "\n  Read the effect as a per-store, per-day change against what the holdout did over the\n"
        "  same window. A metric whose trends are not parallel has no interpretable effect below,\n"
        "  however small its p-value: the estimate then includes whatever was already diverging."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--holdout-dir", type=Path, default=HOLDOUT_DIR)
    args = parser.parse_args(argv)
    return report(load(args.holdout_dir))


if __name__ == "__main__":
    sys.exit(main())
