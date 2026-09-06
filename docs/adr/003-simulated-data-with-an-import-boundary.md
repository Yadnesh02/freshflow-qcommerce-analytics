# ADR-003 — Simulated data, with an enforced import boundary

**Status:** Accepted

## Context

No public dataset carries what this project is about: batch-level perishable inventory with expiry
dates, FEFO allocation, hourly stockout intervals, and the counterfactual needed to measure a
policy against the status quo. Kaggle retail sets have none of it.

Simulated data invites the obvious objection — *you made the data, so of course your model works* —
and that objection is correct unless something structural prevents it.

## Decision

A purpose-built simulator under `simulator/`, generating a year of a 14-store network, **and a hard
rule that `analytics/` may never import from `simulator/`**, enforced by a test that walks the
import graph.

The raw layer is also deliberately dirtied: eight defects injected on purpose — duplicate webhook
events, late arrivals, null batch references, unit drift, two encodings of returns, a timezone mix,
a mid-year SKU identifier migration, and a two-day clickstream outage — each recorded in a manifest
with the row count it damaged.

## Consequences

- The analytics layer cannot read the parameters that generated the data, so a model cannot
  accidentally be handed the answer. That turns "I made the data" from a fatal objection into a
  design decision with a test behind it.
- The injected defects make the staging layer demonstrable rather than claimed: each repair is
  asserted against the manifest's own count, so "we handle dirty data" is a number rather than a
  sentence. The Data Quality page renders the ledger next to the scan for the same reason.
- The clickstream outage is deliberately **not** backfilled. A gap that is quietly filled in is
  indistinguishable afterwards from one that never happened, and the coverage check warns about it
  on every healthy build.
- **What this still cannot do**, and the business case says so: causal claims about customer
  behaviour are claims about the simulator's hazard model. Stockout-to-churn is generated that way,
  so measuring it proves the code works, not that the effect is real.
