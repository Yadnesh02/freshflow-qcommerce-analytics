# The eleven rupee question

**The deal rail loses ₹436,771 a year on its own line. This plan measures whether it earns that
back — and builds the model that decides who it is worth showing to.**

Sprints 0–5 built a dark-store analytics platform that answers five questions for every
`store × SKU × day`. That breadth is real and it stays. But four of those five stories compete for
one reader, and the sharpest unanswered question in the build turned out to be filed as S4.3, one
engine among four.

This document re-cuts the project around that question. Nothing is deleted; the platform is demoted
from headline to foundation, and the deal rail is promoted from engine to subject.

---

## 1. Why this question

Blinkit, Zepto and Swiggy Instamart all run a rupee-store rail: one deeply discounted hero SKU,
rotated daily, capped at one per order, funded by brand co-investment or platform subsidy. It is a
loss leader by construction. Nobody runs it expecting margin on the item.

They run it because it is supposed to be bought back twice — by the rest of the basket it pulls in,
and by the customers it brings back. Both halves are measurable. Neither is usually measured, and
in this build neither is measured properly either.

That is a good question to answer, because the honest answer is genuinely uncertain and it has a
decision attached at the end of it.

## 2. What is already true

Measured in the current build. Three of these four are solid.

| Quantity | Value | Source |
|---|---|---|
| Standalone P&L | **−₹436,771/yr** — 11,696 units, ₹127,820 revenue, ₹564,591 COGS | `fct_order_item` where `promo_id = 'PROMO-DEAL11'` |
| Uptake multiplier | **3.57×** median (IQR 2.47–5.71); 0.85 → 3.76 units/store-SKU-day | 45 SKUs with both dealt and undealt uncensored days |
| Reactivation | **2.1×** at a 30-day gap (5.31% vs 2.48%), 2.4× at 60 days. First-ever orders *lower* | `analytics/optimization/deal_slots.py` |
| Naive attach | **+₹6.27** rest-of-basket margin (₹77.90 vs ₹71.63) | Observational. **Upper bound, not an estimate.** |

And the finding that opens the story: **the rail is run as a gimmick rather than a system.** More
than half the slots over the year went to goods with ~800 days of shelf life, where clearance value
is exactly zero. On the as-of date, dealt SKUs ∩ at-risk SKUs was **empty**.

> **RESOLVED IN D2.** The `deal_slots.py` docstring claimed 12,153 units / ₹162,532 / ₹589,214 /
> −₹426,683 — ₹13.37 per unit on an ₹11 deal, which no definition of revenue on the warehouse
> reproduces. The correct figure is **−₹436,771 on 11,620 net units at exactly ₹11.00**, and it now
> comes from [`analytics/deal/pnl.py`](../analytics/deal/pnl.py), which asserts that revenue over
> net units equals the deal price before returning anything. The docstring is corrected and carries
> a note saying what it used to say.

## 3. The three gaps

| | Gap | Where it stands |
|---|---|---|
| **G1** | The attach number is not causal | Deal-takers self-select. `deal_slots.py` says so in its own docstring: "observational and generous — an upper bound on this term, not an estimate of it." The rail's main justification is its least rigorous number. |
| **G2** | Retention value is set to zero | `reactivation_value` is a declared parameter defaulting to **0**; `retention_90d` returns `None` in `mart_experiment_readout`. The thing the rail exists to buy is valued at nothing and measured not at all. |
| **G3** | Nobody is targeted | Allocation is store × SKU × day. There is no *who*. `mart_customer_360` already carries RFM, cohort, discount-dependency index and 90-day contribution, and nothing uses them. |

## 4. Cut, keep, build

**Cut** means demoted from the story, not deleted from the repo. The code stays, passes CI, and
lives under "the platform underneath" for anyone who digs.

### Cut from the headline

- Price elasticity as a headline chapter
- Demand & availability, forecast accuracy as headline metrics
- Transfer orders, purchase orders, markdown optimiser as headline engines
- Expiry control tower as the hero page
- Policy A/B ablation and the sensitivity sweep as headline — they become appendix

### Keep — load bearing

- Simulator, raw → staging → marts, dbt tests, CI
- `mart_customer_360` — the uplift feature table, already built
- Store-level holdout + the DiD estimator in `analytics/experiment/did.py`
- Metrics API and the semantic layer — the differentiator against a notebook
- `rec_deal_slot` PuLP allocator and its five constraints
- The Warehouse browser page

### Build

| | Piece | Closes |
|---|---|---|
| 1 | Customer-level randomised deal exposure in the simulator | unlocks G3 |
| 2 | Propensity-weighted attach estimate | G1 |
| 3 | Cannibalisation event study on dealt SKUs | missing P&L term |
| 4 | Retention readout — DiD on 90-day retention | G2 |
| 5 | Uplift model, Qini curve, targeting policy | G3 |
| 6 | Allocator objective on measured rather than declared coefficients | closes the loop |
| 7 | Three-page dashboard and the rewritten story | the deliverable |

## 5. The analysis, in four chapters

### Chapter 1 — What does the rail cost?

Largely done. The standalone P&L, the uptake multiplier and the slot-mix critique are measured. The
chapter's job is to set the question up honestly, including labelling +₹6.27 as the upper bound it
is rather than quietly using it as an estimate.

### Chapter 2 — What does it actually earn?

Four estimators on one question. They will not agree, and the disagreement is the finding — a single
confident number here would be the least believable outcome.

| Estimator | Status | What it does |
|---|---|---|
| Store-level holdout + DiD | **exists** | 180 days, 45 pre / 135 post, randomised at store level. Re-point it from wastage and availability to orders per customer, retention and contribution. |
| Propensity weighting for attach | build | Match deal-takers to non-takers on RFM, store, weekday and basket history; compare rest-of-basket margin. This is what replaces +₹6.27. |
| Within-customer pre/post | build | For redeemers, their own 30 days before against 30 after. Removes person-level selection but not timing — which is why it is one of four, not the answer. |
| Cannibalisation event study | build | Within-SKU, around slot days: how much full-price demand did the rail destroy? Currently an unmeasured term in a P&L that presents as complete. |

### Chapter 3 — Who should get it?

A T-learner uplift model on `mart_customer_360` features, predicting the **incremental** probability
of ordering in the next 7 days — not the probability of ordering, which is the mistake that funds
people who were coming anyway.

|  | Orders without the deal | Does not order without it |
|---|---|---|
| **Orders with the deal** | **Sure Things** — would have ordered anyway; every rupee of subsidy is pure loss | **Persuadables** — the entire value of the rail lives here |
| **No order with the deal** | **Sleeping Dogs** — the deal puts them off, training them to wait for discounts | **Lost Causes** — never order either way; excluding them is free money |

Evaluated on a **Qini curve** against a held-out set, which is the correct metric for uplift and the
one chart that makes the case visually. The payload is a policy: show the rail to the top *k*
deciles only, and report subsidy saved against incremental orders retained.

### Chapter 4 — Which SKU runs tomorrow?

The PuLP allocator already enforces five constraints — slots per store-day, one per L2 subcategory,
a minimum on-hand, a shelf-life floor, and a 30% private-label quota. It currently optimises against
*declared* coefficients. Re-point it at the measured ones from chapters 2 and 3 and the decision
becomes downstream of the evidence rather than parallel to it.

## 6. The dashboard, in three pages

Down from seven. Each answers its question in one screen.

| | Page | What it shows |
|---|---|---|
| 1 | **Does the rail pay?** | The deal's full P&L — subsidy and cannibalisation against measured attach and retention. The naive estimate sits beside the causal one; **the gap between them is the page.** |
| 2 | **Who should see it?** | Uplift deciles, the Qini curve, and what the targeting policy saves against showing it to everyone. |
| 3 | **What runs tomorrow?** | Per store, the recommended slots with the reason attached — expected uptake, clearance value, and which constraint bound. The decision, not the analysis. |

The existing seven move under a **Platform** heading. None is deleted.

## 7. Sequence and gates

Every sprint carries a gate that can fail, in the same convention as
[`EXECUTION_PLAN.md`](EXECUTION_PLAN.md). A sprint is not done until its gate could have failed and
did not.

| # | Sprint | Gate | Est |
|---|---|---|---|
| **D0** | Re-headline: README, the question, this plan | A reader lands on the repo and can state the question in one sentence | 0.5d |
| **D1** | Customer-level randomised deal exposure in the simulator | Covariate balance holds — standardised mean difference < 0.10 across **pre-treatment** attributes between exposed and control, and no deal-priced line ever reaches a held-out customer | 1d |
| **D2** | Causal attach + cannibalisation event study | ✅ Attach reported with a CI against the naive figure; P&L reconciliation resolved. **Full-year numbers need a post-D1 warehouse rebuild** | 1.5d |
| **D3** | Retention readout against D1's holdout | ✅ Retention measured with a CI and an MDE beside it; the mechanism behind the sign is identified rather than asserted | 1d |
| **D4** | Uplift model, Qini, targeting policy | ✅ Reported: **no estimator beats random**, with an oracle ceiling proving the effect is there to find | 2d |
| **D5** | Allocator re-pointed at measured coefficients | The deal P&L reconciles end to end: subsidy + cannibalisation against attach + retention, with no unexplained residual | 0.5d |
| **D6** | Three-page dashboard, README, the 60-second story | Each page answers its question above the fold | 1.5d |

> **A correction to this gate, made while building it.** It originally read "across RFM features".
> That was wrong: recency and frequency are exactly what the rail is supposed to move, so requiring
> them to balance *after* exposure would be requiring the treatment not to work. Balance belongs on
> attributes fixed before assignment — segment, home store, signup date, membership. The realised
> figures are max SMD **0.0283** across those four, on a 39,074 / 9,696 split.

### What D2 found, on a 90-day validation build

The estimand changed when D1 landed. The plan wrote this chapter as propensity weighting because
the only comparison available was deal-takers against non-takers; with a randomised holdout the
causal estimate is a difference between arms, and propensity weighting is kept only as the naive
contrast.

| | per assigned customer, 90 days | 95% CI |
|---|---|---|
| Orders | +0.10 | [−0.13, +0.32] — **not significant** |
| Revenue | +₹71.14 | [+18.17, +124.11] |
| Non-deal margin | +₹15.79 | [+3.61, +27.97] |
| Deal margin (the subsidy) | −₹2.79 | [−2.91, −2.67] |
| **Return per rupee of subsidy** | **4.66×** | **[0.30×, 9.02×]** |

Two things to carry forward rather than gloss. The **order count is not significant** at 90 days —
expected arithmetic after the recalibration below, not a broken estimator, and the full year has
roughly four times the data. And the **return's interval crosses 1×**: on this window the rail
cannot be shown to pay for itself at all. That is the honest headline until a full-year build
exists, and it is a better one than a confident number would have been.

The naive and causal figures are **not the same quantity**. +₹8.44 is per deal-taking *order*
(attach only, self-selected); +₹15.79 is per assigned *customer* (incidence plus attach). The
second contains the first. What the comparison shows is how much of the rail's value a per-order
view cannot see.

Cannibalisation, measured on the full warehouse: **2.9% of a block's lift** is paid for out of the
surrounding fortnight, and the days after a block are flat — which argues against pull-forward.

> **A calibration correction, recorded because it changed the answer.** D1's first response
> parameters produced a 25.3× return, which is not a finding — it is the config. They were shrunk
> 6.5× (share-weighted mean 1.169 → 1.026) so the rail is profitable but not absurd, and the spread
> between segments was widened relative to the mean so targeting still has something to find. A
> truly marginal rail is not reachable while `slots_per_store` is 1: the subsidy is ~₹3 a customer,
> so getting to 1× needs a 25-fold shrink that makes the effect statistically invisible. Widening
> the rail means changing the sprint 5 control arm, which would invalidate its published table.

### What D3 found, and a second gate correction

**The rail costs retention rather than buying it**, and the mechanism is the finding.

| outcome, per assigned customer | exposed | holdout | diff | 95% CI |
|---|---|---|---|---|
| Churned | 10.96% | 9.90% | **+1.06 pp** | [+0.38, +1.73] |
| Active in the last 30 days | 50.99% | 52.12% | **−1.14 pp** | [−2.25, −0.03] |
| Stockout-affected days | 1.089 | 0.936 | **+0.153** | [+0.123, +0.182] |

Minimum detectable effect on churn is 0.96 pp at 80% power, so the +1.06 pp sits
just above the floor this design can see — reported next to the estimate rather than left implied.

Redemption does lower hazard: the simulator applies 0.93 per redemption and redeemers churn at
7.94% against the holdout's 9.90%. But only 6.25% of exposed customers ever redeem, while **all**
of them get the demand lift — which concentrates onto a handful of dealt SKUs that then run dry.
`stockout_on_favourite_sku` is the largest term in the churn model. **The rail buys redemptions and
spends availability, and the second costs more than the first.**

That chain runs entirely through the simulator's pre-existing hazard logic. None of it was added
for this chapter.

> **Two gate corrections, both the same mistake.** This gate said "parallel trends is checked on the
> 45-day pre-period", which belongs to the *store-level* Policy A/B holdout. D1's holdout randomises
> customers from day one, so there is no pre-period and parallel trends is not an assumption it
> needs — randomisation does that work. The gate also said `retention_90d` stops returning null;
> that column lives in `mart_experiment_readout` and belongs to the Policy A/B experiment, which is
> a different question and **stays open**. Both gates were written before D1 existed, which is the
> same reason D1's own balance gate named RFM.

> **No CACE, and the reason is worth more than the number.** The holdout's redemption rate is
> exactly zero, so a Wald ratio looks ideal. It is not: exposure reaches churn through availability
> as well as through redemption, so the exclusion restriction fails by construction. Exposed
> customers who never redeemed still carry 1.0094 stockout days against the holdout's 0.9364.
> Dividing by compliance anyway would have reported **+16.88 pp** and attributed an arm-wide effect
> to the 6.25% who took the deal. The module keeps that number visible next to the reason it is
> unusable, so the next person does not recompute it.

**This sharpens D4 rather than blocking it.** D2 says the rail returns 4.66× on margin inside the
window; D3 says it costs retention beyond it. Targeting is now the question of *which customers the
margin gain outruns the churn cost for* — which is a far better brief for an uplift model than
"find the responders".

### What D4 found: the null, and why it is informative

Three estimators on a held-out 30% of customers, all reproducible:

| estimator | Qini on margin | verdict |
|---|---|---|
| T-learner | −77,244 | worse than random |
| X-learner | −60,998 | worse than random |
| Two-stage (behavioural clusters) | −41,051 | worse than random |
| *Oracle — true latent segment* | *+149,419* | *beats random* |

**A null is only informative next to a ceiling.** Without the oracle, "no model worked" and "there
was nothing to find" are indistinguishable and imply opposite next steps. The oracle settles it: the
heterogeneity is real, large and correctly ordered against the configured response —

| segment | configured | actual margin uplift |
|---|---|---|
| deal_hunter | 1.120 | +₹76.6 ± 47 |
| price_sensitive | 1.055 | +₹24.5 ± 28 |
| bulk_planner | 1.015 | −₹3.9 ± 32 |
| convenience | 1.000 | −₹47.7 ± 40 |
| premium | 0.955 | −₹67.7 ± 39 |

— a ₹144 spread. What is missing is the ability to tell *who is who* from behaviour alone.
A supervised classifier recovers the latent segment at 77.4% accuracy against a 29.9% baseline, and
ranking on its predictions reaches a Qini of **+32,933**. Unsupervised clustering on the same
features reaches only −41,051. **The signal is in the data; it is not reachable without a label
nothing in the warehouse carries.** That is the finding, and it names its own next step: find a
behavioural proxy for the segment, or lengthen the burn-in until clustering separates them.

The root cause is signal-to-noise. The per-customer margin effect is **+₹3.69** against an outcome
standard deviation of **₹706** — 0.5% of one SD. A T-learner differences two models fitted on that;
the difference is mostly the difference of their errors.

> **Three reproducibility bugs, found because the numbers moved.** The first run reported the
> T-learner's margin Qini at 8,645; a rerun on identical data said 39,872, then 80,445. All three
> "beats random" verdicts in that run were noise. The causes, in the order they were found: the
> panel query had **no `ORDER BY`**, so DuckDB returned rows in a different order each process and
> the train/test split landed on different customers; LightGBM's **thread count** was unpinned
> (`n_jobs`, not `num_threads`, is the sklearn wrapper's name for it); and DuckDB **sums floats in
> parallel**, so `sum(gross_margin)` differed in its last bits between runs — enough, on a signal
> this small, to move every split point. Monetary sums are now rounded to the paisa, which they
> should have been anyway. Three processes now agree to the decimal.

### The longer burn-in, and a fourth estimator

Both were tried after D4's null. The burn-in was doubled to 90 days and the simulation extended to
225 so the post-period stayed pinned at 135 — otherwise the comparison would have traded outcome
window for feature window and said nothing. A fourth estimator was added on a different principle:
the Horvitz-Thompson transform, `Y* = Y(T−p)/p(1−p)`, whose conditional mean **is** the uplift, so
its splits chase effect heterogeneity rather than outcome level.

| | 45-day burn-in | 90-day burn-in |
|---|---|---|
| Segment recovery (supervised, from features) | 77.4% | **84.1%** |
| Oracle Qini (true segment) | +149,419 | **+96,764** |
| T-learner | −77,244 | −34,852 |
| X-learner | −60,998 | −25,525 |
| Two-stage clustering | −41,051 | −66,315 |
| Transformed outcome | — | −92,270 |

**The burn-in works for what it was supposed to do and does not fix the problem.** Identification
improved by nearly seven points; the T- and X-learners roughly halved their deficit; and still
nothing beats random. The transformed outcome, despite being the only estimator whose target is the
effect itself, came last — its variance at p=0.80 costs more than its objective buys.

So the gap is now precisely located. It is **not** the features (84% of customers are identifiable),
**not** the burn-in, and **not** the absence of signal (the oracle clears +96,764, and segment-level
uplift still runs from +₹58.5 for deal_hunter to −₹97.1 for premium). It is the step between: no
estimator tried converts feature-level identification into effect-level ranking. The one method
class that attacks exactly that — a causal tree, splitting on treatment-effect heterogeneity rather
than on outcome variance or feature-space variance — has not been tried.

That is where this chapter stops, with the next step named rather than taken.

### One hard dependency

**D1 blocks D4.** Proper uplift needs customer-level random assignment, and the simulator currently
randomises at *store* level. Without D1, chapter 3 is observational and inherits exactly the problem
chapter 2 exists to fix. It is the one genuinely new piece of simulation, and it is small.

## 8. What this looks like when it is done

> "A rupee-store rail loses four lakh a year on its own line. I measured what it actually earns back
> — and the honest number is well under what the naive comparison claims. Then I built an uplift
> model that keeps most of the benefit for a fraction of the subsidy, by only showing it to the
> customers who move."

That sentence survives either outcome. If the rail pays, the project found by how much and sharpened
it. If it does not, the project found that a loss leader everyone runs on instinct does not pay —
and still shipped a targeting policy that makes it defensible.

The negative result is not the risk here. It is the second-best headline, and this build has already
shown it can carry one.
