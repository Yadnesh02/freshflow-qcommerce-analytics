"""The metrics API (task S3.7).

Three endpoints and one rule: every number this returns came out of the
registry. `/metrics/{name}` compiles through `semantic.resolver`, which reads
`metrics.yml` and nothing else, so there is no route by which a caller reaches
an aggregate that was not declared. Gate G3 - no number on screen absent from
the registry - is a property of this arrangement rather than a convention the
dashboard is asked to respect.

**Every response echoes its SQL.** The dashboard puts "see query" on each tile.
That is the trust feature analysts want from BI tools, and it doubles as the
audit: a tile that cannot show its statement did not come from here.

**Refusals are first-class.** The resolver declines a slice its source cannot
reach, because a cross join would answer the question with a plausible and
meaningless number. That surfaces as a 400 naming the dimension and listing
what would have worked, which is the behaviour `dimensions.yml` promised in its
own header.

**One read-only connection, cursors per request.** DuckDB permits many readers
and one writer; the API is only ever a reader, so a rebuild running alongside it
is safe. A connection is not safe to use from two threads at once, so each
request takes a cursor under a lock.

    python tasks.py api          # then open http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

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
from serving.api.cache import TTLCache
from serving.api.schemas import (
    ActionQueueResponse,
    ElasticityCell,
    ElasticityResponse,
    ErrorResponse,
    ExpiryAction,
    ExpiryQueueResponse,
    FreshnessResponse,
    MetricDefinition,
    MetricResponse,
    RecommendedAction,
    ResponseMeta,
    SourceFreshness,
)

# A page of tiles is a few hundred rows; anything larger is a caller mistake or
# an export, and this container has 1 GB for everything including the frames it
# builds to answer.
MAX_ROWS = 5000
DEFAULT_ROWS = 500


class Warehouse:
    """One read-only handle, shared, with a cursor per request."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self._con: duckdb.DuckDBPyConnection | None = None
        self._lock = threading.Lock()
        self.resolver: Resolver | None = None
        self.cache = TTLCache()

    def ensure_open(self) -> None:
        """Idempotent, because not every caller runs the lifespan.

        Uvicorn does. `httpx.ASGITransport` - which the dashboard uses to call
        this app in-process - does not: it dispatches requests and skips
        startup entirely. An app that only opened its warehouse in `lifespan`
        would therefore serve the dashboard nothing but AttributeError, and
        only once a page ran. Opening on first use covers both, and the lock
        makes concurrent first requests safe.
        """
        if self._con is not None:
            return
        with self._lock:
            if self._con is None:
                self.open()

    def open(self) -> None:
        self.path = _warehouse_path()
        self._con = duckdb.connect(str(self.path), read_only=True)
        self._con.execute("set enable_progress_bar = false")
        self._con.execute("set memory_limit = '512MB'")
        self._con.execute("set threads = 2")
        self.resolver = Resolver(load_registry(), columns_from(self._con))

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def query(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        self.ensure_open()
        with self._lock:
            cursor = self._con.cursor()
            cursor.execute(sql, params)
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]


warehouse = Warehouse()


def _warehouse_path() -> Path:
    """The full warehouse when one is configured, otherwise the deployed slice.

    Deployed there is no choice - the container has the demo slice and nothing
    else - so `resolve()` is the default and the env var exists for local work
    against the full 365-day dataset.
    """
    configured = os.environ.get("FRESHFLOW_WAREHOUSE")
    if configured:
        return Path(configured)
    from serving.demo_data import resolve

    return resolve()


@asynccontextmanager
async def lifespan(app: FastAPI):
    warehouse.ensure_open()
    yield
    warehouse.close()


def resolver() -> Resolver:
    """The resolver, opening the warehouse on first use if nobody else has."""
    warehouse.ensure_open()
    return warehouse.resolver


app = FastAPI(
    title="FreshFlow Metrics API",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "Every number here is compiled from semantic/metrics.yml. Responses echo the "
        "SQL they ran and the metric definition they compiled, so any figure can be "
        "traced back to its declaration."
    ),
)


@app.exception_handler(ResolverError)
async def _resolver_error(request: Request, exc: ResolverError) -> JSONResponse:
    """A refused request is a 400 with the reason, not a 500.

    Asking for a slice the source cannot reach is a reasonable-looking request
    that has no correct answer, so the response says which dimension and lists
    the ones that would have worked.
    """
    status = 404 if isinstance(exc, UnknownMetric) else 400
    available: list[str] = []
    if isinstance(exc, UnknownMetric):
        available = sorted(resolver().registry.metrics)
    elif isinstance(exc, (UnknownDimension, UnreachableDimension)):
        available = sorted(resolver().registry.dimensions)
    return JSONResponse(
        status_code=status,
        content=ErrorResponse(
            error=type(exc).__name__, detail=str(exc), available=available
        ).model_dump(),
    )


def _definition(metric) -> MetricDefinition:
    return MetricDefinition(
        name=metric.name,
        label=metric.label,
        description=metric.description.strip(),
        family=metric.family,
        type=metric.type,
        source=metric.source,
        grain=list(metric.grain),
        format=metric.format,
        direction=metric.direction,
        owner=metric.owner,
        numerator=metric.numerator,
        denominator=metric.denominator,
        expression=metric.expression,
        guarded_by=metric.guarded_by,
        notes=metric.notes.strip() if metric.notes else None,
    )


def _parse_filters(raw: str | None) -> tuple[Filter, ...]:
    """`store_tier:premium,category:Bakery` into bound predicates.

    Only `=` is offered over the query string. Richer operators exist on the
    resolver and belong in a POST body; smuggling them through a colon-separated
    string would produce a grammar nobody can validate and a place for a caller
    to put something that is not a value.
    """
    if not raw:
        return ()
    predicates = []
    for clause in raw.split(","):
        if ":" not in clause:
            raise ResolverError(
                f"filter '{clause}' is not 'dimension:value'. Example: store_tier:premium"
            )
        dimension, _, value = clause.partition(":")
        predicates.append(Filter(dimension.strip(), "=", value.strip()))
    return tuple(predicates)


def _clamp(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_ROWS
    return max(1, min(limit, MAX_ROWS))


def _serve(sql: str, params: list[Any]) -> tuple[list[dict], bool, float]:
    """Run it, or return the cached answer. Returns (rows, was_cached, ms)."""
    key = (sql, tuple(params))
    started = time.perf_counter()

    hit, cached_rows = warehouse.cache.get(key)
    if hit:
        return cached_rows, True, (time.perf_counter() - started) * 1000

    rows = warehouse.query(sql, params)
    warehouse.cache.put(key, rows)
    return rows, False, (time.perf_counter() - started) * 1000


def _table_exists(table: str) -> bool:
    """Whether a marts table is present.

    The four decision engines are built and run independently, so the action
    queue has to degrade to whatever exists rather than fail whole.
    """
    rows = warehouse.query(
        "select count(*) as n from information_schema.tables "
        "where table_schema = 'marts' and table_name = ?",
        [table],
    )
    return bool(rows and rows[0]["n"])


def _now() -> str:
    return dt.datetime.now(tz=dt.UTC).replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ endpoints
@app.get("/metrics", summary="List every metric the registry publishes")
def list_metrics() -> dict[str, Any]:
    registry = resolver().registry
    return {
        "north_star": registry.north_star,
        "guardrail": registry.guardrail,
        "metrics": [
            {
                "name": m.name,
                "label": m.label,
                "family": m.family,
                "grain": list(m.grain),
                "format": m.format,
                "direction": m.direction,
            }
            for m in sorted(registry.metrics.values(), key=lambda x: x.name)
        ],
        "dimensions": sorted(registry.dimensions),
    }


@app.get("/metrics/{name}", response_model=MetricResponse, summary="Compute one metric")
def get_metric(
    name: str,
    dimensions: str | None = Query(default=None, description="Comma-separated, e.g. `store,week`"),
    filters: str | None = Query(
        default=None, description="Comma-separated `dimension:value`, e.g. `store_tier:premium`"
    ),
    limit: int | None = Query(default=None, ge=1, description=f"Capped at {MAX_ROWS}"),
) -> MetricResponse:
    requested = tuple(d.strip() for d in dimensions.split(",") if d.strip()) if dimensions else ()
    compiled = resolver().compile(
        MetricRequest(
            metric=name,
            dimensions=requested,
            filters=_parse_filters(filters),
            limit=_clamp(limit) if requested else None,
        )
    )
    rows, cached, elapsed = _serve(compiled.sql, list(compiled.params))
    return MetricResponse(
        data=rows,
        meta=ResponseMeta(
            sql=compiled.sql,
            params=list(compiled.params),
            rows=len(rows),
            cached=cached,
            generated_at=_now(),
            elapsed_ms=round(elapsed, 2),
            metric_definition=_definition(compiled.metric),
            warnings=list(compiled.warnings),
        ),
    )


@app.get("/actions/expiry", response_model=ExpiryQueueResponse, summary="The ranked action queue")
def expiry_queue(
    store: str | None = Query(default=None, description="Store id, e.g. `FF-BAN-01`"),
    min_value: float = Query(default=0.0, ge=0, description="Rupees at risk, at least"),
    limit: int = Query(default=50, ge=1),
) -> ExpiryQueueResponse:
    """Batches worth acting on, most valuable first.

    Filtered to `at_risk` by default and deliberately: already-expired stock is
    a real loss and appears in the wastage metrics, but no markdown recovers it,
    so putting it in an action queue would give someone a list they cannot act
    on ranked above one they can.
    """
    clauses = ["risk_state = 'at_risk'", "value_at_risk_inr >= ?"]
    params: list[Any] = [min_value]
    if store:
        clauses.append("store_id = ?")
        params.append(store)

    sql = (
        "select batch_id, store_id, sku_id, sku_name, l1_category,\n"
        "       cast(expiry_date as varchar) as expiry_date, days_to_expiry,\n"
        "       qty_remaining, units_at_risk, expiry_risk_score,\n"
        "       value_at_risk_inr, risk_state\n"
        "from marts.mart_expiry_risk\n"
        f"where {' and '.join(clauses)}\n"
        "order by value_at_risk_inr desc\n"
        f"limit {_clamp(limit)}"
    )
    try:
        rows, cached, elapsed = _serve(sql, params)
    except duckdb.CatalogException as exc:
        raise HTTPException(
            status_code=503,
            detail="mart_expiry_risk is not built - run `python tasks.py expiry-risk`",
        ) from exc

    return ExpiryQueueResponse(
        data=[ExpiryAction(**row) for row in rows],
        meta=ResponseMeta(
            sql=sql,
            params=params,
            rows=len(rows),
            cached=cached,
            generated_at=_now(),
            elapsed_ms=round(elapsed, 2),
            warnings=[
                "at_risk only: expired stock is a booked loss, not an action",
            ],
        ),
    )


@app.get(
    "/elasticity",
    response_model=ElasticityResponse,
    summary="Price response by category and freshness",
)
def elasticity(
    category: str | None = Query(default=None, description="L1 category, e.g. `Dairy & Eggs`"),
    identified_only: bool = Query(
        default=False, description="Drop cells where no price response was established"
    ),
) -> ElasticityResponse:
    """The demand curves S4.2 prices against, including the ones that are not there.

    Nine of twenty-three cells could not be identified: their confidence
    intervals cover zero, and two of the thin ones came back at +8.60 and +1.61,
    which is a Poisson fit dividing by a price series with no variation in it
    rather than a finding. They are returned by default and flagged, because a
    chart of only the cells that worked would show a tidy demand curve across
    every category and imply the last day was measured. It was not, in any
    category.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if category:
        clauses.append("est.l1_category = ?")
        params.append(category)
    if identified_only:
        clauses.append("est.is_identified")
    where = f"where {' and '.join(clauses)}\n" if clauses else ""

    sql = (
        "select est.l1_category, est.dte_band, bands.min_days, bands.max_days,\n"
        "       est.observations, est.discounted_observations,\n"
        "       est.elasticity_raw, est.standard_error, est.is_identified,\n"
        "       case when est.is_identified then est.elasticity_raw end as elasticity_usable,\n"
        "       est.elasticity_basis\n"
        "from marts.mart_price_elasticity as est\n"
        "inner join marts.dim_dte_band as bands on bands.dte_band = est.dte_band\n"
        f"{where}"
        "order by est.l1_category, bands.sort_order"
    )
    try:
        rows, cached, elapsed = _serve(sql, params)
    except duckdb.CatalogException as exc:
        raise HTTPException(
            status_code=503,
            detail="mart_price_elasticity is not built - run `python tasks.py elasticity`",
        ) from exc

    unidentified = sum(1 for row in rows if not row["is_identified"])
    warnings = []
    if unidentified:
        warnings.append(
            f"{unidentified} of {len(rows)} cells found no price response; "
            "elasticity_usable is null for those and they must not be priced against"
        )
    return ElasticityResponse(
        data=[ElasticityCell(**row) for row in rows],
        meta=ResponseMeta(
            sql=sql,
            params=params,
            rows=len(rows),
            cached=cached,
            generated_at=_now(),
            elapsed_ms=round(elapsed, 2),
            warnings=warnings,
        ),
    )


ACTION_QUEUE_SQL = """
with markdown as (
    select
        'markdown' as action_type, store_id, sku_id, sku_name, l1_category,
        'cut to ' || cast(round(recommended_price, 0) as varchar)
            || ' from ' || cast(round(posted_price, 0) as varchar) as detail,
        expected_units_sold as units,
        margin_vs_do_nothing as value_inr,
        'margin gained vs holding price' as value_basis,
        'elasticity ' || cast(round(elasticity_used, 2) as varchar)
            || ' at ' || dte_band as rationale
    from marts.rec_markdown
    where decision = 'markdown'
),
deal as (
    select
        'deal_slot' as action_type, store_id, sku_id, sku_name, l1_category,
        'Rs 11 slot, rank ' || cast(slot_rank as varchar) as detail,
        expected_units as units,
        slot_value as value_inr,
        'net slot value' as value_basis,
        case when clearance_value > 0
             then 'clears ' || cast(round(units_at_risk, 0) as varchar) || ' at-risk units'
             else 'basket and reactivation only' end as rationale
    from marts.rec_deal_slot
),
transfer as (
    select
        'transfer' as action_type, from_store as store_id, sku_id, sku_name, l1_category,
        'send ' || cast(units as varchar) || ' to ' || to_store as detail,
        cast(units as double) as units,
        net_benefit as value_inr,
        'write-off avoided net of the trip' as value_basis,
        cast(round(km, 1) as varchar) || ' km, '
            || cast(round(sellable_days_after_transit, 1) as varchar)
            || 'd of life on arrival' as rationale
    from marts.rec_transfer_order
),
purchase as (
    select
        'replenish' as action_type, store_id, sku_id, sku_name, l1_category,
        'order ' || cast(order_units as varchar) || ' units' as detail,
        order_units as units,
        order_units * landed_cost as value_inr,
        'landed cost being committed' as value_basis,
        case when is_shelf_life_capped
             then 'capped by shelf life, not by service level'
             else 'service level ' || cast(round(critical_ratio, 2) as varchar) end as rationale
    from marts.rec_purchase_order
)
select * from ({union_sql})
{where}
-- Grouped before ranked, deliberately. A global sort by rupees puts every
-- purchase order above every transfer, because a replenishment line's value is
-- money about to be SPENT and a transfer's is money SAVED. Those are not the
-- same quantity and ordering them against each other is a category error that
-- reads as a priority.
order by action_type, value_inr desc
limit {limit}
"""


@app.get("/actions/queue", response_model=ActionQueueResponse, summary="Everything to do today")
def action_queue(
    store: str | None = Query(default=None, description="Store id, e.g. `FF-BAN-01`"),
    action_type: str | None = Query(
        default=None, description="markdown | deal_slot | transfer | replenish"
    ),
    limit: int = Query(default=100, ge=1),
) -> ActionQueueResponse:
    """The four decision engines' output as one ranked list.

    Grouped by engine, ranked by value within each. Not ranked globally: the
    four engines measure different rupees. A replenishment line's value is money
    about to be committed; a transfer's is a write-off avoided. Sorting those
    against one another puts every purchase order above every transfer and reads
    as a priority ordering, which it is not. `value_basis` says what each number
    is so nothing downstream has to guess.

    A missing table is not an error here. The engines are built and run
    independently, so this returns whatever exists and names what does not -
    a queue that 503s because one optimiser has not been run yet is useless on
    exactly the day somebody needs the other three.
    """
    sources = {
        "markdown": "rec_markdown",
        "deal_slot": "rec_deal_slot",
        "transfer": "rec_transfer_order",
        "replenish": "rec_purchase_order",
    }
    wanted = [action_type] if action_type else list(sources)
    available, missing = [], []
    for name in wanted:
        if name not in sources:
            raise HTTPException(status_code=422, detail=f"unknown action_type {name!r}")
        if _table_exists(sources[name]):
            available.append(name)
        else:
            missing.append(f"{sources[name]} is not built")

    if not available:
        return ActionQueueResponse(
            data=[],
            meta=ResponseMeta(
                sql="",
                params=[],
                rows=0,
                cached=False,
                generated_at=_now(),
                elapsed_ms=0.0,
                warnings=missing or ["no decision engine has been run"],
            ),
        )

    blocks = {
        "markdown": "select * from markdown",
        "deal_slot": "select * from deal",
        "transfer": "select * from transfer",
        "replenish": "select * from purchase",
    }
    union_sql = "\nunion all\n".join(blocks[name] for name in available)
    params: list[Any] = []
    where = ""
    if store:
        where = "where store_id = ?"
        params.append(store)

    sql = ACTION_QUEUE_SQL.format(union_sql=union_sql, where=where, limit=_clamp(limit))
    rows, cached, elapsed = _serve(sql, params)

    return ActionQueueResponse(
        data=[RecommendedAction(**row) for row in rows],
        meta=ResponseMeta(
            sql=sql,
            params=params,
            rows=len(rows),
            cached=cached,
            generated_at=_now(),
            elapsed_ms=round(elapsed, 2),
            warnings=missing,
        ),
    )


@app.get("/health/freshness", response_model=FreshnessResponse, summary="Source freshness SLA")
def freshness() -> FreshnessResponse:
    """How far behind each source feed is, and which have breached.

    Read from dq_source_coverage rather than from max(date) on each fact,
    because a feed that stopped arriving looks identical to one that had nothing
    to send unless something tracks the partitions it was supposed to produce.
    """
    sql = (
        "select source_name,\n"
        "       cast(max(last_seen_date) as varchar) as last_seen_date,\n"
        "       cast(max(date_day) - max(last_seen_date) as integer) as days_behind,\n"
        "       cast(count(*) filter (where is_missing_partition) as integer)"
        " as missing_partitions,\n"
        "       coalesce(bool_or(is_stale), false) as is_stale\n"
        "from marts.dq_source_coverage\n"
        "group by source_name\n"
        "order by is_stale desc, days_behind desc"
    )
    rows, cached, elapsed = _serve(sql, [])
    sources = [SourceFreshness(**row) for row in rows]
    return FreshnessResponse(
        as_of=max((s.last_seen_date for s in sources if s.last_seen_date), default=None),
        sources=sources,
        stale_sources=sum(1 for s in sources if s.is_stale),
        meta=ResponseMeta(
            sql=sql,
            params=[],
            rows=len(rows),
            cached=cached,
            generated_at=_now(),
            elapsed_ms=round(elapsed, 2),
        ),
    )


@app.get("/health", summary="Liveness and what this instance is reading")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "warehouse": str(warehouse.path),
        "metrics_published": len(resolver().registry.metrics),
        "cache": {
            "entries": len(warehouse.cache),
            "hit_rate": round(warehouse.cache.stats.hit_rate, 3),
            "ttl_seconds": warehouse.cache.ttl,
        },
    }
