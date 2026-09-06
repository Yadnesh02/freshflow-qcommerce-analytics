# Walkthrough video — shot list and script

**Target: 3 minutes.** OBS Studio, 1080p, system audio off, mic only. Record in one take per
section and cut between them; a clean cut is easier than a clean take.

Before recording: open the live app once and let it wake — Streamlit Cloud sleeps it and the first
load takes ~40 seconds while the container fetches its slice. Have `sql_showcase/12_scd2_point_in_time_margin.sql`
and the dbt docs lineage page open in tabs.

**Say the negative result out loud.** It is the strongest thing here and the most likely thing to be
edited out for looking bad. It does not look bad.

---

## 0:00–0:20 — The problem, over the Executive page

**Show:** the live app, Executive page.

> A quick-commerce dark store makes about a quarter of its money on things that expire in two to
> seven days. Stock too little and you lose the sale; stock too much and you bin it. This is
> fourteen simulated stores in Mumbai — forty-three crore of revenue, eighty lakh a year written off
> at expiry. Everything you're about to see is generated data, and I'll come back to why that's
> defensible.

## 0:20–0:50 — The action queue, and "see query"

**Show:** Expiry Control Tower. Scroll the queue. **Then open a "see query" expander** and let the
SQL sit on screen for three full seconds.

> This is the output that matters: every batch about to expire, ranked by rupees at risk, with hours
> remaining. Not a chart — a queue of decisions.
>
> And this is the part I'd point at in any interview. Every tile can show you the exact SQL that
> produced its number and the registry definition that compiled it. Full lineage from a pixel back
> to a dbt model. The dashboard imports no database driver at all — a test walks the syntax tree and
> fails the build if it ever does.

## 0:50–1:20 — The semantic layer

**Show:** `semantic/metrics.yml` in the editor, then `docs/metrics.md`.

> One YAML file defines every metric — numerator, denominator, grain, format, owner. The API, the
> dashboard, this generated dictionary and the tests all compile from it, so "wastage rate" cannot
> mean two things on two screens. CI fails if the committed dictionary has drifted from the
> registry.
>
> I wrote this before any pipeline code, so the pipeline conforms to the metrics rather than the
> other way round. That ordering matters for the next part.

## 1:20–2:10 — The experiment and the result

**Show:** the Executive page readout table, or `docs/business_case.md` §3.

> The recommendations feed back into the simulator's policy engine, so impact is measured rather
> than estimated. A store-level randomised holdout: half the estate switches, thirty independent
> worlds, difference-in-differences.
>
> And the optimised policy **lost**. It bought 2.9 points of availability by roughly doubling
> write-offs. On gross margin after wastage — the metric I declared in the registry before any of
> this ran — it came out 1.37 points worse, consistently across all thirty seeds.
>
> A margin figure in rupees would have shown a win, because the policy sells more. Declaring a rate
> net of wastage up front is what stopped me reporting one. Component attribution puts essentially
> all of the damage on the replenishment rule; the markdown engine actually helps, and it helps by
> recommending almost nothing — every fitted elasticity is inside the unit interval, so cutting
> price gives up more than it wins.

## 2:10–2:40 — The engineering underneath

**Show:** dbt docs lineage graph, then the Data Quality page.

> Underneath: fifty-four dbt models, three hundred and eighty tests, orchestrated as a Dagster asset
> graph. Eight defects are injected into the raw feeds on purpose — duplicate webhook events, null
> batch references, a timezone mix, a two-day collector outage — and each repair is asserted against
> the manifest's own row count. This page shows the ledger next to the freshness scan, because a
> page of green checks would hide the most interesting thing about the build.

## 2:40–3:00 — Reproducibility and the close

**Show:** the GitHub Actions runs list, green.

> Halfway through, the laptop's RAM turned out to be faulty — one bit flip put a store that doesn't
> exist into a mart, with every total still tying. So every published number now comes from a
> workflow anyone can re-run from a clone, with seven anchor figures that fail the build if they
> move.
>
> The policy failed. The measurement worked. I'd ship the expiry visibility, and I'd test the
> replenishment rule's service level before shipping any of it.

---

## After recording

- Upload unlisted, or to Loom (free tier caps at 5 minutes — fine for 3).
- Add the link to the README's **▶ The app** section, next to the live app link.
- Then tick the walkthrough line in `docs/PROJECT_1_PLAN.md` §14.
