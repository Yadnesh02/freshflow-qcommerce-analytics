# SQL showcase

Fifteen queries against the FreshFlow warehouse, each answering a question
somebody would actually ask before making a decision.

They are **executed by the test suite, not just stored here**
(`tests/test_sql_showcase.py`). Every file has to run against a built warehouse
and return rows, so a query that stops compiling when a column is renamed fails
CI rather than waiting to be discovered by whoever opens it next.

```bash
python tasks.py sql-showcase
```

Runs all fifteen against `data/warehouse/freshflow.duckdb` and prints the shape
of each result. `--warehouse` points it at another build.

## The header contract

Every file starts with three fields, and the tests enforce all three:

```sql
-- question:     what decision this supports, phrased as a question
-- technique:    the SQL mechanic being demonstrated
-- needs_days:   the shortest window that can answer it
-- needs_tables: optional — marts not every build produces
```

`needs_days` is what keeps the row-count assertion honest. CI builds a 30-day
slice and the full warehouse covers a year, so "returns rows" means different
things on each. A query declares the span it needs and is asserted non-empty
only where that span exists — on the full build, nothing is skipped.

`needs_tables` covers the other axis. `ci.yml` generates a slice and runs dbt
and stops; the five Sprint 4 optimisers run in `warehouse.yml`, so nine marts —
`mart_expiry_risk`, `mart_price_elasticity` and the four `rec_*` tables among
them — exist only on the full build. Queries 13 and 14 read those. On the CI
slice they are skipped by name; on the full build they are asserted like
everything else. A query that failed with a catalogue error there would be
indistinguishable from one with a typo in it.

## The queries

| # | Query | Technique |
|---|---|---|
| 01 | Does the stored running balance agree with a recomputation? | window frame, reconciled against a materialised column |
| 02 | Which batches will sell before they expire, under FEFO? | cumulative sum as queue position |
| 03 | Is retention improving for cohorts we acquired recently? | `PIVOT` into a cohort triangle, unobservable cells null |
| 04 | How long does a burst of attention on a product last? | gaps and islands by row-number differencing |
| 05 | Do stockouts come as long stretches or repeated dips? | gaps and islands over adjacent intervals |
| 06 | Which product pairs sell together more than chance predicts? | self-join with an ordered-pair guard, lift over independence |
| 07 | Which SKUs carry revenue, and which are predictable? | cumulative share for ABC, coefficient of variation for XYZ |
| 08 | Which categories are growing, net of the calendar? | `LAG` with a per-day normalisation |
| 09 | Where does demand leak — before the order, or at the pick face? | conditional aggregation across stages |
| 10 | What are the top three lines in each store? | `QUALIFY` with a deterministic tie-break |
| 11 | What is each store-SKU's current price, and did dedupe hold? | `ROW_NUMBER` for pick-latest, and inverted to find duplicates |
| 12 | What margin did we earn using the cost in force that day? | SCD2 point-in-time join vs the naive current-dimension join |
| 13 | How much at-risk stock is worth discounting? | scoring joined to the decision it produced |
| 14 | How much promo lift survives asking how promos were allocated? | naive contrast against the fitted coefficient |
| 15 | Does every unit leave through a door we can name? | full reconciliation with the residual reported, `FULL OUTER JOIN` |

## Three that are worth reading for the answer, not the syntax

**12 — the point-in-time join.** 321 SKUs changed landed cost mid-window. Using
today's cost for a year of sales misstates margin by up to 96% on the SKUs that
moved, and the error does not average out: cost changes are mostly increases.

**14 — promotion lift.** The naive before/after contrast reports lifts over
500%. The fitted elasticity for the same category and freshness band is around
−0.36 and fails identification in most cells. Discounts here land on stock that
is close to expiry, so the naive comparison contains the reason for the discount
as well as its effect.

**15 — the reconciliation.** `received = sold + written off + remaining` holds
on all 648,148 reconciled batches with a residual of exactly zero, and breaks on
all 6,509 the warehouse already flags `is_reconciled = false` — batches whose
receipt movement was lost to the injected null-`batch_id` defect. A
reconciliation that returns a residual instead of a boolean is what makes that
distinction visible.

## What is deliberately not here

**Year-over-year.** The simulated world runs 2025-09-01 to 2026-08-31 — exactly
twelve months — so every year-earlier comparison is null. Query 08 is
month-over-month and says so; the same `LAG` answers YoY with the offset changed
the moment a second year exists.

**A view → cart → checkout funnel.** The clickstream feed carries `pdp_view` and
`notify_me` and nothing between them and an order, and no `customer_id`. Query
09 is browse → ordered → served, and query 04 sessionises store-SKU attention
rather than users, because there is no user on the feed to sessionise.
