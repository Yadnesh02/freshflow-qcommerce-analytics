# ADR-006 — The north star is a rate net of wastage, declared before the experiment

**Status:** Accepted — and it cost us the headline

## Context

The project exists to improve a trade-off, so it needed a single metric to arbitrate. Several
candidates were defensible: gross margin in rupees, gross margin percent, wastage value, service
level, or a margin measure net of what was thrown away.

A metric chosen *after* seeing results is not a metric, it is a summary of whichever number came out
best. That failure mode is invisible in a portfolio project, because nobody sees the version where
the metric was different.

## Decision

**Gross margin after wastage and markdown (`gm_awm`)**, as a **rate**, written into
`semantic/metrics.yml` as the declared north star before any experiment code was written — with
90-day retention as the guardrail, because margin bought by starving stores of stock shows up as
churn rather than as failure.

## Consequences

- **The experiment then said the optimised policy loses on it**, and that is the whole point of
  having declared it first. Policy B raises availability 2.92pp and roughly doubles write-offs; on
  gross margin after wastage it is 1.37pp worse, significant across all thirty seeds.
- A levels metric and a rate metric can disagree, and this one did. `revenue − cogs` as a rupee
  *level* rises under Policy B, because Policy B sells more. Quoting that would have reported a
  clear win. **The project carried "+6.4% margin" as its headline for a while on exactly that
  mistake**, and the correction is recorded rather than quietly edited out.
- The guardrail turned out to be unanswerable by this design — retention is customer-level while
  the holdout randomises stores — so it is reported as not applicable with the reason, not filled
  in. Two of the plan's six metrics are in that state permanently.
- Declaring a rate rather than a level is what stops a policy buying a better number by spending
  more on stock. Everything else follows from that one property.
