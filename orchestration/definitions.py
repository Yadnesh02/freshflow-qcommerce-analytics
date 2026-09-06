"""The Dagster definitions: assets, checks and the daily schedule (task S5.6).

    python tasks.py dagster        # then open http://127.0.0.1:3000

**The dbt project comes in as assets, not as one opaque step.** `dagster_dbt`
reads the manifest, so every model is a node with its real upstream edges and
the lineage runs unbroken from the raw parquet through staging and the marts to
the decision tables. A single "run dbt" asset would draw a graph that looks tidy
and tells you nothing about what to rebuild when one model is wrong.

**Asset checks are where this earns its keep.** A schedule that refreshes the
marts every morning and never looks at them is a cron job with a nicer
interface. Two checks run against the warehouse after it is built: the seven
anchors, and the foreign-key relationship that caught the phantom `FF-LPA-00`
store when every total still tied. Both already exist - the check wraps them
rather than restating them, so there is one definition of each.

**The schedule excludes the simulator on purpose.** A daily refresh of the
decision layer is what a dark-store operation would actually run; regenerating
the world every night would make every historical figure irreproducible, and
`raw_events` is materialisable on demand for when the dataset genuinely changes.
"""

# No `from __future__ import annotations` in this module, deliberately.
# Dagster validates the `context` parameter's annotation against real classes,
# and that import turns every annotation into a string - so the check rejects a
# correctly annotated asset with "Cannot annotate context with type
# AssetExecutionContext", naming the very type it is asking for.

import os
import shutil
import subprocess
import sys
from pathlib import Path

from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    AssetExecutionContext,
    AssetKey,
    AssetSelection,
    Definitions,
    ScheduleDefinition,
    asset_check,
    define_asset_job,
)
from dagster_dbt import DbtCliResource, DbtProject, dbt_assets

from orchestration.assets.pipeline import (
    decision_tables,
    demand_forecast,
    demo_slice,
    forecast_backtest,
    policy_bundle,
    raw_events,
)

ROOT = Path(__file__).resolve().parents[1]
TRANSFORM = ROOT / "transform"

# dbt is installed into the virtualenv, not onto PATH, and DbtCliResource
# validates the executable at construction - so a bare "dbt" fails at import
# time with a pydantic error about a missing binary, before Dagster has drawn
# anything. Resolved from the running interpreter so it works in the venv, in a
# Codespace and on a runner without any of them needing PATH set up.
DBT_EXECUTABLE = shutil.which("dbt") or str(Path(sys.executable).with_name("dbt"))

# Points at the committed manifest rather than regenerating one on import.
# Loading definitions must not require a warehouse: `dagster dev` is often the
# first thing somebody runs on a fresh clone, and a parse step that shells out
# to dbt would fail there for reasons that have nothing to do with Dagster.
dbt_project = DbtProject(
    project_dir=TRANSFORM,
    profiles_dir=TRANSFORM,
    target=os.environ.get("FRESHFLOW_DBT_TARGET", "dev"),
)


@dbt_assets(manifest=dbt_project.manifest_path)
def dbt_models(context: AssetExecutionContext, dbt: DbtCliResource):
    """Every dbt model and test, as assets with their real lineage."""
    yield from dbt.cli(["build"], context=context).stream()


# The checks attach to `agg_store_sku_day` rather than to `dbt_models`, because
# an asset check targets one asset key and `dbt_models` is forty of them. That
# is also the right key on its merits: six of the seven anchors are counts or
# sums over this table, and it is where the phantom FF-LPA-00 store appeared.
WAREHOUSE_ASSET = AssetKey(["marts", "agg_store_sku_day"])


def _task(target: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "tasks.py", target], cwd=ROOT, capture_output=True, text=True
    )


@asset_check(asset=WAREHOUSE_ASSET, name="anchors_hold", blocking=True)
def anchors_hold() -> AssetCheckResult:
    """The seven figures the README and the plan quote still reproduce.

    Blocking, because a build whose anchors have moved should stop the
    downstream decision tables rather than quietly feed them different numbers.
    A moved anchor is a decision for a human - which build to believe - and the
    right place to make it is before the recommendations are regenerated.
    """
    result = _task("anchors")
    return AssetCheckResult(
        passed=result.returncode == 0,
        severity=AssetCheckSeverity.ERROR,
        metadata={"output": result.stdout[-2000:] or result.stderr[-2000:]},
    )


@asset_check(asset=WAREHOUSE_ASSET, name="published_figures_readable", blocking=False)
def published_figures_readable() -> AssetCheckResult:
    """Every documented figure the warehouse can still answer for.

    Not blocking: this is a report, not a gate. It exists so a build surfaces
    the list a documentation pass works from, rather than leaving somebody to
    grep a dozen files for digits after a change moves them.
    """
    result = _task("published")
    return AssetCheckResult(
        passed=result.returncode == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={"output": result.stdout[-2000:] or result.stderr[-2000:]},
    )


# The decision layer, which is what a dark-store operation refreshes each
# morning: rebuild the marts, then re-score the batches and re-cut the
# recommendations the store manager reads with their coffee.
daily_refresh = define_asset_job(
    name="daily_refresh",
    selection=(
        AssetSelection.assets(dbt_models)
        | AssetSelection.assets(forecast_backtest, demand_forecast)
        | AssetSelection.assets(decision_tables, policy_bundle, demo_slice)
    ),
    description="Marts, forecast and the four decision tables. Excludes the simulator.",
)

# 05:30 IST, before a dark store's first delivery window. The recommendations
# have to exist before anybody could act on them, which is the only scheduling
# constraint the business actually has.
daily_schedule = ScheduleDefinition(
    job=daily_refresh,
    # 05:30, not midnight. The first draft said "0 0 * * *" under a comment
    # claiming 05:30 - a schedule whose comment and cron disagree is worse than
    # an undocumented one, because the comment is what anybody reads.
    cron_schedule="30 5 * * *",
    execution_timezone="Asia/Kolkata",
    description="05:30 IST daily, ahead of the first delivery window.",
)

defs = Definitions(
    assets=[
        raw_events,
        dbt_models,
        forecast_backtest,
        demand_forecast,
        decision_tables,
        policy_bundle,
        demo_slice,
    ],
    asset_checks=[anchors_hold, published_figures_readable],
    jobs=[daily_refresh],
    schedules=[daily_schedule],
    resources={"dbt": DbtCliResource(project_dir=dbt_project, dbt_executable=DBT_EXECUTABLE)},
)
