# ADR-008 — Soda runs in its own environment rather than as a dependency

**Status:** Accepted

## Context

Sprint 5 called for declarative freshness checks over the built warehouse, on top of the dbt tests
that already run inside the build. The two answer different questions: dbt asserts that a
transformation is correct, Soda asserts that the artefact is still fit to serve — including the
published demo, which no dbt invocation ever touches again.

The obvious implementation is `uv add soda-core-duckdb`. It resolves cleanly, which is the trap.

## Decision

Soda is **deliberately absent from `pyproject.toml`**. `quality/scan.py` runs it through
`uv run --no-project --isolated`, so the two never share an interpreter, and a test asserts it never
enters the lockfile.

## Consequences

- **What the resolution actually does:** soda-core 3.3.x caps protobuf below 5; dbt-core 1.11
  requires protobuf 6; the only resolutions satisfying both drag dbt-core back to **1.8.8** or
  forward to a **2.0 release candidate**. That would downgrade the transformation layer to
  accommodate the thing that checks it. Confirmed by running the identical resolution with Soda
  removed — dbt-core stays at 1.11.14, so Soda is the cause rather than the probe.
- Soda's own duckdb is pinned to **1.0.0** while the warehouse is written by **1.5.5**. That works —
  DuckDB's storage format is stable across the 1.x line — and it was verified by opening the file
  read-only rather than assumed from version numbers.
- soda-core imports `distutils`, removed in Python 3.12, so the isolated environment also carries
  `setuptools`. Without it the scan dies on import before opening the warehouse, which reads like a
  broken warehouse and is not one.
- **Warnings do not fail the build; failures do.** Soda exits 0, 1, 2, 3 for pass, warn, fail and
  error, and `quality/scan.py` maps 1 down to success. The clickstream outage warns on every healthy
  build, and a step that goes red for a known condition is one that gets switched off — the same
  distinction as the blocking anchors check and the non-blocking figures report.
- **There is no wall-clock freshness check**, which is the conventional spelling. The simulated world
  ends on a fixed date, so `freshness(date_day) < 2d` would fail a little harder every day while
  meaning nothing. Staleness is measured against the world's own last day instead.
- The cost is a `uv` dependency at the shell level and a few seconds per scan to build the isolated
  environment. Cheap against a downgraded warehouse.
