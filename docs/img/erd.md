# FreshFlow — Entity Relationship Diagram

Task **S0.4**. The warehouse model, designed before it is built.

Split into three diagrams because a single 25-entity ERD is unreadable. Column
lists are indicative of the modelling decisions that matter, not exhaustive —
the generated dbt docs are the complete reference once the models exist.

> **The keystone decision:** `fct_order_item` carries a **FEFO-allocated
> `batch_id`**. Without that one foreign key there is no way to attribute a sale
> to a specific expiry date, and the entire expiry-risk, markdown and freshness
> analysis becomes impossible. Everything else in this model is ordinary retail
> dimensional design; that column is what makes the project work.

---

## 1. Core transactional star

Orders, what was in them, and who placed them.

```mermaid
erDiagram
    dim_store     ||--o{ fct_order      : "fulfils from"
    dim_customer  ||--o{ fct_order      : "places"
    dim_date      ||--o{ fct_order      : "on"
    fct_order     ||--|{ fct_order_item : "contains"
    dim_product   ||--o{ fct_order_item : "is a"
    dim_promotion ||--o{ fct_order_item : "discounts"
    dim_customer  ||--o{ fct_clickstream : "browses"
    dim_product   ||--o{ fct_clickstream : "viewed as"

    dim_store {
        string store_id PK
        string store_name
        string locality
        string pincode
        float  lat
        float  lon
        string catchment_tier "premium|mid|mass"
        int    chilled_capacity_units
        int    ambient_capacity_units
        float  serviceable_radius_km
        date   opened_date
    }

    dim_product {
        string  sku_id PK
        string  sku_name
        string  brand
        boolean is_private_label "Nomi"
        string  l1_category
        string  l2_subcategory
        decimal mrp
        decimal landed_cost
        decimal base_price
        int     shelf_life_days
        string  temp_zone "frozen|chilled|ambient"
        string  uom
        decimal gst_rate
        string  abc_class
        string  xyz_class
        boolean deal_eligible_flag
    }

    dim_customer {
        string  customer_id PK
        date    signup_date
        string  home_store_id FK
        string  acquisition_channel
        string  device
        boolean is_member
    }

    dim_date {
        date    date_day PK
        date    week_start_date
        date    month_start_date
        string  day_name
        boolean is_weekend
        boolean is_festival
        string  festival_name
        boolean is_salary_week
        boolean is_monsoon
        boolean is_ipl_window "the season, not the fixture list - see dim_date"
    }

    dim_promotion {
        string  promo_id PK
        string  promo_type "markdown|deal_11|bogo|bundle|free_delivery"
        string  funding_source "brand|platform"
        decimal depth_pct
        timestamp start_ts
        timestamp end_ts
    }

    fct_order {
        string    order_id PK
        string    customer_id FK
        string    store_id FK
        date      date_day FK
        timestamp order_ts
        timestamp promised_ts
        timestamp delivered_ts
        decimal   gmv
        decimal   discount_total
        boolean   is_late
    }

    fct_order_item {
        string  order_id PK "FK"
        string  sku_id PK "FK"
        string  batch_id PK "FK - FEFO-allocated, the keystone"
        string  store_id FK "denormalised for slicing"
        date    date_day FK
        string  promo_id FK
        int     qty
        decimal unit_base_price
        decimal unit_realized_price
        decimal discount_amt
        decimal unit_cogs
        int     dte_at_sale "days to expiry at the moment of sale"
    }

    fct_clickstream {
        string    store_id PK "FK"
        string    sku_id PK "FK"
        timestamp hour_ts_ist PK "the feed timestamps to the hour"
        string    event_type PK "search|impression|pdp_view|add_to_cart|notify_me|checkout"
        boolean   was_in_stock PK "the uncensored-demand signal"
        date      date_day FK
        int       event_count
        int       censored_event_count
    }
```

**Why `was_in_stock` sits on the clickstream:** it is the only record of demand
that existed while stock was zero. Sales data cannot distinguish "nobody wanted
it" from "we had none", and a forecast trained on the difference under-orders
forever. See plan §2, censored demand.

---

## 2. Inventory, supply and availability

Where the perishability problem actually lives.

```mermaid
erDiagram
    dim_supplier ||--o{ fct_purchase_order    : "supplies"
    dim_store    ||--o{ fct_purchase_order    : "orders for"
    dim_product  ||--o{ fct_purchase_order    : "of"
    fct_purchase_order ||--o{ fct_inventory_batch : "receives as"
    fct_inventory_batch ||--|{ fct_inventory_movement : "moves"
    fct_inventory_batch ||--o{ fct_order_item  : "fulfils"
    dim_store    ||--o{ fct_availability_hour : "measured at"
    dim_product  ||--o{ fct_availability_hour : "for"
    dim_store    ||--o{ fct_price_history     : "prices"
    dim_product  ||--o{ fct_price_history     : "of"

    dim_supplier {
        string  supplier_id PK
        string  supplier_name
        float   lead_time_mean_days
        float   lead_time_sd_days
        float   otif_rate
        float   inbound_freshness_pct_mean "share of shelf life remaining on arrival"
    }

    fct_purchase_order {
        string    po_id PK
        string    sku_id PK "FK"
        string    store_id FK
        string    supplier_id FK
        int       ordered_qty
        timestamp ordered_ts
        timestamp expected_ts
        timestamp received_ts
        int       received_qty
        float     inbound_shelf_life_pct
    }

    fct_inventory_batch {
        string    batch_id PK
        string    sku_id FK
        string    store_id FK
        string    supplier_id FK
        string    po_id FK
        date      mfg_date
        date      expiry_date
        timestamp received_ts
        int       qty_received
        decimal   unit_landed_cost
    }

    fct_inventory_movement {
        bigint    movement_seq PK "monotonic - an event log needs a replay order"
        string    batch_id FK
        string    event_type "inbound|sale|transfer_in|transfer_out|damage|expiry_writeoff|cycle_count_adj"
        int       qty_delta
        date      event_date FK "the ledger is daily-grained; movement_seq is the intra-day order"
    }

    fct_availability_hour {
        string    store_id PK "FK"
        string    sku_id PK "FK"
        timestamp hour_ts PK
        date      date_day FK
        int       on_hand_units
        boolean   is_in_stock
        boolean   in_assortment
    }

    fct_price_history {
        string    store_id PK "FK"
        string    sku_id PK "FK"
        timestamp effective_from PK
        timestamp effective_to
        decimal   base_price
        decimal   realized_price
        string    promo_id FK
    }

    dim_product_snapshot {
        string    sku_id PK
        timestamp dbt_valid_from PK
        timestamp dbt_valid_to
        decimal   landed_cost
        decimal   base_price
    }
```

**Three modelling notes worth defending in an interview:**

1. **Batch grain, not SKU grain.** `fct_inventory_batch` is one row per physical
   receipt with its own expiry date. Two deliveries of the same SKU on different
   days are different batches with different risk. Modelling inventory at SKU
   level would average away the entire problem.
2. **Movements, not snapshots.** On-hand is derived by a running-sum window over
   `fct_inventory_movement`, so every balance is auditable back to an event. A
   reconciliation test asserts the derived balance equals the ledger exactly.
3. **`dim_product_snapshot` is a dbt snapshot (SCD Type 2).** Landed cost and
   base price change mid-year; margin on a March order must use March's cost.
   Joining live `dim_product` would silently restate history.

---

## 3. Marts, decision outputs and serving

Everything the metric registry reads from.

```mermaid
erDiagram
    agg_store_sku_day    ||--o{ mart_expiry_risk       : "feeds"
    agg_store_sku_day    ||--o{ mart_forecast_accuracy : "feeds"
    mart_expiry_risk     ||--o{ rec_markdown_action    : "triggers"
    mart_expiry_risk     ||--o{ rec_transfer_order     : "triggers"
    mart_expiry_risk     ||--o{ rec_deal_slot          : "triggers"
    mart_customer_360    ||--o{ rec_customer_offer     : "targets"
    agg_store_sku_day    ||--o{ rec_purchase_order     : "drives"
    rec_markdown_action  ||--o{ mart_markdown_perf     : "evaluated by"
    rec_deal_slot        ||--o{ mart_deal_slot_perf    : "evaluated by"

    agg_store_sku_day {
        date    date_day PK
        string  store_id PK "FK"
        string  sku_id PK "FK"
        boolean is_private_label "denormalised from dim_product"
        int     units_sold
        int     units_demanded_imputed "censored-demand corrected"
        boolean is_censored
        decimal net_revenue
        decimal cogs
        int     opening_units
        int     received_units
        int     closing_units
        decimal received_value
        decimal writeoff_value
        int     writeoff_units
        float   trailing_7d_avg_units
        decimal base_price_avg
        decimal realized_price_avg
        int     markdown_units
        decimal markdown_subsidy_platform
        decimal markdown_subsidy_brand
    }

    mart_order_daily {
        date    date_day PK
        string  store_id PK "FK"
        int     orders_count
        decimal net_revenue
        decimal delivery_fee
        int     late_orders
    }

    mart_expiry_risk {
        string  batch_id PK
        date    date_day PK
        string  store_id FK
        string  sku_id FK
        int     on_hand_units
        int     days_to_expiry
        string  dte_band
        float   forecast_residual_demand
        float   expiry_risk_score "P(unsold at expiry)"
        int     units_at_risk
        decimal unit_landed_cost
        string  risk_bucket "low|watch|high|critical"
        string  recommended_action
    }

    mart_forecast_accuracy {
        date    date_day PK
        string  store_id PK "FK"
        string  sku_id PK "FK"
        int     horizon_days PK
        float   actual_units
        float   forecast_units
        float   naive_units "seasonal-naive baseline for FVA"
        boolean was_censored "excluded from evaluation"
    }

    mart_markdown_perf {
        string  store_id PK "FK"
        string  sku_id PK "FK"
        date    date_day PK
        float   p_sell_full_price
        decimal discount_value
        decimal realized_revenue
        decimal landed_cost_value
        int     units_marked_down
    }

    mart_deal_slot_perf {
        string  store_id PK "FK"
        date    date_day PK
        int     slot_rank PK
        string  sku_id FK
        decimal subsidy_value
        int     orders
        int     control_implied_orders
        decimal treated_basket_value
        decimal control_basket_value
    }

    mart_customer_360 {
        string  customer_id PK
        string  store_id FK "home store"
        date    cohort_month
        string  customer_segment
        string  rfm_segment
        string  ddi_band
        boolean active_curr_90d
        boolean active_prev_90d
        boolean ordered_m1
        boolean m1_observable "cohort's M1 falls inside the order feed"
        int     orders_90d
        int     promo_orders_90d
        decimal gross_margin_90d
        decimal delivery_cost_90d
        decimal subsidy_90d
        float   churn_score
    }

    mart_store_scorecard {
        string  store_id PK "FK"
        date    week_start_date PK
        float   gm_awm
        float   wastage_rate_value
        float   in_stock_pct
        float   forecast_wape
    }

    mart_pl_performance {
        string  sku_id PK "FK"
        date    week_start_date PK
        decimal pl_revenue
        decimal cannibalised_revenue
        decimal incremental_revenue
        decimal incremental_margin
    }

    mart_experiment_readout {
        string  metric_name PK
        string  arm PK
        int     seed PK
        float   value
        float   delta_vs_baseline
        float   ci_low
        float   ci_high
    }

    dq_test_results {
        string  test_id PK
        date    date_day PK
        string  source_name
        string  check_type "schema|freshness|distribution|reconciliation"
        string  test_severity "warn|error"
        string  status "pass|fail"
    }

    rec_markdown_action {
        string  batch_id PK
        date    date_day PK
        float   recommended_discount_pct
        int     expected_units_sold
        decimal expected_margin_recovered
        decimal value_at_risk
    }

    rec_transfer_order {
        string  from_store_id PK "FK"
        string  to_store_id PK "FK"
        string  sku_id PK "FK"
        date    date_day PK
        int     qty
        decimal avoided_writeoff_value
        decimal net_benefit
    }

    rec_deal_slot {
        string  store_id PK "FK"
        date    date_day PK
        int     slot_rank PK
        string  sku_id FK
        string  rationale
        decimal expected_subsidy
    }

    rec_customer_offer {
        string  customer_id PK "FK"
        date    date_day PK
        string  offer_type
        string  channel
        float   predicted_uplift
    }

    rec_purchase_order {
        string  store_id PK "FK"
        string  sku_id PK "FK"
        date    date_day PK
        int     order_up_to_level
        int     recommended_qty
        float   service_level_used
    }
```

---

## Table catalogue

| Table | Layer | Grain | Why it exists |
|---|---|---|---|
| `dim_store` | gold | store | 14 Mumbai dark stores |
| `dim_product` | gold | SKU | 1,500 SKUs, ~325 perishable, ~131 private label |
| `dim_product_snapshot` | gold | SKU × valid_from | SCD2 on cost and price, so historical margin is correct |
| `dim_customer` | gold | customer | ~45,000 customers |
| `dim_date` | gold | date | Festival, monsoon and salary-week flags |
| `dim_promotion` | gold | promo | Type and, critically, who funds it |
| `dim_supplier` | gold | supplier | Lead time and inbound freshness variance |
| `fct_order` | gold | order | ~1.6M orders |
| `fct_order_item` | gold | order × SKU × batch | ~4.2M lines. Batch is part of the key: FEFO can split one line across two batches, which is exactly why it is there. |
| `fct_clickstream` | gold | event | Demand signal during stockouts |
| `fct_inventory_batch` | gold | batch | ~700k batches with expiry dates |
| `fct_inventory_movement` | gold | movement | The auditable inventory ledger |
| `fct_availability_hour` | gold | store × SKU × hour | Time-weighted in-stock, stockout intervals |
| `fct_price_history` | gold | store × SKU × effective_from | Realized price including markdowns |
| `fct_purchase_order` | gold | PO line | Ordering behaviour and supplier performance |
| `agg_store_sku_day` | mart | store × SKU × day | The workhorse; most metrics resolve here |
| `mart_order_daily` | mart | store × day | Order counts and AOV |
| `mart_expiry_risk` | mart | batch × day | The hero table: value at risk per batch |
| `mart_forecast_accuracy` | mart | store × SKU × horizon × day | WAPE, bias, forecast value add |
| `mart_markdown_perf` | mart | store × SKU × day | Depth, leakage, margin recovery |
| `mart_deal_slot_perf` | mart | store × day × slot | ₹11 deal incrementality |
| `mart_customer_360` | mart | customer | RFM, cohort, DDI, contribution, churn |
| `mart_store_scorecard` | mart | store × week | Executive view |
| `mart_pl_performance` | mart | SKU × week | Private label incrementality vs cannibalisation |
| `mart_experiment_readout` | mart | metric × arm × seed | The policy A/B result |
| `dq_test_results` | mart | test × day | Data quality page |
| `rec_markdown_action` | output | batch × day | Recommended discount depth |
| `rec_transfer_order` | output | from × to × SKU × day | Recommended inter-store moves |
| `rec_deal_slot` | output | store × day × slot | Today's ₹11 assortment |
| `rec_customer_offer` | output | customer × day | Targeted offers |
| `rec_purchase_order` | output | store × SKU × day | Replenishment quantities |

Every `source:` in [`semantic/metrics.yml`](../../semantic/metrics.yml) must
appear above. [`tests/test_model_contract.py`](../../tests/test_model_contract.py)
enforces it, so the registry and the model cannot drift apart before either is built.
