"""The readout table from plan §10, filled with real numbers (task S5.5).

Reads the holdout panel and emits one tidy row per metric - Policy A, Policy B,
the difference, a 95% interval and whether it is significant - as parquet under
`data/experiment/`, where a dbt source picks it up and `mart_experiment_readout`
serves it to the Executive page. That chain is what G5 asks for: every number on
the résumé traceable to a table, and the table traceable to a workflow run.

**Four of the plan's six rows are filled, and the other two are named rather
than fudged.** The plan was written before the experiment existed and asked for
six metrics; two of them turn out not to be A/B outcomes at all:

  - **90-day retention** is a customer-level measure, and the holdout randomises
    *stores*. A customer is not in an arm, their store is, so a retention
    difference between arms would be a statement about which stores were drawn
    rather than about the policy. Measuring it properly needs customer-level
    assignment, which is a different experiment.
  - **Forecast WAPE** is a property of Policy B's forecast, and Policy A does not
    forecast. There is no Policy A column to put next to it. It belongs in
    `mart_forecast_accuracy`, where S3.2 already reports it by ABC-XYZ class.

Writing "—" in those rows and saying why is the honest version. Inventing a
comparison for them would be exactly the back-fitting the plan warns against.

**Significance comes from the interval, not from a separate test.** The interval
is over seeds, so a metric whose CI excludes zero is one where the sign held
across independent worlds - which is the claim being made. Reporting a p-value
alongside would imply a second, independent piece of evidence.

    python tasks.py readout
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.experiment.did import estimate, load  # noqa: E402

OUT_DIR = ROOT / "data" / "experiment"
DESTINATION = OUT_DIR / "readout.parquet"

# Each row is (label, how to build the per-store-day series, unit). The series
# are ratios where the plan asks for a rate, so that a bigger store does not
# weigh more than a smaller one simply for being bigger.
ROWS = (
    ("wastage_rate_value", "writeoff_value / nullif(revenue, 0)", "ratio"),
    ("gm_awm_pct", "(revenue - cogs - writeoff_value) / nullif(revenue, 0)", "ratio"),
    ("availability_pct", "1 - units_lost / nullif(units_sold + units_lost, 0)", "ratio"),
    ("markdown_subsidy_inr", "markdown_subsidy", "money"),
)

# Named, not filled. See the module docstring - each is a metric the plan asked
# for that the holdout cannot answer, and the reason differs.
NOT_APPLICABLE = {
    "retention_90d": (
        "the holdout randomises stores, not customers, so a retention gap between arms "
        "describes which stores were drawn rather than the policy"
    ),
    "forecast_wape": (
        "a property of Policy B's forecast; Policy A does not forecast, so there is no "
        "A column - see mart_forecast_accuracy, which reports it by ABC-XYZ class"
    ),
}


def _derive(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["wastage_rate_value"] = out["writeoff_value"] / out["revenue"].replace(0, pd.NA)
    out["gm_awm_pct"] = (out["revenue"] - out["cogs"] - out["writeoff_value"]) / out[
        "revenue"
    ].replace(0, pd.NA)
    served = out["units_sold"] + out["units_lost"]
    out["availability_pct"] = 1 - out["units_lost"] / served.replace(0, pd.NA)
    # A panel generated before a metric existed simply does not carry it, and
    # that is a normal state rather than an error: the holdout runs are stored
    # artifacts, so any metric added later postdates every panel already on
    # disk. Reported as unavailable, with the run that would supply it named -
    # crashing here would make adding a metric a breaking change to every
    # result anybody had already downloaded.
    if "markdown_subsidy" in out.columns:
        out["markdown_subsidy_inr"] = out["markdown_subsidy"]
    return out


def build(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per metric: Policy A, Policy B, delta, interval, significance."""
    derived = _derive(frame)
    post = derived[derived["post"]]
    rows = []
    for name, _expression, unit in ROWS:
        if name not in derived.columns:
            rows.append(
                {
                    "metric": name,
                    "unit": "n/a",
                    "policy_a": None,
                    "policy_b": None,
                    "delta": None,
                    "ci_low": None,
                    "ci_high": None,
                    "significant": None,
                    "seeds": None,
                    "not_applicable_reason": (
                        "this holdout panel predates the metric - re-run holdout.yml to "
                        "produce one that carries it"
                    ),
                }
            )
            continue
        policy_a = float(post[~post["treated"]][name].mean())
        policy_b = float(post[post["treated"]][name].mean())
        result = estimate(derived, name)
        rows.append(
            {
                "metric": name,
                "unit": unit,
                "policy_a": policy_a,
                "policy_b": policy_b,
                "delta": result.effect,
                "ci_low": result.ci_low,
                "ci_high": result.ci_high,
                "significant": bool(result.ci_low > 0 or result.ci_high < 0),
                "seeds": result.seeds,
                "not_applicable_reason": None,
            }
        )
    for name, reason in NOT_APPLICABLE.items():
        rows.append(
            {
                "metric": name,
                "unit": "n/a",
                "policy_a": None,
                "policy_b": None,
                "delta": None,
                "ci_low": None,
                "ci_high": None,
                "significant": None,
                "seeds": None,
                "not_applicable_reason": reason,
            }
        )
    return pd.DataFrame(rows)


def _unfilled(reason) -> bool:
    """Whether this row has a stated reason for being blank.

    Type-checked rather than tested for truthiness: a `None` written into a
    column that also holds strings comes back from pandas as NaN, and `if nan:`
    is True - so every computed row was printed as unfilled *and* listed among
    the reasons, with "nan" as its explanation.
    """
    return isinstance(reason, str) and bool(reason)


def render(table: pd.DataFrame) -> None:
    # ASCII rather than an em dash. This prints to a Windows console as well as
    # to a CI log, and the em dash arrived in the first as a replacement
    # character - a table of unreadable glyphs where the blanks should be.
    blank = "-"
    print(f"\n  {'metric':<22}{'Policy A':>12}{'Policy B':>12}{'delta':>12}{'95% CI':>26}  sig")
    for row in table.itertuples():
        if _unfilled(row.not_applicable_reason):
            print(f"  {row.metric:<22}{blank:>12}{blank:>12}{blank:>12}{blank:>26}  {blank}")
            continue
        fmt = (lambda v: f"{v:,.2f}") if row.unit == "money" else (lambda v: f"{v:.4f}")
        ci = f"[{fmt(row.ci_low)}, {fmt(row.ci_high)}]"
        mark = "yes" if row.significant else "no"
        print(
            f"  {row.metric:<22}{fmt(row.policy_a):>12}{fmt(row.policy_b):>12}"
            f"{fmt(row.delta):>12}{ci:>26}  {mark}"
        )
    unfilled = [r for r in table.itertuples() if _unfilled(r.not_applicable_reason)]
    if unfilled:
        print("\n  not filled, and why:")
        for row in unfilled:
            print(f"    {row.metric}: {row.not_applicable_reason}")
    print(
        "\n  Policy A and Policy B are post-period means per store-day. The delta is the\n"
        "  difference-in-differences, which removes the pre-period gap between the groups and\n"
        "  anything that hit both on the same day - so it is not policy_b minus policy_a."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--holdout-dir", type=Path, default=ROOT / "data" / "holdout")
    parser.add_argument("--out", type=Path, default=DESTINATION)
    args = parser.parse_args(argv)

    table = build(load(args.holdout_dir))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(args.out, index=False)
    render(table)
    print(f"\n  {len(table)} rows -> {args.out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
