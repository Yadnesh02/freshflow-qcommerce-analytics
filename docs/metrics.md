# Metric Dictionary

> **Generated file — do not edit.**
> Source of truth is [`semantic/metrics.yml`](../semantic/metrics.yml).
> Regenerate with `python tasks.py docs`.

**31 metrics** across **9 families**, sliceable by **30 dimensions**.

- **North star:** `gm_awm` — Gross Margin after Wastage & Markdown
- **Guardrail:** `retention_90d` — 90-Day Retention

The guardrail exists because the north star can be gamed: margin bought by starving stores of stock shows up as churn, not as success.

---

## Executive

*The P&L spine. Every other family rolls up into these.*

### `aov` — Average Order Value

Net revenue per delivered order. Read next to basket size uplift: AOV can rise simply because cheap items went out of stock.

| | |
|---|---|
| **Formula** | `SUM(net_revenue)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(orders_count), 0)` |
| **Source** | `mart_order_daily` |
| **Grain** | `store`, `date_day` |
| **Format** | `inr` |
| **Direction** | higher is better |
| **Expected range** | 50 to 5000 |
| **Owner** | analytics |

> ⚠ mart_order_daily is pre-aggregated to store x day, so orders are summed from a count column rather than counted distinct. Counting DISTINCT order_id here would be a column that does not exist at this grain. orders_count is DELIVERED orders. 76,121 headers (4.9%) carry a basket the customer built and no fulfilled line - a total stockout at pick time - and they are excluded because this metric is defined per delivered order. Dividing by every header instead reads Rs 278.29 against Rs 292.56, so the choice moves the tile by 5.1%; mart_order_daily.placed_orders and .unfulfilled_orders carry the other denominator rather than hiding it.

### `cogs_value` — COGS

Landed cost of units sold, batch-attributed via FEFO allocation.

| | |
|---|---|
| **Formula** | `SUM(cogs)` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `date_day` |
| **Format** | `inr` |
| **Direction** | lower is better |
| **Expected range** | — |
| **Owner** | analytics |

### `gm_awm` — Gross Margin after Wastage & Markdown · **north star**

Net revenue less COGS, expiry write-offs and platform-funded markdown subsidy, as a share of net revenue. The project's north star: it is the only metric that cannot be gamed by trading one problem for another. Cutting stock lowers wastage but starves revenue; discounting deeply clears stock but burns subsidy. Both show up here.

| | |
|---|---|
| **Formula** | `SUM(net_revenue - cogs - writeoff_value - markdown_subsidy_platform)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(net_revenue), 0)` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `date_day` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | -0.2 to 0.6 |
| **Owner** | analytics |
| **Guarded by** | `retention_90d` |

> ⚠ Brand-funded markdown is excluded from the subsidy term because the platform does not pay for it. Check funding_source before changing this.

### `gross_margin_pct` — Gross Margin %

Margin before wastage and subsidy. Shown next to GM-AWM deliberately: the gap between the two is the cost of the perishability problem.

| | |
|---|---|
| **Formula** | `SUM(net_revenue - cogs)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(net_revenue), 0)` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `date_day` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | -0.1 to 0.7 |
| **Owner** | analytics |

### `net_revenue` — Net Revenue

Realized item revenue after discounts, excluding delivery fees and GST. The revenue line every margin ratio is divided by.

| | |
|---|---|
| **Formula** | `SUM(net_revenue)` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `date_day` |
| **Format** | `inr` |
| **Direction** | higher is better |
| **Expected range** | — |
| **Owner** | analytics |

---

## Wastage

*Problem P1 - perishables written off at 100% of landed cost.*

### `dte_at_sale_p10` — Days to Expiry at Sale (P10)

10th percentile of remaining shelf life at the moment of sale. The customer-experience metric behind wastage: the mean hides the tail, and the tail is what gets a one-star review.

| | |
|---|---|
| **Formula** | `QUANTILE_CONT(dte_at_sale, 0.10)` |
| **Source** | `fct_order_item` |
| **Grain** | `store`, `category`, `week` |
| **Format** | `days_1dp` |
| **Direction** | higher is better |
| **Expected range** | 0 to 60 |
| **Owner** | analytics |

### `expiry_value_at_risk` — Expiry Value at Risk

Landed-cost value of on-hand units the forecast says will not sell before expiry. The number the action queue is ranked by.

| | |
|---|---|
| **Formula** | `SUM(units_at_risk * unit_landed_cost)` |
| **Source** | `mart_expiry_risk` |
| **Grain** | `store`, `category`, `date_day` |
| **Format** | `inr` |
| **Direction** | lower is better |
| **Expected range** | — |
| **Owner** | analytics |

### `sell_through_rate` — Sell-Through Rate

Units sold as a share of units available (opening stock plus receipts).

| | |
|---|---|
| **Formula** | `SUM(units_sold)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(opening_units + received_units), 0)` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `sku`, `week` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | 0.0 to 1.0 |
| **Owner** | analytics |

> ⚠ The denominator must include opening stock. Dropping it flatters the number on every SKU that carries inventory across days.

### `wastage_rate_value` — Wastage Rate (value)

Value of expiry write-offs as a share of the value of inventory received. Measured in rupees, not units - one wasted paneer is not one wasted banana.

| | |
|---|---|
| **Formula** | `SUM(writeoff_value)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(received_value), 0)` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `category`, `week` |
| **Format** | `percent_1dp` |
| **Direction** | lower is better |
| **Expected range** | 0.0 to 0.35 |
| **Owner** | analytics |

> ⚠ Denominator is received value, not sold value. Using sold value makes the rate fall when sales fall, which is exactly backwards.

### `wastage_value` — Wastage Value

Rupee value of inventory written off at expiry.

| | |
|---|---|
| **Formula** | `SUM(writeoff_value)` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `category`, `date_day` |
| **Format** | `inr` |
| **Direction** | lower is better |
| **Expected range** | — |
| **Owner** | analytics |

---

## Availability

*Problems P3 and P4 - stock in the wrong place, or not there at all.*

### `days_of_cover` — Days of Cover

On-hand units divided by trailing 7-day average daily sales, capped at remaining shelf life. Twenty days of cover on a three-day product is not twenty days of cover - it is nineteen days of write-off.

| | |
|---|---|
| **Formula** | `SUM(closing_units)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(trailing_7d_avg_units), 0)` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `sku`, `date_day` |
| **Format** | `days_1dp` |
| **Direction** | context-dependent |
| **Expected range** | 0 to 400 |
| **Owner** | analytics |

### `fill_rate` — Fill Rate

Units fulfilled as a share of units demanded. Uses imputed demand, so it counts what customers wanted, not only what they managed to buy.

| | |
|---|---|
| **Formula** | `SUM(units_sold)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(units_demanded_imputed), 0)` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `sku`, `date_day` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | 0.0 to 1.0 |
| **Owner** | analytics |

> ⚠ Denominator is the arrival-curve imputation from S3.1, not observed sales - so a store that stocked out at 08:00 shows the fill rate its customers experienced rather than 100%. agg_store_sku_day. demand_imputation_method says which estimator each row used.

### `in_stock_pct` — In-Stock %

Share of assortment hours where on-hand was above zero. Time-weighted, not a midnight snapshot - a snapshot at 00:00 cannot see the 8pm stockout that actually cost the order.

| | |
|---|---|
| **Formula** | `SUM(CASE WHEN on_hand_units > 0 THEN 1 ELSE 0 END)`<br>&nbsp;&nbsp;÷&nbsp;`COUNT(*)` |
| **Source** | `fct_availability_hour` |
| **Grain** | `store`, `sku`, `date_day` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | 0.0 to 1.0 |
| **Owner** | analytics |

### `lost_sales_units` — Lost Sales (units)

Imputed demand that went unfulfilled because stock hit zero.

| | |
|---|---|
| **Formula** | `SUM(GREATEST(units_demanded_imputed - units_sold, 0))` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `sku`, `date_day` |
| **Format** | `number_0dp` |
| **Direction** | lower is better |
| **Expected range** | — |
| **Owner** | analytics |

---

## Forecast

*Problem P4 - you cannot mark down your way out of a bad purchase order.*

### `forecast_bias` — Forecast Bias

Signed error as a share of actuals. Separated from WAPE because a model can be accurate and consistently low, which quietly under-orders forever.

| | |
|---|---|
| **Formula** | `SUM(forecast_units - actual_units)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(actual_units), 0)` |
| **Source** | `mart_forecast_accuracy` |
| **Grain** | `store`, `sku`, `date_day` |
| **Format** | `percent_1dp` |
| **Direction** | context-dependent |
| **Expected range** | -1.0 to 1.0 |
| **Owner** | analytics |

### `forecast_value_add` — Forecast Value Add

Seasonal-naive WAPE minus model WAPE. If this is not positive the model is theatre, and the honest move is to report that and use the naive rule.

| | |
|---|---|
| **Formula** | `SUM(ABS(actual_units - naive_units)) - SUM(ABS(actual_units - forecast_units))`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(actual_units), 0)` |
| **Source** | `mart_forecast_accuracy` |
| **Grain** | `abc_class`, `xyz_class` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | — |
| **Owner** | analytics |

### `forecast_wape` — Forecast WAPE

Weighted absolute percentage error. WAPE not MAPE: MAPE divides by actuals per row and explodes on the low-volume SKUs that make up most of the tail.

| | |
|---|---|
| **Formula** | `SUM(ABS(actual_units - forecast_units))`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(actual_units), 0)` |
| **Source** | `mart_forecast_accuracy` |
| **Grain** | `store`, `sku`, `horizon_days`, `date_day` |
| **Format** | `percent_1dp` |
| **Direction** | lower is better |
| **Expected range** | 0.0 to 3.0 |
| **Owner** | analytics |

> ⚠ Evaluate on uncensored days only, or you are grading the model on stockouts.

---

## Pricing

*Problem P2 - flat discount ladders destroy margin and still waste stock.*

### `discount_leakage_value` — Discount Leakage

Discount rupees given to units the model says would have sold at full price anyway. The metric that proves the optimizer is smart rather than merely generous - a flat markdown ladder maximises this without noticing.

| | |
|---|---|
| **Formula** | `SUM(CASE WHEN p_sell_full_price > 0.80 THEN discount_value ELSE 0 END)` |
| **Source** | `mart_markdown_perf` |
| **Grain** | `store`, `sku`, `week` |
| **Format** | `inr` |
| **Direction** | lower is better |
| **Expected range** | 0 to 10000000 |
| **Owner** | analytics |

### `margin_recovery_rate` — Margin Recovery Rate

Gross margin realized on marked-down units against their landed cost. The counterfactual is minus one hundred percent, because the alternative to a bad markdown is a total write-off.

| | |
|---|---|
| **Formula** | `SUM(realized_revenue - landed_cost_value)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(landed_cost_value), 0)` |
| **Source** | `mart_markdown_perf` |
| **Grain** | `store`, `category`, `week` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | -1.0 to 2.0 |
| **Owner** | analytics |

### `markdown_depth` — Markdown Depth

Unit-weighted discount off base price on marked-down units.

| | |
|---|---|
| **Formula** | `SUM((base_price_avg - realized_price_avg) * markdown_units)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(base_price_avg * markdown_units), 0)` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `category`, `date_day` |
| **Format** | `percent_1dp` |
| **Direction** | context-dependent |
| **Expected range** | 0.0 to 0.9 |
| **Owner** | analytics |

### `markdown_subsidy_value` — Markdown Subsidy (platform-funded)

Rupee cost of discount the platform pays for, excluding brand-funded promotions.

| | |
|---|---|
| **Formula** | `SUM(markdown_subsidy_platform)` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `category`, `date_day` |
| **Format** | `inr` |
| **Direction** | lower is better |
| **Expected range** | — |
| **Owner** | analytics |

---

## Promotion

*Problem P6 - the deal slot as a system, not a gimmick.*

### `basket_size_uplift` — Basket Size Uplift

Net revenue per order on deal-containing orders versus control orders.

| | |
|---|---|
| **Formula** | `SUM(treated_basket_value) - SUM(control_basket_value)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(control_basket_value), 0)` |
| **Source** | `mart_deal_slot_perf` |
| **Grain** | `store`, `week` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | — |
| **Owner** | analytics |
| **Requires** | a control arm — meaningless without a holdout |

### `subsidy_per_incremental_order` — Subsidy per Incremental Order

Deal subsidy divided by orders above the control-implied baseline. Without a holdout this number is fiction, so the denominator is defined against the experiment control arm, never against a naive period-over-period lift.

| | |
|---|---|
| **Formula** | `SUM(subsidy_value)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(orders - control_implied_orders), 0)` |
| **Source** | `mart_deal_slot_perf` |
| **Grain** | `store`, `week` |
| **Format** | `inr` |
| **Direction** | lower is better |
| **Expected range** | — |
| **Owner** | analytics |
| **Requires** | a control arm — meaningless without a holdout |

---

## Merchandising

*Problem P5 - private label mix, net of cannibalisation.*

### `private_label_gmv_share` — Private Label GMV Share

Share of net revenue from platform-owned brands. Read alongside cannibalization: share that comes from replacing a brand sale is worth far less than share that adds to the basket.

| | |
|---|---|
| **Formula** | `SUM(CASE WHEN is_private_label THEN net_revenue ELSE 0 END)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(net_revenue), 0)` |
| **Source** | `agg_store_sku_day` |
| **Grain** | `store`, `category`, `week` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | 0.0 to 1.0 |
| **Owner** | analytics |

---

## Customer

*Problem P7 - whether deals build habit or train discount-seekers.*

### `contribution_per_customer` — Contribution per Customer (90d)

Gross margin less delivery cost, per active customer. The number that reveals whether discount-driven GMV is profitable at all. Subsidy is not subtracted: gross_margin is computed from the realized (post-discount) price, so the discount is already inside it and taking it off again double-charged it - 11.7% of total contribution, concentrated on exactly the discount-heavy customers this metric compares.

| | |
|---|---|
| **Formula** | `SUM(gross_margin_90d - delivery_cost_90d)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(COUNT(DISTINCT customer_id), 0)` |
| **Source** | `mart_customer_360` |
| **Grain** | `store`, `customer_segment`, `ddi_band` |
| **Format** | `inr` |
| **Direction** | higher is better |
| **Expected range** | -2000 to 20000 |
| **Owner** | analytics |

### `discount_dependency_index` — Discount Dependency Index

Share of a customer's trailing-90-day orders containing a promotional item. High-DDI cohorts show strong GMV and weak contribution - this is the metric that separates habit from subsidy addiction.

| | |
|---|---|
| **Formula** | `SUM(promo_orders_90d)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(SUM(orders_90d), 0)` |
| **Source** | `mart_customer_360` |
| **Grain** | `customer_segment`, `cohort_month` |
| **Format** | `percent_1dp` |
| **Direction** | lower is better |
| **Expected range** | 0.0 to 1.0 |
| **Owner** | analytics |

### `repeat_rate` — Repeat Rate

Share of active customers with more than one order in the period. The denominator counts active customers, matching that sentence: dividing by the whole base instead would blend repeat behaviour with the lapse rate and move whenever acquisition moved, which is a different question.

| | |
|---|---|
| **Formula** | `COUNT(DISTINCT CASE WHEN orders_90d > 1 THEN customer_id END)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(COUNT(DISTINCT CASE WHEN orders_90d > 0 THEN customer_id END), 0)` |
| **Source** | `mart_customer_360` |
| **Grain** | `store`, `customer_segment` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | 0.0 to 1.0 |
| **Owner** | analytics |

### `retention_90d` — 90-Day Retention · **guardrail**

Share of customers active in the trailing 90 days who were also active in the prior 90. The guardrail on GM-AWM: margin bought by starving stores of stock shows up here as churn.

| | |
|---|---|
| **Formula** | `COUNT(DISTINCT CASE WHEN active_curr_90d AND active_prev_90d THEN customer_id END)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(COUNT(DISTINCT CASE WHEN active_prev_90d THEN customer_id END), 0)` |
| **Source** | `mart_customer_360` |
| **Grain** | `store`, `cohort_month` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | 0.0 to 1.0 |
| **Owner** | analytics |

### `retention_m1` — M1 Cohort Retention

Share of a signup cohort that ordered again in their first full month. Denominator counts only cohorts whose M1 falls inside the order feed: signups start 18 months before orders do, and without the guard those cohorts report a clean 0.0 that passes every range test while describing the dataset rather than the customers.

| | |
|---|---|
| **Formula** | `COUNT(DISTINCT CASE WHEN ordered_m1 THEN customer_id END)`<br>&nbsp;&nbsp;÷&nbsp;`NULLIF(COUNT(DISTINCT CASE WHEN m1_observable THEN customer_id END), 0)` |
| **Source** | `mart_customer_360` |
| **Grain** | `cohort_month` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | 0.0 to 1.0 |
| **Owner** | analytics |

---

## Data Quality

*Problem P8 - one bad number kills adoption of everything above.*

### `dq_test_pass_rate` — Data Quality Test Pass Rate

Share of dbt and Soda checks passing on the latest run.

| | |
|---|---|
| **Formula** | `SUM(CASE WHEN status = 'pass' THEN 1 ELSE 0 END)`<br>&nbsp;&nbsp;÷&nbsp;`COUNT(*)` |
| **Source** | `dq_test_results` |
| **Grain** | `date_day`, `test_severity` |
| **Format** | `percent_1dp` |
| **Direction** | higher is better |
| **Expected range** | 0.0 to 1.0 |
| **Owner** | analytics |

### `freshness_sla_breaches` — Freshness SLA Breaches

Count of sources whose latest event is older than their declared SLA.

| | |
|---|---|
| **Formula** | `SUM(CASE WHEN check_type = 'freshness' AND status = 'fail' THEN 1 ELSE 0 END)` |
| **Source** | `dq_test_results` |
| **Grain** | `date_day`, `source_name` |
| **Format** | `number_0dp` |
| **Direction** | lower is better |
| **Expected range** | — |
| **Owner** | analytics |

---

## Dimensions

| Dimension | Label | Key | Resolves via |
|---|---|---|---|
| `abc_class` | ABC Class | `sku_id` | `dim_product` |
| `brand` | Brand | `sku_id` | `dim_product` |
| `category` | Category | `sku_id` | `dim_product` |
| `cohort_month` | Signup Cohort | `customer_id` | `mart_customer_360` |
| `customer_segment` | Customer Segment | `customer_id` | `mart_customer_360` |
| `date_day` | Date | `date_day` | already on the mart |
| `day_of_week` | Day of Week | `date_day` | `dim_date` |
| `ddi_band` | Discount Dependency Band | `customer_id` | `mart_customer_360` |
| `dte_band` | Days to Expiry Band | `dte_band` | already on the mart |
| `funding_source` | Funding Source | `promo_id` | `dim_promotion` |
| `horizon_days` | Forecast Horizon | `horizon_days` | already on the mart |
| `is_festival` | Festival Day | `date_day` | `dim_date` |
| `is_monsoon` | Monsoon | `date_day` | `dim_date` |
| `is_private_label` | Private Label | `sku_id` | `dim_product` |
| `is_salary_week` | Salary Week | `date_day` | `dim_date` |
| `locality` | Locality | `store_id` | `dim_store` |
| `month` | Month | `date_day` | `dim_date` |
| `policy_arm` | Policy Arm | `policy_arm` | already on the mart |
| `promo_type` | Promotion Type | `promo_id` | `dim_promotion` |
| `rfm_segment` | RFM Segment | `customer_id` | `mart_customer_360` |
| `sku` | SKU | `sku_id` | `dim_product` |
| `source_name` | Source | `source_name` | already on the mart |
| `store` | Store | `store_id` | `dim_store` |
| `store_tier` | Catchment Tier | `store_id` | `dim_store` |
| `subcategory` | Subcategory | `sku_id` | `dim_product` |
| `supplier` | Supplier | `supplier_id` | `dim_supplier` |
| `temp_zone` | Temperature Zone | `sku_id` | `dim_product` |
| `test_severity` | Test Severity | `test_severity` | already on the mart |
| `week` | Week | `date_day` | `dim_date` |
| `xyz_class` | XYZ Class | `sku_id` | `dim_product` |
