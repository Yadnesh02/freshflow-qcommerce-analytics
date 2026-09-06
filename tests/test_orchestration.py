"""The Dagster definitions load, and the graph is the real pipeline (task S5.6).

`dagster dev` failing to start is a bad way to find out a definition is wrong,
because it happens after somebody has decided to look at the DAG. Loading the
definitions in the suite means an import-time error - a bad annotation, a check
pointed at the wrong asset, an executable that is not on PATH - fails in CI
instead, and all three of those happened while this was being written.
"""

from __future__ import annotations

import pytest

pytest.importorskip("dagster")
pytest.importorskip("dagster_dbt")

from orchestration.assets.pipeline import OPTIMISERS  # noqa: E402
from orchestration.definitions import defs  # noqa: E402


@pytest.fixture(scope="module")
def graph():
    return defs.resolve_asset_graph()


def test_the_definitions_load_at_all(graph) -> None:
    """Three import-time failures were caught by exactly this assertion.

    `from __future__ import annotations` turns the `context` annotation into a
    string and Dagster rejects it while naming the type it wants; an asset check
    cannot target `dbt_models` because that is forty asset keys, not one; and
    `DbtCliResource` validates its executable at construction, so a bare "dbt"
    fails before Dagster draws anything when dbt lives in a virtualenv.
    """
    assert len(list(graph.get_all_asset_keys())) > 0


def test_the_dbt_models_are_assets_rather_than_one_opaque_step(graph) -> None:
    """The lineage has to run through the models, not around them.

    A single "run dbt" asset draws a tidy graph that cannot answer the question
    the graph exists for: when one model is wrong, what has to be rebuilt.
    """
    keys = {k.to_user_string() for k in graph.get_all_asset_keys()}
    assert "marts/agg_store_sku_day" in keys, "the warehouse grain is not an asset"
    assert "staging/stg_pos__order_items" in keys, "staging models are not assets"
    assert sum(1 for k in keys if k.startswith(("marts/", "staging/", "seeds/"))) > 30


def test_the_pipeline_assets_are_all_present(graph) -> None:
    keys = {k.to_user_string() for k in graph.get_all_asset_keys()}
    for expected in (
        "raw_events",
        "forecast_backtest",
        "demand_forecast",
        "decision_tables",
        "policy_bundle",
        "demo_slice",
    ):
        assert expected in keys, f"{expected} is missing from the graph"


def test_the_forecast_chain_is_ordered(graph) -> None:
    """expiry-risk needs mart_demand_forecast, which needs mart_forecast_accuracy.

    The first draft of `warehouse.yml` omitted these two links and died forty
    minutes in with "no mart_demand_forecast". The graph is where that ordering
    should be stated once, so nothing downstream has to remember it.
    """
    from dagster import AssetKey

    forecast_deps = graph.get(AssetKey(["demand_forecast"])).parent_keys
    assert AssetKey(["forecast_backtest"]) in forecast_deps

    decisions_deps = graph.get(AssetKey(["decision_tables"])).parent_keys
    assert AssetKey(["demand_forecast"]) in decisions_deps

    backtest_deps = graph.get(AssetKey(["forecast_backtest"])).parent_keys
    assert AssetKey(["marts", "agg_store_sku_day"]) in backtest_deps, (
        "the forecast chain does not depend on the warehouse, so Dagster could run it first"
    )


def test_the_anchors_check_is_blocking(graph) -> None:
    """A build whose anchors moved must not quietly feed the decision tables.

    Which build to believe is a decision for a human, and the moment to make it
    is before the recommendations are regenerated - not after a store manager
    has read them.
    """
    from orchestration.definitions import anchors_hold, published_figures_readable

    # `blocking` lives on the check spec, not on the definition that carries it.
    def blocking(check) -> bool:
        return next(iter(check.check_specs)).blocking

    assert blocking(anchors_hold), "a moved anchor should stop the run"
    assert not blocking(published_figures_readable), "the figures report is not a gate"


def test_the_daily_schedule_excludes_the_simulator() -> None:
    """Regenerating the world nightly would make every historical figure irreproducible."""
    job = next(j for j in defs.jobs if j.name == "daily_refresh")
    selection = job.selection.resolve(defs.resolve_asset_graph())
    names = {k.to_user_string() for k in selection}
    assert "raw_events" not in names, "the schedule would regenerate the dataset every night"
    assert "decision_tables" in names, "the schedule does not refresh the recommendations"


def test_the_schedule_runs_before_the_first_delivery_window() -> None:
    """The cron and its description have to agree.

    The first draft read `0 0 * * *` under a comment claiming 05:30. A schedule
    whose comment contradicts its cron is worse than an undocumented one,
    because the comment is the part anybody actually reads.
    """
    schedule = next(iter(defs.schedules))
    assert schedule.cron_schedule == "30 5 * * *"
    assert schedule.execution_timezone == "Asia/Kolkata"


def test_the_optimisers_run_in_dependency_order() -> None:
    """expiry-risk scores the batches the rest reason about; elasticity precedes markdown."""
    order = list(OPTIMISERS)
    assert order.index("expiry-risk") < order.index("markdown")
    assert order.index("elasticity") < order.index("markdown")
