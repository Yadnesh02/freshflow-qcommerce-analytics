"""Every published deal figure, in one table, computed once (task D6).

The three pages D6 ships cannot compute any of this. `serving/web/` is
forbidden from importing a database driver - that is gate G3, and a test walks
the syntax tree to keep it true - so a page gets its numbers from the metrics
API or not at all. And the API cannot compute them either: `uplift.py` fits four
estimators and a causal tree, which is seconds of CPU, not milliseconds of
request.

So this follows `analytics/experiment/readout.py` exactly, because that module
solved the same problem for the policy backtest. Statistics are computed here,
where the estimators live beside their tests; the result is written as parquet;
a thin dbt model gives it ordering and display formatting; the API serves the
mart; the page renders it. Nothing recomputes a confidence interval in SQL.

**One row per published figure, long rather than wide.** A wide table would need
a column per metric and a migration every time a chapter adds one. Long means
D6's pages select the rows they want and an unfamiliar figure arrives with its
own unit, interval and provenance attached rather than needing a lookup
somewhere else.

**`source` is not decoration.** Every row names the module that produced it, so
a reader who doubts a number on screen can find the code that made it without
grepping. That is the same promise `show_query` makes for metric tiles, kept for
figures that do not come from the registry.

    python tasks.py deal-readout
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from analytics.deal import cannibalisation as cann
from analytics.deal.attach import itt_effects, naive_attach, per_customer
from analytics.deal.causal_tree import CausalTree
from analytics.deal.pnl import DEAL_PRICE, standalone_pnl
from analytics.deal.retention import _compare
from analytics.deal.retention import outcomes as retention_outcomes
from analytics.deal.uplift import (
    CATEGORICAL,
    CUSTOMER_VALUE,
    FEATURES,
    SEED,
    TEST_FRACTION,
    build_panel,
    qini,
    t_learner,
    targeting_curve,
    x_learner,
)

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)
OUT_DIR = ROOT / "data" / "deal"
DESTINATION = OUT_DIR / "readout.parquet"

# Which page each figure belongs on. Carried in the data rather than decided in
# the page, so adding a figure is one row here instead of an edit in two places.
PAYS, WHO, TOMORROW = "does_it_pay", "who_should_see_it", "what_runs_tomorrow"


def _row(
    page: str,
    metric: str,
    label: str,
    value: float | None,
    unit: str,
    source: str,
    ci_low: float | None = None,
    ci_high: float | None = None,
    note: str | None = None,
) -> dict:
    return {
        "page": page,
        "metric": metric,
        "label": label,
        "value": value,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "unit": unit,
        "source": source,
        "note": note,
    }


def build(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Every figure the three pages show, with its interval and its provenance."""
    rows: list[dict] = []

    # ---------------------------------------------------------- does it pay
    pnl = standalone_pnl(con)
    rows += [
        _row(
            PAYS,
            "deal_units",
            "Units sold on the rail",
            pnl.units,
            "count",
            "analytics/deal/pnl.py",
        ),
        _row(PAYS, "deal_revenue", "Revenue", pnl.revenue, "inr", "analytics/deal/pnl.py"),
        _row(PAYS, "deal_cogs", "Cost of goods", pnl.cogs, "inr", "analytics/deal/pnl.py"),
        _row(
            PAYS,
            "deal_margin",
            "Margin on the rail itself",
            pnl.margin,
            "inr",
            "analytics/deal/pnl.py",
            note=f"realised price Rs {pnl.price_per_unit:.2f} against a Rs {DEAL_PRICE:.2f} deal",
        ),
    ]

    naive = naive_attach(con)
    rows.append(
        _row(
            PAYS,
            "naive_attach",
            "Attach, per deal order (self-selected)",
            naive["naive_attach"],
            "inr",
            "analytics/deal/attach.py",
            note="Not causal. Deal-takers are not a random sample of shoppers.",
        )
    )

    panel = per_customer(con)
    itt = {e.metric: e for e in itt_effects(panel)}
    for metric, label in (
        ("orders", "Orders per customer"),
        ("revenue", "Revenue per customer"),
        ("nondeal_margin", "Margin on the rest of the basket, per customer"),
        ("deal_margin", "Subsidy, per customer"),
        ("margin", "Total margin effect, per customer"),
    ):
        e = itt[metric]
        rows.append(
            _row(
                PAYS,
                f"itt_{metric}",
                label,
                e.diff,
                "count" if metric == "orders" else "inr",
                "analytics/deal/attach.py",
                e.ci_low,
                e.ci_high,
                note="Intent to treat against D1's randomised holdout. "
                "Customers who never ordered are included, as zeros.",
            )
        )

    subsidy = itt["deal_margin"].diff * itt["deal_margin"].n_exposed
    total = itt["margin"].diff * itt["margin"].n_exposed
    rows.append(
        _row(
            PAYS,
            "return_per_rupee",
            "Return per rupee of subsidy",
            -total / subsidy if subsidy else None,
            "ratio",
            "analytics/deal/attach.py",
            -itt["margin"].ci_low * itt["margin"].n_exposed / subsidy if subsidy else None,
            -itt["margin"].ci_high * itt["margin"].n_exposed / subsidy if subsidy else None,
            note="The interval crosses 1x on short windows, which is the honest headline.",
        )
    )

    try:
        summary = cann.summarise(cann.event_panel(con))
        share = (
            -summary["net_cannibalisation"] / summary["in_block_lift"]
            if summary["in_block_lift"]
            else None
        )
        rows.append(
            _row(
                PAYS,
                "cannibalisation_share",
                "Share of a slot's lift paid for by the days around it",
                share,
                "ratio",
                "analytics/deal/cannibalisation.py",
                note="Already inside the causal attach figure above - reported, not added.",
            )
        )
    except ValueError:
        pass

    churn = _compare(retention_outcomes(con), "churned")
    rows += [
        _row(
            PAYS,
            "churn_effect",
            "Effect on churn",
            churn.diff,
            "rate",
            "analytics/deal/retention.py",
            churn.ci_low,
            churn.ci_high,
            note="Positive means the rail LOSES customers.",
        ),
        _row(
            PAYS,
            "churn_mde",
            "Smallest churn effect this design could detect",
            churn.mde,
            "rate",
            "analytics/deal/retention.py",
            note="A null without this beside it is a shrug, not a finding.",
        ),
    ]

    # ------------------------------------------------------- who should see it
    # build_panel, not the attach panel above: the uplift panel carries the
    # pre-period features and the treated flag, and the attach one carries
    # neither. They are different questions over the same customers.
    uplift_panel = build_panel(con)
    rng = np.random.default_rng(SEED)
    held = rng.random(len(uplift_panel)) < TEST_FRACTION
    train, score = uplift_panel[~held].copy(), uplift_panel[held].copy()
    treated = score["treated"].to_numpy()
    margin_out = score["margin_post"].to_numpy()

    numeric = [f for f in FEATURES if f not in CATEGORICAL]
    estimators = {
        "t_learner": lambda: t_learner(train, score, "margin_post"),
        "x_learner": lambda: x_learner(train, score, "margin_post"),
        "causal_tree": lambda: (
            CausalTree(features=numeric, seed=SEED).fit(train, "margin_post").predict(score)
        ),
    }
    predictions = {}
    for name, fn in estimators.items():
        predictions[name] = fn()
        result = qini(predictions[name], treated, margin_out)
        rows.append(
            _row(
                WHO,
                f"qini_{name}",
                f"Qini, {name.replace('_', '-')}",
                result.coefficient,
                "number",
                "analytics/deal/uplift.py",
                note="Above zero beats random targeting; below it does not.",
            )
        )

    churn_pred = CausalTree(features=numeric, seed=SEED).fit(train, "churned").predict(score)
    net_pred = predictions["causal_tree"] - churn_pred * CUSTOMER_VALUE
    net_out = margin_out - score["churned"].to_numpy() * CUSTOMER_VALUE
    curve = targeting_curve(net_pred, treated, net_out)
    for _, r in curve.iterrows():
        rows.append(
            _row(
                WHO,
                f"targeting_{int(r['targeted'] * 100)}",
                f"Net gain targeting the top {r['targeted']:.0%}",
                r["gain"],
                "inr",
                "analytics/deal/uplift.py",
                note=f"vs random: {r['vs_random']:,.0f}. "
                f"Read the Qini, not one point on this curve.",
            )
        )

    rows.append(
        _row(
            WHO,
            "customer_value",
            "Assumed value of a retained customer",
            CUSTOMER_VALUE,
            "inr",
            "analytics/deal/uplift.py",
            note="AN ASSUMPTION. Every net figure above is proportional to it.",
        )
    )

    # --------------------------------------------------- what runs tomorrow
    slots = con.execute(
        """
        select count(*) from information_schema.tables
        where table_schema = 'marts' and table_name = 'rec_deal_slot'
        """
    ).fetchone()[0]
    if slots:
        booked = con.execute("select count(*), sum(slot_value) from marts.rec_deal_slot").fetchone()
        rows += [
            _row(
                TOMORROW,
                "slots_recommended",
                "Slots recommended",
                booked[0],
                "count",
                "analytics/optimization/deal_slots.py",
            ),
            _row(
                TOMORROW,
                "slot_value_total",
                "Expected value of those slots",
                float(booked[1] or 0.0),
                "inr",
                "analytics/optimization/deal_slots.py",
                note="Scored on the conservative end of the causal basket margin (D5).",
            ),
        ]

    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE)
    ap.add_argument("--out", type=Path, default=DESTINATION)
    args = ap.parse_args(argv)

    con = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        table = build(con)
    finally:
        con.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(args.out, index=False)

    print(f"\n  {len(table)} figures -> {args.out}")
    for page, chunk in table.groupby("page", sort=False):
        print(f"\n  {page}")
        for _, r in chunk.iterrows():
            value = "-" if pd.isna(r["value"]) else f"{r['value']:,.4g}"
            print(f"    {r['metric']:<24} {value:>14}  {r['unit']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
