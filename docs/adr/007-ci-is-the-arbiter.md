# ADR-007 — Every published number comes from a workflow, not a laptop

**Status:** Accepted under duress

## Context

On 2026-09-05, `mdsched` confirmed the development laptop has faulty non-ECC RAM — Windows events
1102 and 1202. The machine is a work machine and cannot be repaired by us.

**The failure that matters was never a crash.** One row of `agg_store_sku_day` carried store
`FF-LPA-00`, a store that does not exist, produced by a single `0x31 → 0x30` bit flip between
reading a parquet file and writing a table. Row count unchanged. Net revenue unchanged. Every total
still tied. It was caught by one foreign-key test and by nothing else.

A number produced on that machine cannot be distinguished from a correct one by looking at it.

## Decision

**Any number reaching the README, the app or a résumé is produced by a workflow file.**
`warehouse.yml` is that file for everything derived from the warehouse: a full-year build from a
seed, against the committed lockfile, on a clean runner. Seven anchor figures are pinned and the
build fails if any of them moves.

Read this as a strengthening of gate G5 rather than a workaround for broken hardware. A build that
runs in CI from a clone is one anyone can reproduce. A build that ran on somebody's laptop is a
number asking to be trusted.

## Consequences

- **Local test failures are no longer evidence of anything on their own.** A local `dbt build` gave
  "172 passed, 9 errors, 236 skipped" while the same commit was fully green on a clean runner. The
  rule that followed is blunt: never diagnose from a local build.
- The laptop's warehouse is now demonstrably a *stale* build as well as a suspect one — it fails
  five of the seven anchors and reproduces the figures the documents were written from. When the two
  disagree, `anchors.py` prints the rule in its own output: correct the documents, not the anchors.
- The reports a documentation pass needs are tee'd into the build artifact, because reading a step's
  output requires a browser and a sign-in. One download replaces re-typing a table — and the first
  version of that tee shipped without ever running, which is its own lesson.
- The cost is latency: a full rebuild is ~40 minutes and `warehouse.yml` is `workflow_dispatch` only,
  because it replaces artefacts the live app serves and that should be a decision rather than
  something that happens on every push.
- The anchors check is **blocking** and the published-figures report is **not**. A moved anchor must
  stop the run before recommendations regenerate; a report that fails a build teaches people to skip
  reports.
