# Known Data Issues

> **Generated file — do not edit.** Produced by `simulator/dirt.py`.

The raw layer is deliberately imperfect. Real source feeds arrive with
duplicates, late events, unit drift and outages, and a project whose
Bronze layer is pristine has skipped the part of the job that takes the
time. Each defect below is injected on purpose, is reproducible from the
run seed, and needs a different fix in staging.

This is the document a data team actually keeps: what is wrong with the
feeds, how you notice, and what the pipeline does about it.

| # | Defect | Feeds affected |
|---|---|---|
| 1 | Duplicate order events (retried webhook) | `pos_orders`, `pos_order_items` |
| 2 | Late-arriving orders (up to 48h) | `pos_orders` |
| 3 | Inventory movements with no batch reference | `wms_inventory_movement` |
| 4 | Pack size in kg while uom says g (14 SKUs) | `catalog_snapshot` |
| 5 | Returns encoded two different ways | `pos_order_items`, `pos_returns` |
| 6 | Clickstream in UTC, POS in IST | `clickstream` |
| 7 | SKU identifier format changes on 2026-03-01 | `clickstream` |
| 8 | Clickstream outage (2026-03-01, 2026-03-02) | `clickstream` |

---

## 1. Duplicate order events (retried webhook)

- **Feeds** — `pos_orders`, `pos_order_items`
- **Rows affected** — 22,870

**Symptom.** Exact duplicate rows. Revenue and units overstated by ~0.4%.

**Fix.** Deduplicate on the full row hash in staging. Do not dedupe on order_id alone - a genuine order has many item lines.

## 2. Late-arriving orders (up to 48h)

- **Feeds** — `pos_orders`
- **Rows affected** — 23,253

**Symptom.** An order's timestamp is up to two days before the partition it arrived in. A daily incremental keyed on the partition silently drops them.

**Fix.** Incremental models must key on the event timestamp and reprocess a 48-hour lookback window, not just the newest partition.

## 3. Inventory movements with no batch reference

- **Feeds** — `wms_inventory_movement`
- **Rows affected** — 48,845

**Symptom.** batch_id is null, so the movement cannot be attributed to an expiry date and the stock reconciliation will not balance.

**Fix.** Route to a quarantine table with the reason recorded. Never drop silently - the reconciliation test is what surfaces the gap.

## 4. Pack size in kg while uom says g (14 SKUs)

- **Feeds** — `catalog_snapshot`
- **Rows affected** — 5,110

**Symptom.** A 500 g pack reports pack_qty 0.5. Any per-kilo price or weight rollup is wrong by three orders of magnitude for those SKUs.

**Fix.** Range-check pack_qty by uom in staging and rescale. A dbt-expectations bound on pack_qty per uom catches it.

## 5. Returns encoded two different ways

- **Feeds** — `pos_order_items`, `pos_returns`
- **Rows affected** — 10,470

**Symptom.** Some returns are negative quantities inside the sales feed; others are positive rows in a separate feed. Counting either alone gets net units wrong, and counting both naively double-counts.

**Fix.** Normalise both into one signed movement in staging, and assert that gross sales minus returns reconciles to the inventory ledger.

## 6. Clickstream in UTC, POS in IST

- **Feeds** — `clickstream`
- **Rows affected** — 3,082,041

**Symptom.** Clickstream timestamps are 5h30m behind the orders they relate to. Joining on date misattributes every event before 05:30 IST to the previous day, and the evening demand peak lands in the afternoon.

**Fix.** Conform everything to IST in staging and say so in the column name. A test that the hourly demand curve peaks in the evening catches this immediately.

## 7. SKU identifier format changes on 2026-03-01

- **Feeds** — `clickstream`
- **Rows affected** — 1,677,678

**Symptom.** SKU-00042 becomes SKU_42 partway through the year, so an inner join to the product dimension silently loses every clickstream event after that date.

**Fix.** Normalise the identifier in staging and assert referential integrity against dim_product. A relationships test fails loudly where a silent inner join would not.

## 8. Clickstream outage (2026-03-01, 2026-03-02)

- **Feeds** — `clickstream`
- **Rows affected** — 25,603

**Symptom.** Two partitions are missing entirely, and because collectors fail under load they are two of the busiest days of the year. Any metric averaging over that window is biased downward, and the censored-demand signal is absent exactly where stockouts were worst.

**Fix.** A freshness and row-count check per source per day. Missing partitions must fail a check, not average to zero.
