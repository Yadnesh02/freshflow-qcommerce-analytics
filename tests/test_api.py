"""The metrics API's contract (task S3.7).

S3.7's gate is that `/docs` loads and the SQL echo is visible, and both are
easy to satisfy shallowly. The harder property, and the one gate G3 actually
depends on, is that there exists *no route* through this API to a number the
registry did not declare. That is what most of this file checks: not that the
endpoints work, but that they cannot be made to answer something else.

The echoed SQL is load-bearing rather than decorative. A tile in the dashboard
shows it behind "see query", so a reader can trace any figure to the statement
that produced it and the definition that compiled it - and a tile that cannot
show its statement did not come from here. `test_the_echoed_sql_is_the_sql_that_ran`
is what stops the echo drifting into a plausible-looking approximation.

Needs a built warehouse:

    python tasks.py build
    python -m pytest tests/test_api.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)
OPENAPI_PATH = ROOT / "serving" / "api" / "openapi.json"

pytestmark = pytest.mark.needs_warehouse


@pytest.fixture(scope="module")
def client():
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    # pin the API at the same warehouse the rest of the suite reads, rather than
    # letting it resolve the deployed demo slice
    os.environ["FRESHFLOW_WAREHOUSE"] = str(WAREHOUSE)
    from serving.api.main import app

    with TestClient(app) as test_client:
        yield test_client
    # the lifespan shutdown closes it, but ensure_open() may have reopened it
    # lazily since; DuckDB refuses a writer while any reader is live
    from serving.api.main import warehouse

    warehouse.close()


@pytest.fixture(scope="module")
def con():
    connection = duckdb.connect(str(WAREHOUSE), read_only=True)
    connection.execute("set enable_progress_bar = false")
    yield connection
    connection.close()


# ================================================== the gate
def test_the_openapi_page_loads(client) -> None:
    """S3.7's gate, literally."""
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    for required in ("/metrics/{name}", "/actions/expiry", "/health/freshness"):
        assert required in paths, f"{required} is missing from the published contract"


def test_every_response_echoes_its_sql(client) -> None:
    """The other half of the gate, on all three endpoints."""
    for path, params in (
        ("/metrics/gm_awm", {"dimensions": "store", "limit": 3}),
        ("/actions/expiry", {"limit": 3}),
        ("/health/freshness", {}),
    ):
        meta = client.get(path, params=params).json()["meta"]
        assert meta["sql"].strip().lower().startswith("select"), f"{path} echoed no statement"
        assert "generated_at" in meta and meta["rows"] >= 0


def test_the_echoed_sql_is_the_sql_that_ran(client, con) -> None:
    """An echo that only resembles the query is worse than none.

    It would look like an audit trail and function as decoration, and the first
    time someone used it to debug a number they would be debugging a different
    statement. So the echoed text is executed directly and has to reproduce the
    response.

    Floats are compared with a tolerance rather than exactly, and not to make
    the test pass: DuckDB aggregates in parallel and sums the partitions in
    whatever order they finish, so the same statement over the same rows can
    differ in the last couple of bits between runs. Observed here as
    4.186079581892022e-06 against 4.186079581892068e-06 - a relative difference
    of 1e-14. Demanding bit equality would assert something about the
    scheduler; the property worth holding is that the echoed query answers the
    same question.
    """
    body = client.get("/metrics/wastage_rate_value", params={"dimensions": "category"}).json()
    replayed = con.execute(body["meta"]["sql"], body["meta"]["params"]).fetchall()

    assert len(replayed) == len(body["data"]), "the echoed SQL returns a different row count"
    served = [tuple(row.values()) for row in body["data"]]
    for replayed_row, served_row in zip(replayed, served, strict=True):
        assert len(replayed_row) == len(served_row)
        for replayed_value, served_value in zip(replayed_row, served_row, strict=True):
            if isinstance(served_value, float):
                assert replayed_value == pytest.approx(served_value, rel=1e-9), (
                    "the echoed SQL returns a materially different number"
                )
            else:
                assert replayed_value == served_value, (
                    "the echoed SQL returns different rows than the response carried"
                )


def test_the_metric_definition_travels_with_the_number(client) -> None:
    """If a figure looks wrong, the next question is what it was meant to mean."""
    definition = client.get("/metrics/fill_rate").json()["meta"]["metric_definition"]
    assert definition["name"] == "fill_rate"
    assert definition["numerator"] and definition["denominator"]
    assert definition["source"] and definition["grain"]


def test_the_north_star_arrives_with_its_guardrail(client) -> None:
    """The registry pairs them because the north star can be moved by starving
    stores of stock, which reads as churn rather than as failure. The pairing
    has to survive the trip to the caller or the dashboard will separate them."""
    listing = client.get("/metrics").json()
    definition = client.get(f"/metrics/{listing['north_star']}").json()["meta"]["metric_definition"]
    assert definition["guarded_by"] == listing["guardrail"]


# ================================================== G3: no other route to a number
def test_there_is_no_endpoint_that_takes_raw_sql(client) -> None:
    """G3 holds only if the registry is the sole way in.

    One `/query?sql=` for convenience would end it - every guarantee about
    where numbers come from would become a convention instead of a property.
    """
    paths = client.get("/openapi.json").json()["paths"]
    for path, methods in paths.items():
        for method, spec in methods.items():
            parameters = {p["name"] for p in spec.get("parameters", [])}
            assert "sql" not in parameters and "query" not in parameters, (
                f"{method.upper()} {path} accepts a raw statement - G3 cannot hold"
            )
        assert "post" not in methods, f"{path} accepts POST; only reads were designed for"


def test_an_unknown_metric_is_a_404_listing_what_exists(client) -> None:
    body = client.get("/metrics/gross_margin_after_lunch").json()
    assert body["error"] == "UnknownMetric"
    assert "gm_awm" in body["available"]


def test_an_unreachable_slice_is_a_400_not_a_wrong_number(client) -> None:
    """The refusal `dimensions.yml` promised in its own header.

    agg_store_sku_day has no customer_id, so slicing wastage by discount band
    has no correct answer - and a cross join would supply a plausible one.
    """
    response = client.get("/metrics/wastage_rate_value", params={"dimensions": "ddi_band"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "UnreachableDimension"
    assert "customer_id" in body["detail"]
    assert "ddi_band" in body["available"]


def test_a_malformed_filter_explains_the_format(client) -> None:
    body = client.get("/metrics/net_revenue", params={"filters": "store_tier=premium"}).json()
    assert "dimension:value" in body["detail"]


def test_filter_values_do_not_reach_the_sql_text(client) -> None:
    """They arrive from a query string, so they are bound or they are a hole."""
    meta = client.get(
        "/metrics/fill_rate", params={"dimensions": "category", "filters": "store_tier:premium"}
    ).json()["meta"]
    assert "premium" not in meta["sql"]
    assert meta["params"] == ["premium"]


# ================================================== the guards
def test_the_row_limit_is_enforced_against_the_caller(client) -> None:
    """1 GB for the whole container, and the frame counts against it."""
    from serving.api.main import MAX_ROWS

    body = client.get(
        "/metrics/net_revenue", params={"dimensions": "sku", "limit": MAX_ROWS * 10}
    ).json()
    assert body["meta"]["rows"] <= MAX_ROWS
    assert f"limit {MAX_ROWS}" in body["meta"]["sql"]


def test_repeating_a_request_is_served_from_cache(client) -> None:
    """Twelve tiles on a page must not be twelve scans of the same table."""
    params = {"dimensions": "store", "limit": 5}
    first = client.get("/metrics/in_stock_pct", params=params).json()
    second = client.get("/metrics/in_stock_pct", params=params).json()
    assert first["meta"]["cached"] is False
    assert second["meta"]["cached"] is True
    assert first["data"] == second["data"], "the cache returned something else"


def test_dimension_order_is_part_of_the_question(client) -> None:
    """Two orderings are two presentations, so they are two cache entries.

    Not a correctness issue - both are right - but the SQL differs, and a cache
    keyed loosely enough to conflate them would serve one request's columns for
    the other's.
    """
    a = client.get("/metrics/net_revenue", params={"dimensions": "store,category"}).json()
    b = client.get("/metrics/net_revenue", params={"dimensions": "category,store"}).json()
    assert list(a["data"][0]) != list(b["data"][0])


def test_slicing_outside_the_declared_grain_warns(client) -> None:
    body = client.get("/metrics/net_revenue", params={"dimensions": "day_of_week"}).json()
    assert any("declared grain" in w for w in body["meta"]["warnings"])


# ================================================== the action queue
def test_the_expiry_queue_ranks_by_money_and_excludes_the_unactionable(client) -> None:
    """Already-expired stock is a booked loss; ranking it above stock a markdown
    could still save would hand someone a list they cannot act on."""
    body = client.get("/actions/expiry", params={"limit": 25}).json()
    assert body["data"], "the action queue is empty"

    values = [row["value_at_risk_inr"] for row in body["data"]]
    assert values == sorted(values, reverse=True), "the queue is not ranked by value at risk"
    assert all(row["risk_state"] == "at_risk" for row in body["data"])
    assert all(row["days_to_expiry"] >= 0 for row in body["data"])


def test_the_expiry_queue_filters_by_store_and_by_value(client) -> None:
    everything = client.get("/actions/expiry", params={"limit": 200}).json()["data"]
    store = everything[0]["store_id"]

    filtered = client.get(
        "/actions/expiry", params={"store": store, "min_value": 500, "limit": 200}
    ).json()["data"]
    assert filtered, f"no at-risk batches at {store} above Rs 500"
    assert all(row["store_id"] == store for row in filtered)
    assert all(row["value_at_risk_inr"] >= 500 for row in filtered)


# ================================================== freshness
def test_freshness_reports_every_source_and_flags_the_known_outage(client) -> None:
    """dq_source_coverage tracks partitions a feed was supposed to produce, so a
    feed that stopped is distinguishable from one with nothing to send. Defect 8
    removed two clickstream days, and this is where that becomes visible."""
    body = client.get("/health/freshness").json()
    assert body["sources"], "no sources reported"
    assert body["as_of"]

    by_name = {s["source_name"]: s for s in body["sources"]}
    assert "clickstream" in by_name
    assert by_name["clickstream"]["missing_partitions"] == 2, (
        "the documented two-day clickstream outage is not being surfaced"
    )
    assert body["stale_sources"] == sum(1 for s in body["sources"] if s["is_stale"])


# ================================================== the committed contract
def test_the_committed_openapi_matches_the_running_app(client) -> None:
    """The spec is committed so the contract is reviewable in a diff.

    A stale copy is worse than none: it is the thing a front-end author reads
    when the app is not running, and it would send them to build against
    endpoints that changed. Regenerate with `python tasks.py openapi`.
    """
    assert OPENAPI_PATH.exists(), f"{OPENAPI_PATH} is missing - run `python tasks.py openapi`"
    committed = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    running = client.get("/openapi.json").json()
    assert committed == running, (
        "serving/api/openapi.json has drifted from the app - "
        "run `python tasks.py openapi` and commit the result"
    )
