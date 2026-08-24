"""Purchase orders, supplier behaviour and inbound batches (task S1.3).

This is where inventory first exists as a physical thing with an expiry date,
and where most of the wastage that stores get blamed for is actually created.

Three supply-side facts are modelled because each one produces a different
analytical finding:

**Lead-time variance, not lead time.** A reliable 3-day supplier is easier to
plan around than an erratic 1-to-4-day one, because safety stock has to cover
the spread rather than the mean. Drawn from a Gamma, which is right-skewed -
deliveries run late far more often than they run early.

**Inbound freshness.** A dairy batch arriving with 61% of its shelf life
already gone has barely half the selling window the planner assumed. Invisible
unless inventory is modelled at batch grain, which is why it is.

**Cost/quality trade-off.** The worst supplier is also the cheapest, so the
landed cost of a batch depends on who shipped it. Procurement's incentive and
the wastage bill point in opposite directions - the root cause is a real
trade-off, not an oversight.

Suppliers are sampled **per order**, not fixed per SKU. That means the same SKU
arrives from different suppliers over the year, which is what lets a later
analysis compare inbound freshness holding the product constant instead of
confounding supplier with assortment.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from simulator.config_loader import SimConfig

# a batch always gets at least one day of usable life, however bad the inbound
MIN_USABLE_DAYS = 1
# floor on inbound freshness - below this a delivery would be rejected at the door
MIN_INBOUND_FRESHNESS = 0.12
# share of OTIF failures that arrive late rather than short
LATE_SHARE_OF_FAILURES = 0.60
# how short a short delivery is
SHORT_FILL_RANGE = (0.45, 0.90)
# how late a late delivery is, in extra days
LATE_DAYS_RANGE = (1, 3)


@dataclass
class SupplyChain:
    """Turns order quantities into dated, priced, expiring inventory batches."""

    cfg: SimConfig
    catalog: pd.DataFrame
    seed: int = 42

    store_ids: list[str] = field(init=False)
    supplier_ids: list[str] = field(init=False)
    shocks: dict[dt.date, dict] = field(init=False)

    def __post_init__(self) -> None:
        self.store_ids = [s["store_id"] for s in self.cfg.stores]
        self._store_index = {s: i for i, s in enumerate(self.store_ids)}

        sup = self.cfg.suppliers
        self.supplier_ids = [s["supplier_id"] for s in sup]
        self._lead_mean = np.array([s["lead_time_mean_days"] for s in sup])
        self._lead_sd = np.array([s["lead_time_sd_days"] for s in sup])
        self._otif = np.array([s["otif_rate"] for s in sup])
        self._fresh_mean = np.array([s["inbound_freshness_pct"][0] for s in sup])
        self._fresh_sd = np.array([s["inbound_freshness_pct"][1] for s in sup])
        self._cost_index = np.array([s["cost_index"] for s in sup])
        self._monsoon_penalty = np.array([s.get("monsoon_otif_penalty", 0.0) for s in sup])

        self._private_label_supplier = next(
            i for i, s in enumerate(sup) if s.get("private_label_only")
        )
        self._branded_by_category = self._build_supplier_pools()

        # SKU attributes as arrays, so order placement stays vectorised
        self._sku_ids = self.catalog["sku_id"].to_numpy()
        self._shelf_life = self.catalog["shelf_life_days"].to_numpy()
        self._landed_cost = self.catalog["landed_cost"].to_numpy()
        self._is_private = self.catalog["is_private_label"].to_numpy()
        # ordered, unlike cfg.category_names which is a set - the integer
        # category indices below depend on this order being stable
        self._category_names = [c.l1 for c in self.cfg.categories]
        cat_index = {name: i for i, name in enumerate(self._category_names)}
        self._sku_category = self.catalog["l1_category"].map(cat_index).to_numpy()

        self.shocks = self._precompute_shocks()
        self._po_counter = 0
        self._batch_counter = 0

    # ------------------------------------------------------------------ setup
    def _build_supplier_pools(self) -> dict[int, tuple[np.ndarray, np.ndarray]]:
        """Per category: the branded supplier indices and their cumulative shares."""
        cat_index = {c.l1: i for i, c in enumerate(self.cfg.categories)}
        pools: dict[int, list[tuple[int, float]]] = {}
        for si, s in enumerate(self.cfg.suppliers):
            if s.get("private_label_only"):
                continue
            for name, share in s["category_share"].items():
                pools.setdefault(cat_index[name], []).append((si, share))

        out: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        for ci, entries in pools.items():
            idx = np.array([e[0] for e in entries])
            cum = np.cumsum([e[1] for e in entries])
            out[ci] = (idx, cum / cum[-1])
        return out

    def _precompute_shocks(self) -> dict[dt.date, dict]:
        """Resolve supply disruptions once, so both policy arms face the same world."""
        rng = np.random.default_rng([self.seed, 4242])
        start, end = self.cfg.window
        names = self.cfg.category_names
        out: dict[dt.date, dict] = {}

        for shock in self.cfg.raw["suppliers"].get("shocks") or []:
            months = shock.get("months")
            affects = shock["affects_categories"]
            targets = set(names) if affects == "all" else set(affects)
            lo, hi = shock["duration_days"]

            day = start
            while day <= end:
                if (months is None or day.month in months) and rng.random() < shock[
                    "probability_per_day"
                ]:
                    for offset in range(int(rng.integers(lo, hi + 1))):
                        hit = day + dt.timedelta(days=offset)
                        if hit > end:
                            break
                        entry = out.setdefault(hit, {"supply": {}, "freshness": {}, "names": []})
                        entry["names"].append(shock["name"])
                        for cat in targets:
                            entry["supply"][cat] = min(
                                entry["supply"].get(cat, 1.0), shock["supply_factor"]
                            )
                            if "inbound_freshness_override" in shock:
                                entry["freshness"][cat] = shock["inbound_freshness_override"]
                day += dt.timedelta(days=1)
        return out

    # ------------------------------------------------------------------ query
    def supply_factor(self, date: dt.date, category_idx: np.ndarray) -> np.ndarray:
        """Fill-rate multiplier per order, from any shock active on `date`."""
        entry = self.shocks.get(date)
        if not entry or not entry["supply"]:
            return np.ones(len(category_idx))
        lookup = np.array([entry["supply"].get(n, 1.0) for n in self._category_names])
        return lookup[category_idx]

    def _freshness_override(self, date: dt.date, category_idx: np.ndarray) -> np.ndarray:
        entry = self.shocks.get(date)
        if not entry or not entry["freshness"]:
            return np.full(len(category_idx), np.nan)
        lookup = np.array([entry["freshness"].get(n, np.nan) for n in self._category_names])
        return lookup[category_idx]

    def _pick_suppliers(self, sku_idx: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        """Sample a supplier per order line, weighted by configured category share."""
        out = np.empty(len(sku_idx), dtype=np.int32)
        private = self._is_private[sku_idx]
        out[private] = self._private_label_supplier

        branded = ~private
        if branded.any():
            cats = self._sku_category[sku_idx]
            u = rng.random(len(sku_idx))
            for ci, (idx, cum) in self._branded_by_category.items():
                mask = branded & (cats == ci)
                if mask.any():
                    out[mask] = idx[
                        np.searchsorted(cum, u[mask], side="right").clip(0, len(idx) - 1)
                    ]
        return out

    # ------------------------------------------------------------------ orders
    def place_orders(
        self,
        store_idx: np.ndarray,
        sku_idx: np.ndarray,
        qty: np.ndarray,
        order_date: dt.date,
        rng: np.random.Generator,
    ) -> pd.DataFrame:
        """Resolve a day's purchase orders all the way to their arrival outcome.

        The outcome is settled at order time rather than on arrival. Nothing in
        the simulation can observe it early, and resolving it up front keeps the
        daily loop a simple lookup.
        """
        n = len(sku_idx)
        if n == 0:
            return self._empty_po_frame()

        sup = self._pick_suppliers(sku_idx, rng)

        # Gamma lead times: right-skewed, so late is more common than early
        mean, sd = self._lead_mean[sup], self._lead_sd[sup]
        shape = np.clip((mean / np.maximum(sd, 1e-6)) ** 2, 0.5, 5000)
        scale = np.maximum(sd, 1e-6) ** 2 / mean
        lead = rng.gamma(shape, scale)

        # monsoon degrades on-time performance for the produce suppliers
        monsoon = order_date.month in self.cfg.calendar["monsoon"]["months"]
        otif = self._otif[sup] - (self._monsoon_penalty[sup] if monsoon else 0.0)
        failed = rng.random(n) >= np.clip(otif, 0.05, 1.0)
        late = failed & (rng.random(n) < LATE_SHARE_OF_FAILURES)
        short = failed & ~late

        lead = lead + np.where(late, rng.integers(*LATE_DAYS_RANGE, size=n, endpoint=True), 0)
        offset = np.maximum(0, np.floor(lead + 0.5)).astype(int)
        planned = np.maximum(0, np.floor(self._lead_mean[sup] + 0.5)).astype(int)

        # short deliveries, then any active supply shock on top
        fill = np.where(short, rng.uniform(*SHORT_FILL_RANGE, size=n), 1.0)
        fill *= self.supply_factor(order_date, self._sku_category[sku_idx])
        received_qty = np.maximum(0, np.round(qty * fill)).astype(np.int32)

        # inbound freshness: the share of shelf life still remaining on arrival
        fresh = rng.normal(self._fresh_mean[sup], self._fresh_sd[sup])
        override = self._freshness_override(order_date, self._sku_category[sku_idx])
        fresh = np.where(np.isnan(override), fresh, override)
        fresh = np.clip(fresh, MIN_INBOUND_FRESHNESS, 1.0)

        base = self._po_counter
        self._po_counter += n
        po_ids = [f"PO-{base + i:07d}" for i in range(n)]

        return pd.DataFrame(
            {
                "po_id": po_ids,
                "store_id": np.array(self.store_ids)[store_idx],
                "sku_id": self._sku_ids[sku_idx],
                "supplier_id": np.array(self.supplier_ids)[sup],
                "ordered_qty": qty.astype(np.int32),
                "ordered_date": order_date,
                "expected_date": [order_date + dt.timedelta(days=int(d)) for d in planned],
                "received_date": [order_date + dt.timedelta(days=int(d)) for d in offset],
                "received_qty": received_qty,
                "inbound_freshness_pct": np.round(fresh, 4),
                "is_short": short,
                "is_late": offset > planned,
                "_sku_idx": sku_idx,
                "_sup_idx": sup,
            }
        )

    def to_batches(self, pos: pd.DataFrame) -> pd.DataFrame:
        """Materialise received purchase orders as inventory batches.

        Expiry is set from the shelf life *remaining on arrival*, not the full
        shelf life. That single line is what makes supplier quality show up as
        wastage weeks later.
        """
        received = pos[pos["received_qty"] > 0]
        if received.empty:
            return self._empty_batch_frame()

        sku_idx = received["_sku_idx"].to_numpy()
        sup_idx = received["_sup_idx"].to_numpy()
        shelf = self._shelf_life[sku_idx]
        fresh = received["inbound_freshness_pct"].to_numpy()

        usable = np.maximum(MIN_USABLE_DAYS, np.floor(shelf * fresh)).astype(int)
        recv = received["received_date"].to_numpy()
        expiry = [d + dt.timedelta(days=int(u)) for d, u in zip(recv, usable, strict=True)]
        mfg = [e - dt.timedelta(days=int(s)) for e, s in zip(expiry, shelf, strict=True)]

        base = self._batch_counter
        self._batch_counter += len(received)

        return pd.DataFrame(
            {
                "batch_id": [f"BAT-{base + i:08d}" for i in range(len(received))],
                "sku_id": received["sku_id"].to_numpy(),
                "store_id": received["store_id"].to_numpy(),
                "supplier_id": received["supplier_id"].to_numpy(),
                "po_id": received["po_id"].to_numpy(),
                "mfg_date": mfg,
                "expiry_date": expiry,
                "received_date": recv,
                "qty_received": received["received_qty"].to_numpy(),
                # the cheap supplier really is cheaper - that is why nobody drops them
                "unit_landed_cost": np.round(
                    self._landed_cost[sku_idx] * self._cost_index[sup_idx], 2
                ),
                "usable_days": usable,
                "_sku_idx": sku_idx,
            }
        )

    # ------------------------------------------------------------------ empties
    @staticmethod
    def _empty_po_frame() -> pd.DataFrame:
        return pd.DataFrame(
            {
                c: pd.Series(dtype=t)
                for c, t in {
                    "po_id": "object",
                    "store_id": "object",
                    "sku_id": "object",
                    "supplier_id": "object",
                    "ordered_qty": "int32",
                    "ordered_date": "object",
                    "expected_date": "object",
                    "received_date": "object",
                    "received_qty": "int32",
                    "inbound_freshness_pct": "float64",
                    "is_short": "bool",
                    "is_late": "bool",
                    "_sku_idx": "int64",
                    "_sup_idx": "int32",
                }.items()
            }
        )

    @staticmethod
    def _empty_batch_frame() -> pd.DataFrame:
        return pd.DataFrame(
            {
                c: pd.Series(dtype=t)
                for c, t in {
                    "batch_id": "object",
                    "sku_id": "object",
                    "store_id": "object",
                    "supplier_id": "object",
                    "po_id": "object",
                    "mfg_date": "object",
                    "expiry_date": "object",
                    "received_date": "object",
                    "qty_received": "int32",
                    "unit_landed_cost": "float64",
                    "usable_days": "int64",
                    "_sku_idx": "int64",
                }.items()
            }
        )


if __name__ == "__main__":
    from simulator.catalog import build_catalog
    from simulator.config_loader import load_sim_config

    cfg = load_sim_config()
    cat = build_catalog(cfg)
    sc = SupplyChain(cfg, cat)
    rng = np.random.default_rng(3)

    # Short-life dairy only. Averaging cheese at 120 days with milk at 3 makes the
    # supplier gap look enormous and tells you nothing - the difference that costs
    # money is the one on products that were always going to be a race against time.
    dairy = cat.index[
        (cat["l1_category"] == "Dairy & Eggs") & (cat["shelf_life_days"] <= 14)
    ].to_numpy()
    n_rep = 40
    sku_idx = np.tile(dairy, n_rep)
    store_idx = rng.integers(0, len(cfg.stores), len(sku_idx))
    qty = rng.integers(12, 60, len(sku_idx))

    pos = sc.place_orders(store_idx, sku_idx, qty, dt.date(2026, 2, 17), rng)
    batches = sc.to_batches(pos)

    print(f"purchase orders : {len(pos):,}")
    print(f"batches created : {len(batches):,}")
    print(f"short-shipped   : {pos['is_short'].mean():.1%}")
    print(f"late            : {pos['is_late'].mean():.1%}\n")

    joined = batches.merge(
        pos[["po_id", "inbound_freshness_pct"]], on="po_id", suffixes=("", "_po")
    )
    summary = joined.groupby("supplier_id").agg(
        batches=("batch_id", "size"),
        freshness=("inbound_freshness_pct", "mean"),
        usable_days=("usable_days", "mean"),
        landed_cost=("unit_landed_cost", "mean"),
    )
    print("short-life dairy (shelf life <= 14 days) by supplier:")
    print(summary.round(3).to_string())

    a = summary.loc["SUP-DAIRY-A", "usable_days"]
    b = summary.loc["SUP-DAIRY-B", "usable_days"]
    print(f"\nSUP-DAIRY-B ships {a - b:.1f} fewer usable shelf-days than SUP-DAIRY-A")
