# Architecture decision records

Eight decisions that shaped this project, each with the alternative that was seriously considered
and what the choice cost.

These are not a summary of the code. They are the arguments — the places where a different call was
defensible, and where the reasoning matters more than the result. Where a decision was later
contradicted by evidence, the record says so rather than being rewritten.

| ADR | Decision | Status |
|---|---|---|
| [001](001-in-house-semantic-layer.md) | An in-house semantic layer instead of a BI tool | Accepted |
| [002](002-duckdb-as-the-warehouse.md) | DuckDB as the warehouse | Accepted |
| [003](003-simulated-data-with-an-import-boundary.md) | Simulated data, with an enforced import boundary | Accepted |
| [004](004-api-in-process-over-asgi.md) | The metrics API runs in-process over ASGI | Accepted |
| [005](005-demo-slice-as-a-release-asset.md) | The demo slice ships as a Release asset, not in git | Accepted |
| [006](006-north-star-declared-before-the-experiment.md) | The north star is a rate net of wastage, declared up front | Accepted — and it cost us the headline |
| [007](007-ci-is-the-arbiter.md) | Every published number comes from a workflow, not a laptop | Accepted under duress |
| [008](008-soda-runs-isolated.md) | Soda runs in its own environment rather than as a dependency | Accepted |

## Format

Context, Decision, Consequences. No template ceremony beyond that — an ADR nobody reads because it
is nine headings long has failed at its only job.
