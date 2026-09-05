"""Attribute Policy B's effect to its four components (task S5.4).

Reads the ablation runs and reports, per metric, what each component
contributes. A component's contribution is the effect it *adds*: the full
policy's DiD minus the DiD of the policy with that component handed back to the
baseline. Remove something that helps and the estate does worse, so a positive
contribution means the component improves that metric.

**The gate is that the parts sum to approximately the whole, and the word doing
the work is "approximately".** The components interact - a newsvendor that
orders less leaves less for markdown to clear, so switching either off changes
what the other would have done - and the residual between the sum of the four
contributions and the total effect is that interaction. It is reported rather
than hidden, because its size decides whether a per-component attribution can
honestly be quoted at all. A residual comparable to the total means the
components are not separable and no single number should be put next to any one
of them.

    python tasks.py attribution
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

ABLATION_DIR = ROOT / "data" / "ablation"
FULL = "full"


def load(directory: Path = ABLATION_DIR) -> pd.DataFrame:
    files = sorted(directory.glob("*.parquet"))
    if not files:
        raise SystemExit(
            f"no ablation runs in {directory}\n"
            "  run `python -m simulator.ablation --seed N --component C`, or download\n"
            "  the ablation-panel artifact from an ablation.yml run and unzip it there"
        )
    frame = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    frame["margin"] = frame["revenue"] - frame["cogs"]
    frame = frame.drop_duplicates(
        subset=["component", "seed", "store_id", "day_rel"], ignore_index=True
    )
    if FULL not in set(frame["component"]):
        raise SystemExit(
            "the ablation runs contain no `full` setting, so there is no total for the\n"
            "  components to be compared against - re-run with --component full"
        )
    return frame


def report(frame: pd.DataFrame) -> int:
    components = [c for c in sorted(frame["component"].unique()) if c != FULL]
    seeds = frame["seed"].nunique()
    post_days = int(frame[frame["post"]]["day_rel"].nunique())
    print(
        f"\n  attribution: {len(components)} components, {seeds} seeds, "
        f"{post_days} post-period days\n"
    )
    print("  contribution = the full policy's effect minus the effect without that component")
    print("  positive means the component improves the metric\n")

    header = "".join(f"{c:>16}" for c in components)
    print(f"  {'metric':<16}{'full':>14}{header}{'sum of parts':>15}{'residual':>12}")
    for metric in METRICS:
        total = estimate(frame[frame["component"] == FULL], metric).relative
        contributions = [
            total - estimate(frame[frame["component"] == c], metric).relative for c in components
        ]
        parts = sum(contributions)
        cells = "".join(f"{c:>15.1f}%" for c in contributions)
        residual = total - parts
        print(f"  {metric:<16}{total:>13.1f}%{cells}{parts:>14.1f}%{residual:>11.1f}%")

    print(
        "\n  The residual is the interaction between components, not an error: removing the\n"
        "  newsvendor changes what markdown would have done, and vice versa. A residual\n"
        "  comparable in size to the total means the four are not separable, and no\n"
        "  per-component figure from that row should be quoted on its own."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ablation-dir", type=Path, default=ABLATION_DIR)
    args = parser.parse_args(argv)
    return report(load(args.ablation_dir))


if __name__ == "__main__":
    sys.exit(main())
