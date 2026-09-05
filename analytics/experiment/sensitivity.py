"""Which findings survive a wrong belief (task S5.3).

Runs `did.estimate` once per swept setting and lays the results side by side, so
a conclusion that only holds at one parameter value is visible as one that
changes sign or loses its interval partway along a row.

**A finding survives if it keeps its sign and its significance across the
range.** That is a deliberately blunt rule and it is stated here rather than
left implicit, because "sensitivity analysis" otherwise degrades into a table
nobody draws a conclusion from. Two of this project's headline results were
already known not to survive before this was written - the delivery-cost
crossover and the markdown disposal cost - and the point of the exercise is to
find out which others join them.

**The delivery cost is not swept here.** It changes how customer contribution is
computed from a world that already exists, not how any store behaves, so it
needs no simulation and belongs over the built warehouse. Sweeping it here would
imply a compute cost it does not have and, worse, imply the two kinds of
sensitivity are the same kind of claim.

    python tasks.py sensitivity
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.experiment.did import METRICS, estimate  # noqa: E402

SWEEP_DIR = ROOT / "data" / "sweep"


def load(directory: Path = SWEEP_DIR) -> pd.DataFrame:
    files = sorted(directory.glob("*.parquet"))
    if not files:
        raise SystemExit(
            f"no sweep runs in {directory}\n"
            "  run `python -m simulator.sweep --seed N --param P --value V`, or\n"
            "  download the sweep-panel artifact from a sweep.yml run and unzip it there"
        )
    frame = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    frame["margin"] = frame["revenue"] - frame["cogs"]
    frame = frame.drop_duplicates(
        subset=["param", "value", "seed", "store_id", "day_rel"], ignore_index=True
    )

    # Runs of different lengths must not be compared. The sweep directory is
    # just a folder, so a file left over from an earlier configuration joins the
    # table silently and shifts one column - and a shifted column reads as a
    # parameter response, which is the one conclusion this file exists to draw.
    # Caught by shape rather than by trusting a filename.
    shapes = frame.groupby(["param", "value"]).agg(
        first_day=("day_rel", "min"), last_day=("day_rel", "max"), seeds=("seed", "nunique")
    )
    if len(set(zip(shapes["first_day"], shapes["last_day"], strict=True))) > 1:
        raise SystemExit(
            "the sweep directory mixes runs of different lengths, which cannot be compared:\n"
            + shapes.to_string()
            + "\n\n  delete the odd ones out, or re-run them at a single horizon"
        )
    return frame


def survives(estimates: list) -> bool:
    """Same sign and significant at every setting of the parameter.

    An effect that is significant at one end of a range and not at the other has
    not been shown to be robust; it has been shown to depend on the parameter.
    That is a finding in its own right and the table reports it as one.
    """
    if len(estimates) < 2:
        return False
    signs = {e.effect > 0 for e in estimates}
    return len(signs) == 1 and all(e.p_value < 0.05 for e in estimates)


def report(frame: pd.DataFrame) -> int:
    params = sorted(frame["param"].unique())
    # Read from the data, not restated from the workflow. A header that says
    # "180-day" because somebody typed it stops being true the first time anyone
    # runs a shorter sweep, and is then most confident exactly when it is wrong.
    post_days = int(frame[frame["post"]]["day_rel"].nunique())
    pre_days = int(frame[~frame["post"]]["day_rel"].nunique())
    per_setting = frame.groupby(["param", "value"])["seed"].nunique()
    seeds = (
        f"{per_setting.min()}"
        if per_setting.min() == per_setting.max()
        else f"{per_setting.min()}-{per_setting.max()}"
    )
    print(
        f"\n  sensitivity: {len(params)} parameters, {seeds} seeds per setting, "
        f"{pre_days} pre / {post_days} post days\n"
    )

    for param in params:
        block = frame[frame["param"] == param]
        values = sorted(block["value"].unique())
        print(f"  --- {param} ---")
        header = "".join(f"{v:>16g}" for v in values)
        print(f"  {'metric':<16}{header}   survives")
        for metric in METRICS:
            estimates = [estimate(block[block["value"] == v], metric) for v in values]
            cells = "".join(f"{e.relative:>15.1f}%" for e in estimates)
            # "not swept" is not "did not survive". A parameter given one value
            # produces a table identical in shape to a swept one, and printing
            # NO against it reads as a finding failing when nothing was varied.
            # The first real run showed exactly that for dispersion_scale and
            # lead_time_scale, both of which carry a single setting.
            if len(values) < 2:
                verdict = "not swept"
            else:
                verdict = "yes" if survives(estimates) else "\033[31mNO\033[0m"
            print(f"  {metric:<16}{cells}   {verdict}")
        print()

    print(
        "  Cells are the DiD effect as a percentage of what the holdout did over the same window.\n"
        "  'survives' means the sign never flips and every setting clears p < 0.05. A NO is a\n"
        "  finding, not a failure: it says the conclusion is a statement about the parameter."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sweep-dir", type=Path, default=SWEEP_DIR)
    args = parser.parse_args(argv)
    return report(load(args.sweep_dir))


if __name__ == "__main__":
    sys.exit(main())
