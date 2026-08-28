"""Response shapes for the metrics API (task S3.7).

Every response carries a `meta` block, and `meta.sql` is the point of it. The
dashboard puts a "see query" control on every tile, so any number on screen can
be traced to the exact statement that produced it and the exact metric
definition that compiled it. That is the trust feature analysts want from BI
tools and rarely get - and here it is also the enforcement mechanism for gate
G3, because a tile whose SQL came from anywhere but the registry has nothing to
show.

`metric_definition` echoes the registry entry rather than a summary of it. If a
number looks wrong, the first question is what it was supposed to mean, and the
answer should arrive with the number rather than requiring a trip to a YAML file
in a repository the reader may not have.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MetricDefinition(BaseModel):
    """The registry entry, as published. What the number was supposed to mean."""

    name: str
    label: str
    description: str
    family: str
    type: str
    source: str
    grain: list[str]
    format: str
    direction: str
    owner: str
    numerator: str | None = None
    denominator: str | None = None
    expression: str | None = None
    guarded_by: str | None = Field(
        default=None,
        description="Metric that must be read alongside this one. The north star can be "
        "moved by starving stores of stock, which shows up as churn rather than failure.",
    )
    notes: str | None = None


class ResponseMeta(BaseModel):
    sql: str = Field(description="The exact statement executed. Shown behind 'see query'.")
    params: list[Any] = Field(
        default_factory=list,
        description="Filter values, bound rather than interpolated.",
    )
    rows: int
    cached: bool
    generated_at: str
    elapsed_ms: float
    metric_definition: MetricDefinition | None = None
    warnings: list[str] = Field(
        default_factory=list,
        description="Valid but worth knowing - a slice outside the metric's declared "
        "grain answers a subtly different question, and a chart cannot say so.",
    )


class MetricResponse(BaseModel):
    data: list[dict[str, Any]]
    meta: ResponseMeta


class ExpiryAction(BaseModel):
    """One row of the ranked action queue."""

    batch_id: str
    store_id: str
    sku_id: str
    sku_name: str | None = None
    l1_category: str | None = None
    expiry_date: str
    days_to_expiry: int
    qty_remaining: float
    units_at_risk: float
    expiry_risk_score: float
    value_at_risk_inr: float
    risk_state: str


class ExpiryQueueResponse(BaseModel):
    data: list[ExpiryAction]
    meta: ResponseMeta


class SourceFreshness(BaseModel):
    source_name: str
    last_seen_date: str | None
    days_behind: int | None
    missing_partitions: int
    is_stale: bool


class FreshnessResponse(BaseModel):
    as_of: str | None
    sources: list[SourceFreshness]
    stale_sources: int
    meta: ResponseMeta


class ErrorResponse(BaseModel):
    """A refusal, with the reason and what would have worked instead.

    The resolver refuses a slice its source cannot reach rather than emitting a
    cross join, so this is a normal outcome of a reasonable-looking request and
    the message has to be usable by whoever typed it.
    """

    error: str
    detail: str
    available: list[str] = Field(default_factory=list)
