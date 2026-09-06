# ADR-001 — An in-house semantic layer instead of a BI tool

**Status:** Accepted

## Context

Every number this project publishes has to mean one thing. "Wastage rate" appears on the executive
page, in the API, in the metric dictionary and in the tests, and the moment those four disagree the
dashboard stops being evidence and becomes decoration.

Power BI or Metabase would have given charts on day one. The cost is where the definition lives: a
DAX measure inside a `.pbix` is not diffable in a pull request, cannot be imported by a test, and
cannot be read by anything except the tool that owns it.

The counter-argument is real and should be stated: building this is days of work that produce no
chart, and it gives up drag-and-drop self-service and a great deal of free polish.

## Decision

One YAML registry — `semantic/metrics.yml` — holding every metric with its numerator, denominator,
grain, format and owner. A small Python resolver compiles a metric request into DuckDB SQL. The
API, the dashboard, the generated dictionary and the tests all read from that one file.

The registry is the contract, and it was written **before** any pipeline code, so the pipeline
conforms to the metrics rather than the metrics describing whatever the pipeline happened to
produce.

## Consequences

- A metric cannot drift between surfaces, because there is only one definition to drift from.
- `docs/metrics.md` is generated from the registry and CI fails if the committed copy has drifted,
  so the documentation cannot rot independently of the thing it documents.
- Every API response echoes the SQL it ran and the registry entry that compiled it, which makes
  "see query" on every tile possible at all — full lineage from a pixel back to a dbt model.
- The guarantee is structural rather than agreed: a test walks the OpenAPI spec and fails if any
  endpoint grows a `sql` parameter, and another walks the AST of every dashboard module and fails
  on `import duckdb`. One convenient shortcut for an awkward chart would end it, so both are
  enforced.
- We gave up self-service. At a company with forty business users, the right answer is Power BI on
  top of exactly this layer, not instead of it.
