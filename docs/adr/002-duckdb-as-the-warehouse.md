# ADR-002 — DuckDB as the warehouse

**Status:** Accepted

## Context

The project needed a warehouse that runs multi-million-row window functions, is free, and does not
require a cloud account, a Docker daemon or a running server. The candidates were BigQuery or
Snowflake on a free tier, Postgres locally, and DuckDB.

The free tiers are the ones that look most like a real job, and that is their whole appeal. They
also expire, require an account, meter the thing this project does most (full-table window
functions over a year of data), and make "clone the repo and run it" impossible for a reader.

## Decision

DuckDB, as a single file under `data/warehouse/`, with dbt-duckdb as the adapter.

## Consequences

- `git clone && python tasks.py all` reproduces the entire project on a laptop with no account
  anywhere. That is the single most valuable property for something whose purpose is to be read.
- It is genuinely fast at this shape of work — the full 365-day build with several multi-million-row
  window models runs in about forty minutes on a 4-core CI runner.
- **The settings block in `profiles.yml` is not tuning, it is survival.** dbt runs models
  concurrently and DuckDB will claim most of the machine's RAM across all threads at once; without
  an explicit `memory_limit` the build dies with an access violation rather than a clean
  out-of-memory error, which is a confusing way to learn you needed one.
- DuckDB files are **not byte-stable**: a no-op rebuild produces a different file. That single fact
  drives ADR-005, because it makes committing the deployed slice a permanent cost per rebuild.
- Storage is stable across the 1.x line, which is what lets an isolated Soda pinned to duckdb 1.0.0
  read a warehouse written by 1.5.5 (ADR-008). We verified that rather than assuming it.
- The honest limit: this is a single-node embedded engine. Nothing here demonstrates cluster
  behaviour, and the answer to "how would this work at 10x" is a real architectural answer rather
  than a bigger machine.
