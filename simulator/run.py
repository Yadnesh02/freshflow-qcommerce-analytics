"""Run the whole simulated year and emit the Bronze layer (task S1.7).

Every component so far has been exercised in isolation. This is where they
meet: demand responds to price and freshness, baskets are assembled from that
demand, order lines consume real batches first-expiring-first, stockouts and
near-expiry deliveries are logged against the customers who experienced them,
and the policy decides what to reorder for tomorrow.

    python tasks.py simulate --days 365 --seed 42

Order of operations within a day matters and is deliberate:

    receive -> expire -> price -> demand -> baskets -> fulfil -> reorder

Pricing is decided before demand is drawn, because a markdown is a decision the
store makes in the morning and customers respond to during the day. Reordering
happens last, on what is left after trading. Getting this sequence wrong would
let the store react to sales it has not made yet.

Output is Hive-partitioned parquet under data/raw/<source>/dt=YYYY-MM-DD/,
which is what a real extract into a lake looks like.
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from simulator.catalog import build_catalog, to_bronze
from simulator.config_loader import SimConfig, load_sim_config
from simulator.customers import BasketAssembler, CustomerBase
from simulator.demand import DemandModel, freshness_multiplier, price_multiplier
from simulator.fefo import (
    LOW_DTE_FRACTION,
    Fulfiller,
    InventoryLedger,
    to_movement_frame,
)
from simulator.policies.base import PolicyContext
from simulator.policies.baseline import BaselinePolicy
from simulator.supply import SupplyChain

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"

DEAL_PRICE = 11.0
# Clickstream is sampled: every out-of-stock view is kept because that is the
# censored-demand signal the forecast depends on, but successful views are
# sampled down. Real pipelines do exactly this, and keeping all of them would
# triple the dataset for no analytical gain.
INSTOCK_VIEW_SAMPLE = 0.20
NOTIFY_ME_RATE = 0.28
# a few SKUs are repriced through the year, so the SCD2 snapshot has something
# to capture and historical margin has to use historical cost
REPRICE_MONTHS = (12, 3, 6)
REPRICE_SHARE = 0.08
# Assortment. A dark store does not carry a three-day product it sells twice a
# week - it would write off most of every delivery, and the supplier minimum
# order alone would be a fortnight of cover. Real networks range perishables by
# store velocity, so the simulation does too. This is a merchandising decision,
# deliberately kept out of the policy: it is not one of the baseline's
# weaknesses, it is the ranging any competent operator already does.
PERISHABLE_DAYS = 14
MIN_PERISHABLE_VELOCITY = 0.5  # expected units per store per day


@dataclass
class DayCounters:
    units_demanded: int = 0
    units_sold: int = 0
    units_lost: int = 0
    units_substituted: int = 0
    units_expired: int = 0
    writeoff_value: float = 0.0
    revenue: float = 0.0
    cogs: float = 0.0
    orders: int = 0
    stockout_cells: int = 0


@dataclass
class SimulationRun:
    """One end-to-end pass of the simulated network."""

    cfg: SimConfig
    seed: int = 42
    days: int | None = None
    out_dir: Path = RAW_DIR
    policy_name: str = "baseline"
    quiet: bool = False

    summary: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng([self.seed, 1])
        self.catalog = build_catalog(self.cfg, seed=self.seed)
        self.demand = DemandModel(self.cfg, self.catalog, seed=self.seed)
        self.customers = CustomerBase(self.cfg, seed=self.seed)
        self.baskets = BasketAssembler(self.cfg, self.catalog, self.customers)
        self.supply = SupplyChain(self.cfg, self.catalog, seed=self.seed)
        self.fulfiller = Fulfiller(self.cfg, self.catalog)
        self.policy = BaselinePolicy(self.cfg, self.catalog)

        self.S, self.K = len(self.cfg.stores), len(self.catalog)
        self.ledger = InventoryLedger(self.S, self.K)

        self.store_ids = np.array([s["store_id"] for s in self.cfg.stores])
        self.sku_ids = self.catalog["sku_id"].to_numpy()
        self.base_price = self.catalog["base_price"].to_numpy().astype(float)
        self.landed_cost = self.catalog["landed_cost"].to_numpy().astype(float)
        self.shelf_life = self.catalog["shelf_life_days"].to_numpy()
        self.elasticity = self.catalog["elasticity"].to_numpy()
        self.open_ord = np.array([s["opened_date"].toordinal() for s in self.cfg.stores])

        # rolling state the policy reads
        window = self.cfg.policies["baseline"]["replenishment"]["trailing_window_days"]
        self._window = window
        self._sales_history = np.zeros((window, self.S, self.K))
        # which cells were actually available to sell on each of those days
        self._instock_history = np.ones((window, self.S, self.K), dtype=bool)
        self._history_day = 0
        self.on_order = np.zeros((self.S, self.K), dtype=np.int64)

        self._store_pos = {s: i for i, s in enumerate(self.store_ids)}
        self._pending: dict[dt.date, list[pd.DataFrame]] = defaultdict(list)
        self._batch_ids: list[str] = []
        self._counts: dict[str, int] = defaultdict(int)

    def _trailing_demand(self) -> np.ndarray:
        """Mean daily sales over the days the SKU was actually available.

        Averaging over stockout days too would make every stockout lower the
        demand estimate, which lowers the order, which causes more stockouts.
        Excluding them is the crude lost-sales correction most replenishment
        systems apply. It is not proper censored-demand imputation - that stays
        the optimised policy's advantage in Sprint 3.
        """
        available = self._instock_history.sum(axis=0)
        return self._sales_history.sum(axis=0) / np.maximum(available, 1)

    def _store_index_of(self, store_ids: np.ndarray) -> np.ndarray:
        return np.array([self._store_pos[s] for s in store_ids], dtype=int)

    def _apply_assortment(self) -> None:
        """Range perishables by store velocity, preserving total network volume."""
        expected = self.demand.base
        perishable = self.shelf_life <= PERISHABLE_DAYS
        drop = perishable[None, :] & (expected < MIN_PERISHABLE_VELOCITY)

        before = expected.sum()
        self.demand.base[drop] = 0.0
        self.demand.base *= before / self.demand.base.sum()
        self.assortment = ~drop

        n_perishable_cells = int((perishable[None, :] & np.ones_like(drop)).sum())
        self._log(
            f"assortment: dropped {int(drop.sum()):,} slow perishable cells "
            f"({drop.sum() / n_perishable_cells:.0%} of perishable store-SKU pairs)"
        )

    def _seed_opening_stock(self, day: dt.date) -> None:
        """Fill the shelves before day one.

        Starting from an empty warehouse would spend the first fortnight in
        permanent stockout, then bury the model in cold-start orders that all
        expire together. Real networks open the analysis window mid-life, so
        the simulation does too: every trading store starts with roughly the
        cover its own policy would hold, on batches whose remaining shelf life
        is already spread out.
        """
        rep = self.cfg.policies["baseline"]["replenishment"]
        cover = rep["assumed_lead_time_days"] + rep["review_period_days"] + rep["safety_days"]
        rng = np.random.default_rng([self.seed, 909])
        ord_ = day.toordinal()

        expected = self.demand.base  # calibrated units per average day
        qty = np.round(expected * cover).astype(int)
        qty[self.open_ord > ord_] = 0  # a store that has not opened has no stock
        qty[~self.assortment] = 0

        si, ki = np.nonzero(qty > 0)
        # spread remaining life so the opening stock does not all expire at once
        fraction = rng.uniform(0.30, 0.95, len(si))
        usable = np.maximum(1, np.floor(self.shelf_life[ki] * fraction)).astype(int)

        self.ledger.receive(
            si,
            ki,
            qty[si, ki],
            ord_ + usable,
            self.shelf_life[ki],
            self.landed_cost[ki],
            ord_,
        )
        self._batch_ids.extend(f"BAT-OPEN-{i:08d}" for i in range(len(si)))
        # and give the policy a plausible sales history, so it does not treat
        # every SKU in the catalogue as brand new
        self._sales_history[:] = expected[None, :, :]
        self.ledger.drain_movements()
        self._log(f"opening stock: {qty.sum():,} units across {len(si):,} store-SKU cells")

    # ------------------------------------------------------------------ output
    def _write(self, source: str, day: dt.date, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        path = self.out_dir / source / f"dt={day.isoformat()}"
        path.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path / "part-0.parquet", index=False)
        self._counts[source] += len(frame)

    def _log(self, msg: str) -> None:
        if not self.quiet:
            print(msg, flush=True)

    # ------------------------------------------------------------------ pricing
    def _apply_repricing(self, day: dt.date) -> bool:
        """Occasional cost and price revisions, so SCD2 has something to track."""
        if not (day.day == 1 and day.month in REPRICE_MONTHS):
            return False
        rng = np.random.default_rng([self.seed, day.toordinal()])
        picked = rng.random(self.K) < REPRICE_SHARE
        factor = rng.uniform(0.94, 1.10, self.K)
        self.base_price = np.where(picked, np.round(self.base_price * factor, 0), self.base_price)
        self.landed_cost = np.where(
            picked, np.round(self.landed_cost * factor, 2), self.landed_cost
        )
        return True

    def _day_prices(self, discount: np.ndarray, deals: dict[int, list[int]]) -> np.ndarray:
        """Realised shelf price per store x SKU after markdown and the deal rail."""
        price = self.base_price[None, :] * (1.0 - discount)
        for si, skus in deals.items():
            price[si, skus] = DEAL_PRICE
        return price

    # ------------------------------------------------------------------ one day
    def _step(self, day: dt.date, rng: np.random.Generator) -> DayCounters:
        c = DayCounters()
        ord_ = day.toordinal()
        store_open = self.open_ord <= ord_

        # --- 1. goods inwards -------------------------------------------------
        arriving = self._pending.pop(day, [])
        if arriving:
            pos = pd.concat(arriving, ignore_index=True)

            # release the whole ordered quantity from the on-order position,
            # including short shipments - the order is closed either way, and
            # holding it open would stop the store ever reordering the shortfall
            po_store = self._store_index_of(pos["store_id"].to_numpy())
            np.subtract.at(
                self.on_order,
                (po_store, pos["_sku_idx"].to_numpy()),
                pos["ordered_qty"].to_numpy(),
            )
            self.on_order = np.maximum(self.on_order, 0)

            batches = self.supply.to_batches(pos)
            if not batches.empty:
                store_idx = self._store_index_of(batches["store_id"].to_numpy())
                sku_idx = batches["_sku_idx"].to_numpy()
                self.ledger.receive(
                    store_idx,
                    sku_idx,
                    batches["qty_received"].to_numpy(),
                    np.array([d.toordinal() for d in batches["expiry_date"]]),
                    self.shelf_life[sku_idx],
                    batches["unit_landed_cost"].to_numpy(),
                    ord_,
                )
                self._batch_ids.extend(batches["batch_id"].tolist())
                self._write("wms_inventory_batch", day, batches.drop(columns=["_sku_idx"]))
            self._write("wms_purchase_orders", day, pos.drop(columns=["_sku_idx", "_sup_idx"]))

        # --- 2. write off anything past its date ------------------------------
        for batch_row, units in self.ledger.expire(ord_):
            c.units_expired += units
            c.writeoff_value += units * self.ledger.batch_cost(batch_row)

        # --- 3. today's prices, decided before customers see them -------------
        min_dte = self.ledger.min_dte_matrix(ord_)
        ctx = PolicyContext(
            date=day,
            on_hand=self.ledger.on_hand_matrix.copy(),
            on_order=self.on_order,
            trailing_avg=self._trailing_demand(),
            min_dte=min_dte,
            store_open=store_open,
            catalog=self.catalog,
            rng=rng,
        )
        discount = self.policy.markdown(ctx)
        deals = self.policy.deal_slots(ctx)
        price = self._day_prices(discount, deals)

        # --- 4. demand responds to price and to freshness ---------------------
        lam = self.demand.daily_lambda(day)
        ratio = np.clip(price / self.base_price[None, :], 0.05, 3.0)
        lam = lam * price_multiplier(ratio, self.elasticity[None, :])
        # freshness aversion keys on the stock-weighted average, not the
        # soonest-expiring unit - see InventoryLedger.weighted_dte_matrix
        avg_dte = self.ledger.weighted_dte_matrix(ord_)
        remaining = np.clip(avg_dte / np.maximum(self.shelf_life, 1)[None, :], 0.0, 1.0)
        lam = lam * np.where(avg_dte < 9999, freshness_multiplier(remaining, self.cfg), 1.0)

        units = self.demand.sample(lam, rng)
        c.units_demanded = int(units.sum())

        # start today's sales slice clean, or a store that sold nothing would
        # keep reporting whatever it sold a week ago
        slot = self._history_day % self._window
        self._sales_history[slot] = 0
        self._instock_history[slot] = True

        # --- 5. baskets, then fulfilment against real batches -----------------
        by_store = self.customers.active_by_store(day)
        is_monsoon = self.demand.factors[day].is_monsoon
        order_frames, item_frames, stockout_rows, click_rows = [], [], [], []

        for si in range(self.S):
            if not store_open[si]:
                continue
            orders, items = self.baskets.assemble(
                si, units[si], day, by_store[si], rng, is_monsoon=is_monsoon
            )
            if orders.empty:
                continue

            hour_of = dict(zip(orders["order_id"], orders["order_ts"], strict=True))
            cust_of = dict(zip(orders["order_id"], orders["customer_row"], strict=True))
            lines, first_out = [], {}

            for oid, sku_idx, qty in zip(
                items["order_id"], items["sku_idx"], items["qty"], strict=True
            ):
                allocs, lost, hit_stockout = self.fulfiller.fulfil_line(
                    self.ledger, si, int(sku_idx), int(qty), ord_, rng
                )
                cust = cust_of[oid]
                if hit_stockout:
                    self.customers.record("stockout", np.array([cust]))
                    key = (si, int(sku_idx))
                    first_out.setdefault(key, hour_of[oid].hour)
                c.units_lost += lost

                for sold_sku, a in allocs:
                    if sold_sku != sku_idx:
                        c.units_substituted += a.qty
                    unit_price = float(price[si, sold_sku])
                    cost = self.ledger.batch_cost(a.batch_row)
                    lines.append(
                        (
                            oid,
                            self.sku_ids[sold_sku],
                            self._batch_ids[a.batch_row],
                            a.qty,
                            float(self.base_price[sold_sku]),
                            unit_price,
                            round(float(self.base_price[sold_sku]) - unit_price, 2),
                            round(cost, 2),
                            a.dte_at_sale,
                            _promo_id(discount[si, sold_sku], sold_sku in deals.get(si, [])),
                            sold_sku != sku_idx,
                        )
                    )
                    c.units_sold += a.qty
                    c.revenue += a.qty * unit_price
                    c.cogs += a.qty * cost
                    if a.shelf_life_fraction < LOW_DTE_FRACTION:
                        self.customers.record("low_dte", np.array([cust]))
                    if _promo_id(discount[si, sold_sku], sold_sku in deals.get(si, [])):
                        self.customers.record("deal", np.array([cust]))

            if not lines:
                continue
            item_df = pd.DataFrame(
                lines,
                columns=[
                    "order_id",
                    "sku_id",
                    "batch_id",
                    "qty",
                    "unit_base_price",
                    "unit_realized_price",
                    "discount_amt",
                    "unit_cogs",
                    "dte_at_sale",
                    "promo_id",
                    "is_substitution",
                ],
            )
            sold_per_sku = (
                item_df.assign(_i=item_df["sku_id"].map({s: i for i, s in enumerate(self.sku_ids)}))
                .groupby("_i")["qty"]
                .sum()
            )
            self._sales_history[self._history_day % self._window, si, sold_per_sku.index] = (
                sold_per_sku.to_numpy()
            )

            late_rows = orders.loc[orders["is_late"], "customer_row"].to_numpy()
            if late_rows.size:
                self.customers.record("late", late_rows)
            self.customers.record("order", orders["customer_row"].to_numpy())
            c.orders += len(orders)

            out = orders.assign(
                store_id=self.store_ids[si],
                customer_id=self.customers.df["customer_id"].to_numpy()[orders["customer_row"]],
            ).drop(columns=["customer_row", "store_idx"])
            order_frames.append(out)
            item_frames.append(item_df)

            for (_, sku), hour in first_out.items():
                stockout_rows.append((self.store_ids[si], self.sku_ids[sku], day, hour))
                self._instock_history[slot, si, sku] = False
            c.stockout_cells += len(first_out)

            click_rows.append(self._clickstream(si, day, item_df, first_out, orders, rng))

        # --- 6. emit the day -------------------------------------------------
        self._write("pos_orders", day, _concat(order_frames))
        self._write("pos_order_items", day, _concat(item_frames))
        self._write(
            "wms_stockout_interval",
            day,
            pd.DataFrame(stockout_rows, columns=["store_id", "sku_id", "event_date", "hour_out"]),
        )
        self._write("clickstream", day, _concat(click_rows))
        self._write(
            "wms_inventory_movement",
            day,
            to_movement_frame(self.ledger.drain_movements(), self._batch_ids),
        )
        self._write("price_history", day, self._price_frame(day, discount, deals, price))
        self._write("catalog_snapshot", day, self._catalog_frame(day))

        # --- 7. reorder for tomorrow, on what is left -------------------------
        ctx_after = PolicyContext(
            date=day,
            on_hand=self.ledger.on_hand_matrix.copy(),
            on_order=self.on_order,
            trailing_avg=self._trailing_demand(),
            min_dte=self.ledger.min_dte_matrix(ord_),
            store_open=store_open,
            catalog=self.catalog,
            rng=rng,
        )
        req = self.policy.replenish(ctx_after)
        if len(req):
            keep = self.assortment[req.store_idx, req.sku_idx]
            req = type(req)(req.store_idx[keep], req.sku_idx[keep], req.qty[keep])
        if len(req):
            pos = self.supply.place_orders(req.store_idx, req.sku_idx, req.qty, day, rng)
            np.add.at(self.on_order, (req.store_idx, req.sku_idx), req.qty)
            for arrival, group in pos.groupby("received_date"):
                self._pending[arrival].append(group)

        self._history_day += 1
        return c

    # ------------------------------------------------------------------ feeds
    def _clickstream(self, si, day, items, first_out, orders, rng) -> pd.DataFrame:
        """Browse events. Every out-of-stock view is kept; successful views are sampled."""
        rows = []
        oos_hour = {sku: hour for (_, sku), hour in first_out.items()}
        for sku, hour in oos_hour.items():
            shoppers = max(1, int(rng.poisson(3)))
            for _ in range(shoppers):
                rows.append((self.store_ids[si], self.sku_ids[sku], day, hour, "pdp_view", False))
                if rng.random() < NOTIFY_ME_RATE:
                    rows.append(
                        (self.store_ids[si], self.sku_ids[sku], day, hour, "notify_me", False)
                    )

        keep = rng.random(len(items)) < INSTOCK_VIEW_SAMPLE
        hours = orders.set_index("order_id")["order_ts"]
        for sku_id, oid in zip(items.loc[keep, "sku_id"], items.loc[keep, "order_id"], strict=True):
            rows.append((self.store_ids[si], sku_id, day, hours[oid].hour, "pdp_view", True))

        return pd.DataFrame(
            rows, columns=["store_id", "sku_id", "event_date", "hour", "event_type", "was_in_stock"]
        )

    def _price_frame(self, day, discount, deals, price) -> pd.DataFrame:
        """Sparse: only cells whose price differs from the shelf price."""
        si, ki = np.nonzero(discount > 0)
        rows = {
            "store_id": self.store_ids[si],
            "sku_id": self.sku_ids[ki],
            "effective_date": day,
            "base_price": self.base_price[ki],
            "realized_price": price[si, ki],
            "promo_id": [_promo_id(d, False) for d in discount[si, ki]],
        }
        frame = pd.DataFrame(rows)
        deal_rows = [
            (
                self.store_ids[s],
                self.sku_ids[k],
                day,
                float(self.base_price[k]),
                DEAL_PRICE,
                "PROMO-DEAL11",
            )
            for s, skus in deals.items()
            for k in skus
        ]
        if deal_rows:
            frame = pd.concat(
                [frame, pd.DataFrame(deal_rows, columns=list(rows))], ignore_index=True
            )
        return frame

    def _catalog_frame(self, day: dt.date) -> pd.DataFrame:
        return to_bronze(self.catalog).assign(
            base_price=self.base_price, landed_cost=self.landed_cost, snapshot_date=day
        )

    # ------------------------------------------------------------------ driver
    def run(self) -> pd.DataFrame:
        dates = sorted(self.demand.factors)
        if self.days:
            dates = dates[: self.days]

        self._write_reference()
        self._apply_assortment()
        self._seed_opening_stock(dates[0])
        started = time.time()
        self._log(
            f"simulating {len(dates)} days, {self.S} stores, {self.K:,} SKUs, "
            f"policy '{self.policy.name}', seed {self.seed}"
        )

        month = dates[0].month
        for i, day in enumerate(dates):
            if self._apply_repricing(day):
                self._log(f"  {day}  repriced a slice of the catalogue")

            counters = self._step(day, self.rng)
            self.summary.append({"date": day, **counters.__dict__})

            if day.month != month:
                self.customers.step_month(day, self.rng)
                self._write("customer_snapshot", day, self.customers.to_bronze())
                month = day.month

            if not self.quiet and (i + 1) % 30 == 0:
                done = (i + 1) / len(dates)
                elapsed = time.time() - started
                self._log(
                    f"  {day}  {done:5.0%}  "
                    f"sold {counters.units_sold:>6,}  "
                    f"lost {counters.units_lost:>5,}  "
                    f"expired {counters.units_expired:>5,}  "
                    f"[{elapsed:.0f}s, eta {elapsed / done - elapsed:.0f}s]"
                )

        self._write("customer_snapshot", dates[-1], self.customers.to_bronze())
        self._log(f"\ndone in {time.time() - started:.0f}s")
        return pd.DataFrame(self.summary)

    def _write_reference(self) -> None:
        day = sorted(self.demand.factors)[0]
        self._write("ref_stores", day, pd.DataFrame(self.cfg.stores))
        self._write(
            "ref_suppliers",
            day,
            pd.DataFrame(
                [
                    {k: v for k, v in s.items() if k != "category_share"}
                    | {"categories": ", ".join(s["category_share"])}
                    for s in self.cfg.suppliers
                ]
            ),
        )

    def report(self, frame: pd.DataFrame) -> None:
        total = frame.sum(numeric_only=True)
        gross = total["revenue"] - total["cogs"]
        self._log("\n" + "=" * 62)
        self._log(f"{'units demanded':<24}{total['units_demanded']:>14,.0f}")
        self._log(f"{'units sold':<24}{total['units_sold']:>14,.0f}")
        self._log(f"{'units substituted':<24}{total['units_substituted']:>14,.0f}")
        self._log(f"{'units lost to stockout':<24}{total['units_lost']:>14,.0f}")
        self._log(f"{'units expired':<24}{total['units_expired']:>14,.0f}")
        self._log(f"{'orders':<24}{total['orders']:>14,.0f}")
        self._log("-" * 62)
        self._log(f"{'revenue':<24}{total['revenue']:>14,.0f}")
        self._log(f"{'COGS':<24}{total['cogs']:>14,.0f}")
        self._log(f"{'gross margin':<24}{gross:>14,.0f}   ({gross / total['revenue']:.1%})")
        self._log(f"{'wastage value':<24}{total['writeoff_value']:>14,.0f}")
        gm_awm = (gross - total["writeoff_value"]) / total["revenue"]
        self._log(f"{'GM after wastage':<24}{'':>14}   ({gm_awm:.1%})")
        self._log("-" * 62)
        fill = total["units_sold"] / max(total["units_demanded"], 1)
        self._log(f"{'fill rate':<24}{fill:>14.1%}")
        self._log(
            f"{'wastage rate (units)':<24}"
            f"{total['units_expired'] / max(total['units_sold'] + total['units_expired'], 1):>14.1%}"
        )
        self._log("=" * 62)
        for source, n in sorted(self._counts.items()):
            self._log(f"  {source:<26}{n:>14,} rows")


def _promo_id(discount: float, is_deal: bool) -> str | None:
    if is_deal:
        return "PROMO-DEAL11"
    if discount > 0:
        return f"PROMO-MD-{int(round(discount * 100)):02d}"
    return None


def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=None, help="limit the run (default: full window)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=RAW_DIR)
    ap.add_argument("--keep", action="store_true", help="do not clear the output directory first")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.out.exists() and not args.keep:
        shutil.rmtree(args.out)

    run = SimulationRun(
        load_sim_config(), seed=args.seed, days=args.days, out_dir=args.out, quiet=args.quiet
    )
    frame = run.run()
    run.report(frame)
    return 0


if __name__ == "__main__":
    sys.exit(main())
