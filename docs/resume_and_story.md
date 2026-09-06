# Résumé bullets and the 60-second story

Every number here is traceable to `mart_experiment_readout` or the anchors report, both produced by
workflow runs. **Nothing in this file was typed from a laptop build.** If a figure moves, re-run
`python tasks.py published` against a clean-runner build and correct this file — do not correct the
anchors.

---

## Résumé bullets

Pick three or four. They are ordered by how well they survive being interrogated.

> **Built a closed-loop policy backtest for a simulated 14-store q-commerce network and measured a
> proposed replenishment-and-markdown policy against the status quo across 30 seeded worlds — the
> policy raised availability 2.9pp but doubled write-offs, coming out 1.35pp worse on gross margin
> after wastage, the metric declared before the experiment ran.**

> **Designed a YAML metric registry and SQL resolver that the API, dashboard, generated dictionary
> and test suite all compile from, so a metric cannot drift between surfaces; enforced structurally
> — a test fails the build if any dashboard module imports a database driver, and every API response
> echoes the SQL and registry definition behind its number.**

> **Modelled batch-level perishable inventory with FEFO allocation and expiry attribution across
> 6.3M store-SKU-days, and shipped a rupee-valued action queue ranking every at-risk batch by money
> and hours remaining; validated the risk ranking against realised write-off in the following seven
> days (0%, 0%, 0%, 1.7%, 42% across bands).**

> **Corrected censored demand using a fitted intraday arrival curve rather than a flat
> hours-remaining multiplier, moving measured lost sales from 94,350 to 213,230 units — and *down*
> for evening stockouts, which a flat multiplier cannot do.**

> **Built the data-quality layer against eight defects injected into the raw feeds on purpose —
> duplicate webhook events, null batch references, a timezone mix, a mid-year SKU migration, a
> two-day collector outage — with each repair asserted against the manifest's own row count.**

> **Ran the full pipeline as reproducible GitHub Actions workflows after a hardware fault made
> laptop-produced numbers untrustworthy: seven pinned anchor figures fail the build if any moves, so
> every published figure is traceable to a run anyone can re-execute from a clone.**

### Bullets to avoid

- ~~"+6.4% margin"~~ — that was `revenue − cogs` as a rupee *level*, which rises because the policy
  sells more. The declared north star is a rate net of wastage, and on it the policy **loses**.
- ~~"reduced wastage"~~ — it did not. It roughly doubled write-offs. The defensible framing is the
  trade, stated in full.
- ~~"fewer units expired"~~ — `units_expired` never clears p < 0.05 under sensitivity. It is not a
  claim this project can make.

---

## The 60-second story

*Say it in this order. The negative result is the strongest part — lead toward it, not away.*

**The problem (10s).** A quick-commerce dark store makes a quarter of its money on things that go
off in two to seven days. Stock too little and you lose the sale; stock too much and you bin it.
Fourteen stores, ₹43 crore of revenue, ₹80 lakh a year written off at expiry.

**What I built (15s).** A full stack: a simulator that generates a year of realistic, deliberately
dirty events; a dbt warehouse with batch-level expiry attribution; a demand forecast; and five
decision engines — replenishment, markdown, deal slots, transfers, targeting — feeding a
rupee-valued action queue. Every number on the dashboard is compiled from one YAML metric registry,
and the app cannot reach the database except through that.

**How I proved it (15s).** The recommendations feed back into the simulator's policy engine, so
impact is measured rather than estimated. A store-level randomised holdout — half the estate
switching, thirty independent worlds, difference-in-differences — plus a sensitivity sweep and
per-component attribution.

**What it found (15s).** The optimised policy **lost**. It bought 2.9 points of availability by
roughly doubling wastage, and on gross margin after wastage — the metric I declared in the registry
before running anything — it came out 1.35 points worse, consistently across all thirty seeds. The
attribution puts essentially all of it on the newsvendor. The markdown engine came out ahead
precisely by recommending almost nothing, because every fitted elasticity is inside the unit
interval.

**Why that is the interesting answer (5s).** A margin figure in rupees would have shown a win,
because the policy sells more. I declared a rate net of wastage up front, which is what stopped me
reporting one. What I would ship is the expiry visibility — it needs no model to be trusted — and
what I would test next is the newsvendor's service level.

---

## The questions this invites, and the answers

**"So your project failed?"**
The policy failed; the measurement worked. I built the apparatus that could tell the difference and
it told me something I did not want to hear, consistently across thirty worlds. A pipeline that can
only confirm the thing it was built to confirm is worth less than one that can refute it.

**"Why should I believe a simulated result?"**
You should not believe the *magnitudes*. What is real is the method: the analytics layer cannot
import the simulator and a test enforces it, the metric was declared before the experiment, and
every figure is reproducible from a seed in CI. On real data the same harness runs unchanged.

**"What would you do differently?"**
Randomise price. The elasticities are fitted on observational variation, so they are confounded —
prices moved for reasons correlated with demand. A naive before-and-after reports lifts over 500%
where the fitted coefficient is around −0.36 and fails identification in most cells. That gap is the
entire case for a price test on a store subset before any network rollout.
