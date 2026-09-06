"""The pipeline as assets, upstream and downstream of dbt (task S5.6).

Dagster's unit is the *asset* - a table or file that exists - rather than the
task that produces it, and that framing is why this is worth having beyond a
screenshot. `tasks.py` already runs the pipeline; what it cannot say is which
things exist, which are stale, and what would have to be rebuilt to fix a given
one. The asset graph answers that, and the dbt models come in as assets too, so
one lineage runs from the raw parquet through 60-odd models to the decision
tables the app serves.

**Each asset shells out to the same module `tasks.py` calls.** Not a
reimplementation: the orchestration layer decides *when* and *in what order*,
never *how*. A second code path for "the Dagster version" of a step would drift
from the one CI runs, and the two would disagree exactly when it mattered.

**Nothing here is scheduled to run against production data by default.** The
daily schedule in `definitions.py` targets the decision layer - the marts and
optimisers that a real dark-store operation would refresh each morning - and
deliberately excludes the simulator, because regenerating the world every night
would be a strange thing for a business to do and would make every historical
figure irreproducible.
"""

# No `from __future__ import annotations` in this module, deliberately.
# Dagster validates the `context` parameter's annotation against real classes,
# and that import turns every annotation into a string - so the check rejects a
# correctly annotated asset with "Cannot annotate context with type
# AssetExecutionContext", naming the very type it is asking for.

import subprocess
import sys
from pathlib import Path

from dagster import AssetExecutionContext, AssetKey, asset

ROOT = Path(__file__).resolve().parents[2]

# The five Sprint 4 engines, in dependency order. expiry_risk scores the batches
# the others reason about; elasticity is fitted before markdown can read a
# coefficient. The order is the same one warehouse.yml runs, and it is written
# once here rather than restated per asset.
OPTIMISERS = ("expiry-risk", "elasticity", "markdown", "deal-slots", "transfers", "newsvendor")


def _run(context: AssetExecutionContext, *args: str) -> None:
    """Run a tasks.py target, streaming its output into the asset's log."""
    command = [sys.executable, "tasks.py", *args]
    context.log.info(" ".join(command))
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.stdout:
        context.log.info(result.stdout[-4000:])
    if result.returncode != 0:
        context.log.error(result.stderr[-4000:])
        raise RuntimeError(f"`tasks.py {' '.join(args)}` failed with exit {result.returncode}")


@asset(
    group_name="bronze",
    description=(
        "The 12 raw event feeds, 365 days at seed 42. Deterministic from the seed, "
        "and deliberately not on the daily schedule - a business does not regenerate "
        "its own history each night."
    ),
)
def raw_events(context: AssetExecutionContext) -> None:
    _run(context, "simulate", "--days", "365", "--seed", "42")


@asset(
    group_name="forecast",
    deps=[AssetKey(["marts", "agg_store_sku_day"])],
    description="Rolling-origin backtest of the forecast baselines. Writes mart_forecast_accuracy.",
)
def forecast_backtest(context: AssetExecutionContext) -> None:
    _run(context, "backtest")


@asset(
    group_name="forecast",
    deps=[forecast_backtest],
    description=(
        "LightGBM demand forecast. Writes mart_demand_forecast, which expiry risk "
        "needs and which is why the chain cannot start at the optimisers."
    ),
)
def demand_forecast(context: AssetExecutionContext) -> None:
    _run(context, "forecast")


@asset(
    group_name="decisions",
    deps=[demand_forecast],
    description=(
        "The five decision engines, in dependency order: expiry risk, elasticity, "
        "markdown, deal slots, transfers, newsvendor. Writes the four rec_* tables "
        "and mart_price_elasticity."
    ),
)
def decision_tables(context: AssetExecutionContext) -> None:
    for target in OPTIMISERS:
        _run(context, target)


@asset(
    group_name="decisions",
    deps=[decision_tables],
    description=(
        "Sprint 4's fitted parameters, exported for the optimised policy arm. "
        "Policy B reads this bundle and never the rec_* tables - those were fitted "
        "on the whole year, so a policy reading its own row on day 120 would be "
        "reading day 300."
    ),
)
def policy_bundle(context: AssetExecutionContext) -> None:
    _run(context, "policy-bundle")


@asset(
    group_name="serving",
    deps=[decision_tables],
    description=(
        "The <80MB slice the deployed app reads. Building it locally does not change "
        "what the app serves until `tasks.py publish-demo` uploads it."
    ),
)
def demo_slice(context: AssetExecutionContext) -> None:
    _run(context, "demo-slice")
