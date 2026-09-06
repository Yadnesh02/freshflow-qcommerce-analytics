# ADR-004 — The metrics API runs in-process over ASGI

**Status:** Accepted

## Context

Gate G3 requires that no number on the dashboard came from anywhere but the metric registry. The
clean way to guarantee that is an API boundary: the app cannot reach the warehouse, only the API
can, and the API only serves compiled metrics.

The obvious implementation is a deployed FastAPI service on Render or Fly. Streamlit Community
Cloud runs one container, so that means a second service — which sleeps on the free tier and
cold-starts in 30–60 seconds. A portfolio demo whose first page view takes a minute has failed
before it is read.

## Decision

The dashboard speaks to FastAPI **in-process through `httpx.ASGITransport`** — no socket, no port,
but a genuine request cycle: routing, query parsing, Pydantic validation, the resolver, the
exception handlers. Setting `FRESHFLOW_API_URL` switches to a deployed API with no other change,
because the client above that line never knew which it was talking to.

## Consequences

- The boundary is real; only the network is absent. Every guarantee that depends on the boundary —
  no endpoint accepts SQL, every response echoes what it ran — holds identically.
- One container, no cold start beyond Streamlit's own, and one fewer thing that can be down.
- **ASGITransport is async-only.** It implements `handle_async_request` and nothing else, so the
  client is built on `AsyncClient` driven from one event loop it owns. A synchronous `httpx.Client`
  fails against it with a missing-attribute error that surfaces only when a page actually runs —
  not on import, and not under starlette's `TestClient`, which bridges sync to async itself.
- `serving/api/openapi.json` is committed and a test asserts it matches the running app, so the
  interface is published and diffable even though no Swagger page is hosted. **This is why the
  Definition of Done's "live API `/docs`" line was dropped rather than satisfied**: it asked for the
  network, and the network is the one part deliberately absent.
- If this ever needs to serve another consumer, the change is one environment variable and a deploy.
