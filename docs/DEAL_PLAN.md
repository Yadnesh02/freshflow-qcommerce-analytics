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
| **D3** | Retention readout — DiD on 90-day retention | `retention_90d` stops returning null, and parallel trends is checked on the 45-day pre-period rather than asserted | 1d |
| **D4** | Uplift model, Qini, targeting policy | Beats random targeting on Qini on a held-out set. If it does not, that is the finding and it is reported | 2d |
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
