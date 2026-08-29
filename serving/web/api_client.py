"""The dashboard's only route to a number (task S3.8).

S3.8's gate is that nothing under `serving/web/` touches DuckDB, and this file
is why that is achievable rather than merely asked for: it is the single place
the app performs I/O, and it speaks HTTP to the metrics API. Every figure the
dashboard renders therefore arrived through the resolver, which reads
`metrics.yml` and nothing else. That chain is gate G3.

**In-process by default, over a real ASGI request.** Streamlit Community Cloud
runs one container, and standing up a second service on Render to satisfy an
architectural diagram would add a cold start of 30-60 seconds to the first page
view of a portfolio demo. `httpx.ASGITransport` calls the FastAPI application
directly - no socket, no port - but through the genuine request cycle: routing,
query parsing, Pydantic validation, the resolver, the exception handlers. The
boundary is real; only the network is absent.

That transport is async-only - it implements `handle_async_request` and nothing
else - so this client is built on `AsyncClient` and drives it from one event
loop it owns. Streamlit's script thread has no loop of its own, so the loop is
created once and reused rather than spun up per call. A synchronous
`httpx.Client` fails against it with `'ASGITransport' object has no attribute
'handle_request'`, which surfaces only when a page actually runs - not on
import, and not in a test that uses starlette's TestClient, because that bridges
sync to async itself.

Setting `FRESHFLOW_API_URL` switches to a deployed API with no other change,
because the client above this line never knew which it was talking to.

**Failures return, they do not raise.** A tile whose metric is missing should
say so in the space where the number goes, next to eleven tiles that worked.
Raising would take the page down for one bad slice, and the reader would learn
less than the message would have told them.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class ApiResult:
    """A response, or a legible failure. Never an exception."""

    ok: bool
    data: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    detail: str | None = None

    @property
    def sql(self) -> str | None:
        return self.meta.get("sql")

    @property
    def definition(self) -> dict[str, Any] | None:
        return self.meta.get("metric_definition")

    @property
    def warnings(self) -> list[str]:
        return list(self.meta.get("warnings", []))

    def scalar(self, column: str | None = None) -> float | None:
        """The single value of an ungrouped metric."""
        if not self.ok or not self.data:
            return None
        row = self.data[0]
        key = column or next(iter(row))
        value = row.get(key)
        return None if value is None else float(value)


class MetricsClient:
    def __init__(self, base_url: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url or os.environ.get("FRESHFLOW_API_URL")
        # One loop for the life of the client. Streamlit re-runs the script on
        # every interaction, so a loop per call would be created and torn down
        # dozens of times a page.
        self._loop = asyncio.new_event_loop()

        if self.base_url:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        else:
            from serving.api.main import app

            # A real ASGI round trip against the same application uvicorn would
            # serve. Importing the app is not the same as importing the
            # warehouse: this module never sees a connection.
            self._client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://api",
                timeout=timeout,
            )

    def close(self) -> None:
        self._loop.run_until_complete(self._client.aclose())
        self._loop.close()

    # ------------------------------------------------------------------ calls
    def _get(self, path: str, params: dict[str, Any] | None = None) -> ApiResult:
        try:
            response = self._loop.run_until_complete(
                self._client.get(
                    path, params={k: v for k, v in (params or {}).items() if v is not None}
                )
            )
        except httpx.HTTPError as exc:
            return ApiResult(ok=False, error="Unreachable", detail=str(exc))

        try:
            body = response.json()
        except ValueError:
            return ApiResult(
                ok=False, error=f"HTTP {response.status_code}", detail=response.text[:300]
            )

        if response.status_code >= 400:
            return ApiResult(
                ok=False,
                error=body.get("error", f"HTTP {response.status_code}"),
                detail=body.get("detail", ""),
            )
        if isinstance(body, dict) and "data" in body:
            return ApiResult(ok=True, data=body["data"], meta=body.get("meta", {}))
        # /metrics and /health return bare objects rather than data+meta
        return ApiResult(ok=True, data=[body] if isinstance(body, dict) else body)

    def metric(
        self,
        name: str,
        dimensions: list[str] | None = None,
        filters: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> ApiResult:
        return self._get(
            f"/metrics/{name}",
            {
                "dimensions": ",".join(dimensions) if dimensions else None,
                "filters": ",".join(f"{k}:{v}" for k, v in filters.items()) if filters else None,
                "limit": limit,
            },
        )

    def expiry_queue(
        self, store: str | None = None, min_value: float = 0.0, limit: int = 50
    ) -> ApiResult:
        return self._get(
            "/actions/expiry", {"store": store, "min_value": min_value, "limit": limit}
        )

    def elasticity(self, category: str | None = None, identified_only: bool = False) -> ApiResult:
        """Price response by category and freshness band, unidentified cells included.

        Included on purpose: a chart of only the cells that worked would show a
        clean demand curve for every category and imply the last day was
        measured. It was not, in any of them.
        """
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        if identified_only:
            params["identified_only"] = True
        return self._get("/elasticity", params)

    def action_queue(
        self,
        store: str | None = None,
        action_type: str | None = None,
        limit: int = 100,
    ) -> ApiResult:
        """Everything the four decision engines say to do today."""
        params: dict[str, Any] = {"limit": limit}
        if store:
            params["store"] = store
        if action_type:
            params["action_type"] = action_type
        return self._get("/actions/queue", params)

    def freshness(self) -> ApiResult:
        return self._get("/health/freshness")

    def catalogue(self) -> ApiResult:
        return self._get("/metrics")

    def health(self) -> ApiResult:
        return self._get("/health")
