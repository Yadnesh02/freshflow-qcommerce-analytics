"""The resolver compiles every metric, and every compilation runs (task S3.6).

S3.6's gate is that pytest renders *and executes* every metric in the registry,
and the second verb is the one doing the work. Rendering proves the resolver
can build a string; executing proves the string describes something the
warehouse actually contains. The registry has been written ahead of the
warehouse throughout this project - metrics named tables months before they
existed - so "the SQL parses" and "the columns are there" are different claims
and only one of them is worth making.

The failure mode this file exists to catch is not a crash. It is a metric that
compiles, runs, and returns a number that means something other than what its
label says. So alongside execution there are tests that the ratio is divided
after aggregating rather than averaged, that a join cannot fan out, and that a
dimension the source cannot reach is refused rather than cross-joined.

Needs a built warehouse:

    python tasks.py build
    python -m pytest tests/test_resolver.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from semantic.registry import load_registry
from semantic.resolver import (
    Filter,
    MetricRequest,
    Resolver,
    ResolverError,
    UnknownDimension,
    UnknownMetric,
    UnreachableDimension,
    columns_from,
)

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

registry = load_registry()
METRICS = sorted(registry.metrics.values(), key=lambda m: m.name)
DIMENSIONS = sorted(registry.dimensions.values(), key=lambda d: d.name)

pytestmark = pytest.mark.needs_warehouse


@pytest.fixture(scope="module")
def con():
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    connection = duckdb.connect(str(WAREHOUSE), read_only=True)
    connection.execute("set enable_progress_bar = false")
    connection.execute("set memory_limit = '4GB'")
    connection.execute("set threads = 2")
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def resolver(con):
    return Resolver(registry, columns_from(con))


@pytest.fixture(scope="module")
def tables(con) -> set[str]:
    return {
        r[0]
        for r in con.execute(
            "select table_name from information_schema.tables where table_schema = 'marts'"
        ).fetchall()
    }


# ================================================== the gate
@pytest.mark.parametrize("metric", METRICS, ids=[m.name for m in METRICS])
def test_every_metric_compiles_and_executes(metric, resolver, con, tables) -> None:
    """S3.6's gate, stated as the plan states it.

    Ungrouped: the metric over the whole table, which is the simplest question
    it can be asked and the one every dashboard tile starts from.
    """
    if metric.source not in tables:
        pytest.skip(f"{metric.source} is a Sprint 4/5 table - not built yet")

    compiled = resolver.compile(MetricRequest(metric.name))
    rows = con.execute(compiled.sql, list(compiled.params)).fetchall()
    assert len(rows) == 1, f"an ungrouped metric returned {len(rows)} rows"
    assert len(rows[0]) == 1, "an ungrouped metric returned more than one column"


@pytest.mark.parametrize("metric", METRICS, ids=[m.name for m in METRICS])
def test_every_metric_executes_at_its_declared_grain(metric, resolver, con, tables) -> None:
    """The grain is part of the definition, so it has to actually work.

    A metric declaring `grain: [store, sku, date_day]` is asserting those three
    slices are available from its source. If the source cannot reach one, the
    declaration is wrong and this is where that surfaces - not on a dashboard
    where the dimension is simply missing from a dropdown.
    """
    if metric.source not in tables:
        pytest.skip(f"{metric.source} is a Sprint 4/5 table - not built yet")

    compiled = resolver.compile(MetricRequest(metric.name, tuple(metric.grain), limit=20))
    rows = con.execute(compiled.sql, list(compiled.params)).fetchall()
    assert rows, f"{metric.name} at grain {metric.grain} returned nothing at all"
    assert len(rows[0]) == len(metric.grain) + 1


@pytest.mark.parametrize(
    "metric",
    [m for m in METRICS if m.tests],
    ids=[m.name for m in METRICS if m.tests],
)
def test_metric_values_respect_their_declared_bounds(metric, resolver, con, tables) -> None:
    """A metric that declares `between: [0, 1]` and returns 1.4 is not a metric.

    Run ungrouped, because a bound is a claim about the metric rather than about
    any particular slice of it.
    """
    if metric.source not in tables:
        pytest.skip(f"{metric.source} is a Sprint 4/5 table - not built yet")

    bounds = next((t["between"] for t in metric.tests if "between" in t), None)
    if bounds is None:
        pytest.skip("no range declared")

    compiled = resolver.compile(MetricRequest(metric.name))
    value = con.execute(compiled.sql, list(compiled.params)).fetchone()[0]
    if value is None:
        pytest.skip(f"{metric.name} is null over the whole table - nothing to bound")

    low, high = bounds
    assert low <= value <= high, (
        f"{metric.name} computes {value:.4f}, outside its declared [{low}, {high}]"
    )


# ================================================== the shape is right
@pytest.mark.parametrize(
    "metric",
    [m for m in METRICS if m.is_ratio],
    ids=[m.name for m in METRICS if m.is_ratio],
)
def test_ratios_divide_after_aggregating(metric, resolver) -> None:
    """SUM(a)/SUM(b) sliced by store is not the mean of per-store ratios.

    The two differ whenever the denominators differ, which for anything divided
    by orders or units is always, and the wrong one is higher exactly where the
    denominator is small - the tail. The registry keeps the halves apart so this
    layer cannot pre-divide; this asserts the layer did not.
    """
    sql = resolver.compile(MetricRequest(metric.name)).sql
    assert f"{metric.numerator} / {metric.denominator}" in sql, (
        f"{metric.name} was not compiled as numerator / denominator"
    )
    assert "avg(" not in sql.lower().replace(metric.numerator.lower(), ""), (
        f"{metric.name} averages something outside its own numerator"
    )


def test_every_dimension_join_target_is_unique_on_its_key(con) -> None:
    """Fan-out is a correctness bug, not a performance one.

    Every join here is a left join to a dimension table. If any target holds two
    rows for one key, the base rows duplicate and every SUM in the metric
    doubles - silently, and only for the slices that use that dimension. The
    modelling makes this true; this checks it rather than trusting it.
    """
    offenders = []
    for dimension in DIMENSIONS:
        if not dimension.needs_join:
            continue
        table = dimension.join["table"]
        exists = con.execute(
            "select count(*) from information_schema.tables where table_name = ?", [table]
        ).fetchone()[0]
        if not exists:
            continue
        duplicated = con.execute(
            f"""
            select count(*) from (
                select {dimension.join["join_on"]}
                from marts.{table}
                group by {dimension.join["join_on"]}
                having count(distinct {dimension.attribute}) > 1
            )
            """
        ).fetchone()[0]
        if duplicated:
            offenders.append((dimension.name, table, duplicated))
    assert not offenders, (
        f"these dimensions would fan out their metric: {offenders}. Every SUM sliced by "
        "one of them is multiplied."
    )


def test_a_slice_the_source_cannot_reach_is_refused(resolver) -> None:
    """The registry's own promise: refuse rather than return a wrong number.

    agg_store_sku_day has no customer_id, so slicing wastage by discount band is
    not a hard query - it is a meaningless one, and a cross join would answer it
    with a plausible figure.
    """
    with pytest.raises(UnreachableDimension, match="customer_id"):
        resolver.compile(MetricRequest("wastage_rate_value", ("ddi_band",)))


def test_unknown_names_fail_loudly(resolver) -> None:
    with pytest.raises(UnknownMetric, match="unknown metric"):
        resolver.compile(MetricRequest("gross_margin_after_lunch"))
    with pytest.raises(UnknownDimension, match="unknown dimension"):
        resolver.compile(MetricRequest("net_revenue", ("phase_of_moon",)))


# ================================================== filters
def test_filter_values_are_bound_not_interpolated(resolver) -> None:
    """These arrive from a query string in S3.7."""
    compiled = resolver.compile(
        MetricRequest("net_revenue", ("category",), (Filter("store_tier", "=", "premium"),))
    )
    assert "premium" not in compiled.sql, "a filter value was written into the SQL text"
    assert compiled.params == ("premium",)
    assert "?" in compiled.sql


def test_a_filtered_dimension_is_joined_even_when_not_selected(resolver, con) -> None:
    """The bug this file found before the API could.

    Joining only the selected dimensions leaves the where clause pointing at an
    alias that was never introduced. It compiles fine and fails on execution,
    which is to say it fails in production rather than here.
    """
    compiled = resolver.compile(
        MetricRequest("fill_rate", ("category",), (Filter("store_tier", "=", "premium"),), limit=5)
    )
    assert "d_store_tier" in compiled.sql
    rows = con.execute(compiled.sql, list(compiled.params)).fetchall()
    assert rows, "a filtered request returned nothing - the join or the predicate is wrong"


def test_filtering_actually_narrows_the_result(resolver, con) -> None:
    """A filter that changes nothing is worse than no filter: it reads as applied."""
    unfiltered = con.execute(
        *_run(resolver.compile(MetricRequest("net_revenue", ("store",))))
    ).fetchall()
    filtered = con.execute(
        *_run(
            resolver.compile(
                MetricRequest("net_revenue", ("store",), (Filter("store_tier", "=", "premium"),))
            )
        )
    ).fetchall()
    assert 0 < len(filtered) < len(unfiltered), (
        f"filtering to premium stores returned {len(filtered)} of {len(unfiltered)} rows"
    )


def test_malformed_filters_are_rejected_at_construction(resolver) -> None:
    with pytest.raises(ResolverError, match="unsupported operator"):
        Filter("store", "DROP TABLE", "x")
    with pytest.raises(ResolverError, match="needs a collection"):
        Filter("store", "in", "not-a-list")
    with pytest.raises(ResolverError, match="exactly two"):
        Filter("date_day", "between", [1, 2, 3])


def test_a_negative_limit_is_refused(resolver) -> None:
    with pytest.raises(ResolverError, match="limit must be positive"):
        resolver.compile(MetricRequest("net_revenue", limit=0))


# ================================================== what the caller is told
def test_the_north_star_carries_its_guardrail(resolver) -> None:
    """The registry pairs them because the north star can be moved by starving
    stores of stock, which shows up as churn rather than as failure. Carrying
    the pairing to the caller is what stops the pair being separated by whoever
    builds the dashboard."""
    compiled = resolver.compile(MetricRequest(registry.north_star))
    assert compiled.guardrail == registry.guardrail, (
        f"{registry.north_star} compiled without naming {registry.guardrail}"
    )


def test_slicing_beyond_the_declared_grain_warns_rather_than_fails(resolver) -> None:
    """It is a valid query answering a slightly different question, and the
    reader of a chart cannot tell. Refusing would be wrong; silence would too."""
    compiled = resolver.compile(MetricRequest("net_revenue", ("day_of_week",)))
    assert any("declared grain" in w for w in compiled.warnings), (
        f"no warning for an out-of-grain slice: {compiled.warnings}"
    )


def _run(compiled):
    return compiled.sql, list(compiled.params)
