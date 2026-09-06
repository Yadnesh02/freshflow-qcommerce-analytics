# FreshFlow — the business case

**A 14-store Mumbai dark-store network. ₹43.2 Cr of annual revenue, ₹80.6 L written off at expiry.**
This is what we tested, what it found, and what we would ship.

*Source of every figure: `mart_experiment_readout` and the anchors report, both produced by
workflow runs anyone can re-run from a clone. Nothing here was typed from a laptop build.*

---

## 1. The problem

A quarter of GMV is perishable — dairy, bakery, fresh — on a 2–7 day shelf life with a 100%
write-off at expiry. That creates a tension no single lever resolves:

> Stock enough to never go out of stock, without stocking so much that perishables expire.

Today the network sits at one end of it. **Availability is 79.1%** — roughly one in five
store-SKU-days cannot serve what was asked for — against **wastage of 1.9% of revenue, ₹80.6 L a
year**. Stores are running lean and paying for it in lost sales rather than in bins.

The obvious move is to order more, mark down what is at risk, and move stock between stores. We
built exactly that as "Policy B" — a newsvendor replenishment rule with a perishable critical ratio,
a markdown optimiser, a deal-slot allocator and an inter-store transfer engine — and measured it
against the status quo.

## 2. How we measured it

**A store-level randomised holdout, not a before-and-after.** Fourteen stores, 180 days, half
switching to Policy B on day 46 and the rest never switching. Thirty independent worlds. The
estimate is a difference-in-differences, so it removes both the pre-existing gap between the two
groups and anything that hit both on the same day.

The metric that decides it — **gross margin after wastage and markdown** — was written into
`semantic/metrics.yml` as the north star **before any of this ran**. That ordering is the whole
discipline: it is what stops a result being re-framed once it is known.

## 3. What it found

**Policy B loses on the metric we declared.**

| | Status quo | Policy B | Difference | 95% interval |
|---|---|---|---|---|
| **Gross margin after wastage** — *north star* | 22.22% | 20.76% | **−1.35pp** | [−1.62, −1.08] |
| Availability | 79.14% | 82.00% | **+2.94pp** | [+2.77, +3.11] |
| Wastage, as a share of revenue | 1.90% | 3.74% | **+1.80pp** | [+1.59, +2.01] |
| Markdown subsidy per store-day | ₹1,255.27 | ₹325.46 | **−₹1,040.97** | [−1,229, −852] |

Every row is significant across all thirty seeds.

Read plainly: **Policy B buys 2.9 points of availability by roughly doubling what we throw away.**
On the rate that nets wastage off margin, it is 1.35 points worse than doing nothing.

Component attribution puts **essentially all of the damage on the newsvendor** — it trades wastage
for availability by design, and at this shelf-life profile the trade is not worth making. The
markdown optimiser is the largest *positive* contributor, and it earns that by recommending almost
nothing: every fitted price elasticity sits inside the unit interval, so while stock is short of
demand a discount gives up more on the units already selling than it wins on the ones it attracts.
The transfer engine contributes nothing measurable at all.

> **A margin figure quoted in rupees would have shown a gain**, because Policy B sells more. The
> north star is a *rate net of wastage* precisely so that a policy cannot buy its way to a better
> number by spending more on stock.

## 4. What we would ship

**Ship expiry visibility now.** The batch-level expiry ledger and the ranked, rupee-valued action
queue need no model to be trusted: they say which batches are at risk, how much money is on them,
and how many hours are left. Validated against the seven days after scoring, realised write-off
across risk bands runs 0%, 0%, 0%, 1.7%, 42% — the ranking separates. It changes behaviour on day
one and it is the highest-confidence component we have.

**Do not ship the newsvendor as configured.** It is the component the evidence is against. Its
service level is set from shelf life alone; the experiment says that is too aggressive for this
assortment.

**Treat the markdown engine as a measurement, not a lever.** It was built to find discount
opportunities and its finding is that there are almost none. Sensitivity confirms it: raising the
disposal cost from ₹0 to ₹50 per unit barely moves the result. That is a genuine answer to "should
we discount our way out of expiry", and the answer is no.

## 5. What would change the answer

Three things, in the order we would do them:

1. **Re-tune the newsvendor's service level and re-run.** The harness is a workflow; a new setting
   is thirty jobs and about seven minutes. The question is not whether replenishment can help but
   at what service level it stops paying for itself.
2. **Randomise price to identify elasticity.** Today's coefficients are fitted on observational
   variation — prices moved for reasons correlated with demand. A price test on a store subset
   would tell us whether the markdown engine is right or merely unidentified. A naive
   before-and-after on the current data reports lifts above 500%; the fitted coefficient for the
   same cells is around −0.36 and fails identification in most of them. The gap between those two
   numbers is the entire case for running the test.
3. **Price the availability we are buying.** −1.35pp of margin for +2.9pp of availability is only a
   bad trade if a point of availability is worth less than half a point of margin. We do not know
   what it is worth, because that is a retention question and this design randomises stores rather
   than customers. Until someone puts a number on it, the north star is the right arbiter.

## 6. The honest caveats

- **The data is simulated.** A purpose-built simulator generates every order; no real company data
  is used. The analytics layer is architecturally forbidden from importing the simulator and a test
  enforces it, so results are not circular — but they are results about a model of a business.
- **Delivery cost is assumed, not measured** — ₹42 per order, declared in one place. The customer
  value ranking turns on it: the bands reorder at ₹34, so the declared figure sits just above the
  crossover.
- **Two of the six planned metrics cannot be answered by this design** and are reported as such
  rather than filled in. Ninety-day retention is a customer-level measure while the holdout
  randomises stores; forecast accuracy has no status-quo column because the status quo does not
  forecast.

---

*Everything above is reproducible: `python tasks.py all` rebuilds the warehouse from a seed, and the
experiment, holdout, sensitivity and ablation each run as a GitHub Actions workflow over thirty
seeds. The readout table is committed, so the numbers in this document rebuild on any clone.*
