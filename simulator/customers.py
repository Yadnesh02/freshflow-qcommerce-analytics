"""Customers, baskets and the churn hazard (task S1.4).

Two things live here.

**The customer base** - who exists, when they joined, which store they shop,
which behavioural segment they belong to, and when they go dormant. The
segment label is generator ground truth and never leaves the simulator; the
segmentation in mart_customer_360 has to be inferred from order history.

**The churn hazard** - and this is the load-bearing part of the whole project.
Monthly churn probability rises with stockouts on a customer's favourite SKUs,
with receiving items close to expiry, and with late deliveries. It falls with
consistent availability and with successfully redeeming a deal.

Without that mechanism, the retention lift reported in the Sprint 5 experiment
would be a coincidence asserted rather than an effect measured. It is the
causal path from "we managed inventory better" to "customers stayed", and it
is why plan section 11 marks this task as the one never to cut.

Baskets are *assembled from* the demand the model in demand.py already
produced, rather than generated independently. One source of truth for volume:
segment preferences decide **who** buys a unit, never **how many** units a
store sells.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from simulator.config_loader import SimConfig

# how long before the window opens the founding cohort signed up
INITIAL_TENURE_DAYS = 540
# a new store pulls in customers faster for its first few months
NEW_STORE_WINDOW_DAYS = 90
# delivery promise, and how often it slips
PROMISE_MINUTES = 15
BASE_LATE_RATE = 0.055
PEAK_HOUR_LATE_MULTIPLIER = 2.1
MONSOON_LATE_MULTIPLIER = 1.7
PEAK_HOURS = (8, 9, 10, 19, 20, 21, 22)
# a delivery is "low freshness" to the customer below this share of shelf life
LOW_DTE_FRACTION = 0.25

DEVICES = ("android", "ios", "web")
DEVICE_WEIGHTS = (0.68, 0.24, 0.08)
PAYMENT_MODES = ("upi", "card", "wallet", "cod")
PAYMENT_WEIGHTS = (0.61, 0.19, 0.13, 0.07)


# =============================================================== customer base
@dataclass
class CustomerBase:
    """The customer master, plus the churn process that retires people from it."""

    cfg: SimConfig
    seed: int = 42

    df: pd.DataFrame = field(init=False)
    segment_names: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self.segment_names = [s["name"] for s in self.cfg.segments]
        self._seg_index = {n: i for i, n in enumerate(self.segment_names)}
        self.store_ids = [s["store_id"] for s in self.cfg.stores]

        acq = self.cfg.raw["segments"]["acquisition"]
        self._churn_cfg = self.cfg.raw["segments"]["churn"]
        self._base_hazard = np.array(
            [self._churn_cfg["base_monthly_hazard"][n] for n in self.segment_names]
        )
        self._orders_per_month = np.array(
            [np.mean(s["orders_per_month"]) for s in self.cfg.segments]
        )

        self.df = self._generate(acq)

        n = len(self.df)
        self._churn_date = np.full(n, None, dtype=object)
        # event counters, reset each month when the hazard is evaluated
        self._ev_stockout = np.zeros(n, dtype=np.int32)
        self._ev_low_dte = np.zeros(n, dtype=np.int32)
        self._ev_late = np.zeros(n, dtype=np.int32)
        self._ev_cancelled = np.zeros(n, dtype=np.int32)
        self._ev_deal = np.zeros(n, dtype=np.int32)
        self._ev_orders = np.zeros(n, dtype=np.int32)

        self._segment_idx = self.df["_segment_idx"].to_numpy()
        self._store_idx = self.df["_home_store_idx"].to_numpy()
        self._signup_ord = np.array([d.toordinal() for d in self.df["signup_date"]])

    # ------------------------------------------------------------------ setup
    def _generate(self, acq: dict) -> pd.DataFrame:
        rng = np.random.default_rng([self.seed, 7001])
        start, end = self.cfg.window
        shares = np.array([s["share"] for s in self.cfg.segments])
        store_weight = np.array([s["demand_index"] for s in self.cfg.stores])
        open_ord = np.array([s["opened_date"].toordinal() for s in self.cfg.stores])

        def pick_stores(signup_ords: np.ndarray) -> np.ndarray:
            """Only stores already trading can acquire a customer.

            Vectorised: a per-customer loop over 48k rng.choice calls dominated
            the whole simulator's runtime.
            """
            age = signup_ords[:, None] - open_ord[None, :]
            live = age >= 0
            w = np.where(live, store_weight[None, :], 0.0)
            # a newly opened store acquires harder in its first months
            recent = live & (age <= NEW_STORE_WINDOW_DAYS)
            w = np.where(recent, w * acq["new_store_uplift"], w)

            # anyone predating every store attaches to the first one to open
            orphan = w.sum(axis=1) == 0
            if orphan.any():
                w[orphan, int(np.argmin(open_ord))] = 1.0

            cum = np.cumsum(w, axis=1)
            cum /= cum[:, -1:]
            draw = rng.random(len(signup_ords))
            return (cum < draw[:, None]).sum(axis=1).clip(0, len(open_ord) - 1).astype(np.int32)

        # founding cohort - already customers when the window opens
        n0 = acq["initial_customers"]
        tenure = rng.integers(1, INITIAL_TENURE_DAYS, n0)
        signups = [start - dt.timedelta(days=int(t)) for t in tenure]

        # then a monthly intake across the window
        month = dt.date(start.year, start.month, 1)
        lo, hi = acq["monthly_new_customers"]
        while month <= end:
            n_new = int(rng.integers(lo, hi + 1))
            days = rng.integers(0, 28, n_new)
            for d in days:
                day = month + dt.timedelta(days=int(d))
                if start <= day <= end:
                    signups.append(day)
            month = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)

        signups = sorted(signups)
        n = len(signups)
        signup_ords = np.array([d.toordinal() for d in signups])
        home_idx = pick_stores(signup_ords)

        return pd.DataFrame(
            {
                "customer_id": [f"CUS-{i:07d}" for i in range(n)],
                "signup_date": signups,
                "home_store_id": np.array(self.store_ids)[home_idx],
                "_home_store_idx": home_idx,
                "acquisition_channel": rng.choice(
                    list(acq["channel_mix"]), size=n, p=list(acq["channel_mix"].values())
                ),
                "device": rng.choice(DEVICES, size=n, p=DEVICE_WEIGHTS),
                "is_member": rng.random(n) < 0.22,
                # ground truth - stripped before anything is emitted as source data
                "_segment_idx": rng.choice(len(shares), size=n, p=shares),
            }
        )

    # ------------------------------------------------------------------ state
    def active_mask(self, date: dt.date) -> np.ndarray:
        """Signed up, and not yet churned."""
        signed = self._signup_ord <= date.toordinal()
        churned = np.array([c is not None and c <= date for c in self._churn_date], dtype=bool)
        return signed & ~churned

    def active_by_store(self, date: dt.date) -> dict[int, np.ndarray]:
        active = np.flatnonzero(self.active_mask(date))
        return {si: active[self._store_idx[active] == si] for si in range(len(self.store_ids))}

    # ------------------------------------------------------------------ events
    def record(self, kind: str, customer_rows: np.ndarray) -> None:
        """Log an experience that will move this month's churn hazard."""
        target = {
            "stockout": self._ev_stockout,
            "low_dte": self._ev_low_dte,
            "late": self._ev_late,
            "cancelled": self._ev_cancelled,
            "deal": self._ev_deal,
            "order": self._ev_orders,
        }[kind]
        np.add.at(target, customer_rows, 1)

    def hazard(self, date: dt.date) -> np.ndarray:
        """Monthly churn probability per customer, given this month's experience."""
        c = self._churn_cfg
        h = self._base_hazard[self._segment_idx].copy()

        # tenure protects: long-standing customers are harder to lose
        months = np.maximum(0, (date.toordinal() - self._signup_ord) / 30.44)
        h *= np.maximum(c["tenure_decay_per_month"] ** months, c["tenure_floor"])

        w = c["worsened_by"]
        worse = (
            w["stockout_on_favourite_sku"] ** self._ev_stockout
            * w["received_low_dte_item"] ** self._ev_low_dte
            * w["late_delivery"] ** self._ev_late
            * w["order_cancelled"] ** self._ev_cancelled
        )
        h *= np.minimum(worse, c["max_worsening_multiplier"])

        b = c["improved_by"]
        better = b["successful_deal_redemption"] ** np.minimum(self._ev_deal, 3)
        # a clean month for someone who actually shopped is itself retentive
        clean = (self._ev_orders > 0) & (self._ev_stockout == 0)
        better = np.where(clean, better * b["consistent_availability_30d"], better)
        h *= np.maximum(better, c["min_improvement_multiplier"])

        return np.clip(h, 0.0, 0.95)

    def step_month(self, date: dt.date, rng: np.random.Generator) -> int:
        """Evaluate churn for everyone active, then clear the month's counters."""
        active = self.active_mask(date)
        h = self.hazard(date)
        churning = active & (rng.random(len(h)) < h)
        for i in np.flatnonzero(churning):
            self._churn_date[i] = date

        for arr in (
            self._ev_stockout,
            self._ev_low_dte,
            self._ev_late,
            self._ev_cancelled,
            self._ev_deal,
            self._ev_orders,
        ):
            arr[:] = 0
        return int(churning.sum())

    def to_bronze(self) -> pd.DataFrame:
        """Customer master as a source feed - latent segment removed."""
        out = self.df.drop(columns=[c for c in self.df.columns if c.startswith("_")])
        return out.assign(churn_date=self._churn_date)


# =============================================================== baskets
@dataclass
class BasketAssembler:
    """Turns a store-day's unit demand into orders and order lines.

    Segment preference decides which customer buys a unit, never how many units
    the store sells - so per-SKU totals coming out of the demand model are
    preserved exactly. Getting this backwards would double-count the store
    category affinity that demand.py has already applied.
    """

    cfg: SimConfig
    catalog: pd.DataFrame
    customers: CustomerBase

    def __post_init__(self) -> None:
        segs = self.cfg.segments
        cats = [c.l1 for c in self.cfg.categories]
        cat_index = {n: i for i, n in enumerate(cats)}
        self._sku_cat = self.catalog["l1_category"].map(cat_index).to_numpy()
        self._is_private = self.catalog["is_private_label"].to_numpy()
        self._sku_ids = self.catalog["sku_id"].to_numpy()
        self._hour_curves = np.vstack(
            [np.asarray(self.cfg.calendar["hour_curves"][c]) for c in self.catalog["hour_curve"]]
        )

        # segment x category preference, and the private-label tilt
        self._bias = np.ones((len(segs), len(cats)))
        for si, s in enumerate(segs):
            for name, mult in (s.get("category_bias") or {}).items():
                self._bias[si, cat_index[name]] = mult
        self._pl_affinity = np.array([s["private_label_affinity"] for s in segs])
        self._basket_lo = np.array([s["basket_items"][0] for s in segs])
        self._basket_hi = np.array([s["basket_items"][1] for s in segs])
        self._order_rate = np.array([np.mean(s["orders_per_month"]) for s in segs])

        self._order_counter = 0

    def _segment_weights(self, sku_idx: np.ndarray) -> np.ndarray:
        """(n_segments, n_units) relative appetite of each segment for each unit."""
        w = self._bias[:, self._sku_cat[sku_idx]]
        pl = self._is_private[sku_idx]
        return np.where(pl[None, :], w * self._pl_affinity[:, None], w)

    def assemble(
        self,
        store_idx: int,
        units: np.ndarray,
        date: dt.date,
        active_rows: np.ndarray,
        rng: np.random.Generator,
        is_monsoon: bool = False,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Build one store's orders for one day from its sampled unit demand."""
        sold = np.flatnonzero(units > 0)
        if sold.size == 0 or active_rows.size == 0:
            return _empty_orders(), _empty_items()

        unit_sku = np.repeat(sold, units[sold])
        total_units = unit_sku.size

        # who is shopping today: segment mix of this store's live customers,
        # weighted by how often each segment orders
        seg_of = self.customers._segment_idx[active_rows]
        seg_counts = np.bincount(seg_of, minlength=len(self._order_rate))
        seg_appetite = seg_counts * self._order_rate
        if seg_appetite.sum() == 0:
            return _empty_orders(), _empty_items()
        seg_share = seg_appetite / seg_appetite.sum()

        # split every unit across segments, preserving per-SKU totals exactly
        pref = self._segment_weights(unit_sku) * seg_share[:, None]
        pref /= pref.sum(axis=0, keepdims=True)
        cum = np.cumsum(pref, axis=0)
        unit_seg = (cum < rng.random(total_units)[None, :]).sum(axis=0).clip(0, len(seg_share) - 1)

        order_frames: list[pd.DataFrame] = []
        item_frames: list[pd.DataFrame] = []
        next_id = self._order_counter

        for s in range(len(seg_share)):
            pool = unit_sku[unit_seg == s]
            candidates = active_rows[seg_of == s]
            if pool.size == 0 or candidates.size == 0:
                continue
            rng.shuffle(pool)

            # carve this segment's units into baskets of its own typical size
            draw = rng.integers(self._basket_lo[s], self._basket_hi[s] + 1, pool.size)
            bounds = np.cumsum(draw)
            n_baskets = int(np.searchsorted(bounds, pool.size, side="left")) + 1
            ends = np.minimum(bounds[:n_baskets], pool.size)
            starts = np.concatenate(([0], ends[:-1]))
            sizes = ends - starts
            keep = sizes > 0
            starts, sizes = starts[keep], sizes[keep]
            n_baskets = len(sizes)
            if n_baskets == 0:
                continue

            order_of_unit = np.repeat(np.arange(n_baskets), sizes)
            first_sku = pool[starts]

            # order hour follows the intraday curve of the basket's lead item
            curves = self._hour_curves[first_sku]
            ccum = np.cumsum(curves, axis=1)
            ccum /= ccum[:, -1:]
            hours = (ccum < rng.random(n_baskets)[:, None]).sum(axis=1).clip(0, 23)
            minutes = rng.integers(0, 60, n_baskets)

            late_p = np.full(n_baskets, BASE_LATE_RATE)
            late_p = np.where(
                np.isin(hours, PEAK_HOURS), late_p * PEAK_HOUR_LATE_MULTIPLIER, late_p
            )
            if is_monsoon:
                late_p = late_p * MONSOON_LATE_MULTIPLIER
            is_late = rng.random(n_baskets) < np.minimum(late_p, 0.6)
            # an on-time order must actually beat the promise, or the is_late
            # flag and the timestamps tell different stories
            slip = np.where(is_late, rng.integers(6, 40, n_baskets), rng.integers(-9, 1, n_baskets))

            order_ids = np.array([f"ORD-{next_id + i:08d}" for i in range(n_baskets)], dtype=object)
            next_id += n_baskets

            order_ts = np.array(
                [
                    dt.datetime(date.year, date.month, date.day, int(h), int(m))
                    for h, m in zip(hours, minutes, strict=True)
                ],
                dtype=object,
            )
            order_frames.append(
                pd.DataFrame(
                    {
                        "order_id": order_ids,
                        "customer_row": rng.choice(candidates, size=n_baskets, replace=True),
                        "store_idx": store_idx,
                        "order_ts": order_ts,
                        "promised_ts": [
                            t + dt.timedelta(minutes=PROMISE_MINUTES) for t in order_ts
                        ],
                        "delivered_ts": [
                            t + dt.timedelta(minutes=int(PROMISE_MINUTES + d))
                            for t, d in zip(order_ts, slip, strict=True)
                        ],
                        "is_late": is_late,
                        "payment_mode": rng.choice(
                            PAYMENT_MODES, size=n_baskets, p=PAYMENT_WEIGHTS
                        ),
                        "n_units": sizes,
                    }
                )
            )

            # repeats of the same SKU inside one basket collapse to a line with qty > 1
            lines = (
                pd.DataFrame({"o": order_of_unit, "sku_idx": pool[: order_of_unit.size]})
                .groupby(["o", "sku_idx"], sort=False)
                .size()
                .reset_index(name="qty")
            )
            item_frames.append(
                pd.DataFrame(
                    {
                        "order_id": order_ids[lines["o"].to_numpy()],
                        "sku_idx": lines["sku_idx"].to_numpy(),
                        "sku_id": self._sku_ids[lines["sku_idx"].to_numpy()],
                        "qty": lines["qty"].to_numpy(),
                    }
                )
            )

        if not order_frames:
            return _empty_orders(), _empty_items()
        orders = pd.concat(order_frames, ignore_index=True)
        items = pd.concat(item_frames, ignore_index=True)
        self._order_counter = next_id
        return orders, items


def _empty_orders() -> pd.DataFrame:
    return pd.DataFrame(
        {
            c: pd.Series(dtype=t)
            for c, t in {
                "order_id": "object",
                "customer_row": "int64",
                "store_idx": "int64",
                "order_ts": "object",
                "promised_ts": "object",
                "delivered_ts": "object",
                "is_late": "bool",
                "payment_mode": "object",
                "n_units": "int64",
            }.items()
        }
    )


def _empty_items() -> pd.DataFrame:
    return pd.DataFrame(
        {
            c: pd.Series(dtype=t)
            for c, t in {
                "order_id": "object",
                "sku_idx": "int64",
                "sku_id": "object",
                "qty": "int64",
            }.items()
        }
    )


if __name__ == "__main__":
    from simulator.catalog import build_catalog
    from simulator.config_loader import load_sim_config
    from simulator.demand import DemandModel

    cfg = load_sim_config()
    cat = build_catalog(cfg)
    base = CustomerBase(cfg)
    model = DemandModel(cfg, cat)
    asm = BasketAssembler(cfg, cat, base)
    rng = np.random.default_rng(5)

    day = dt.date(2026, 2, 17)
    print(f"customers generated : {len(base.df):,}")
    print(f"active on {day}  : {base.active_mask(day).sum():,}")
    seg_mix = base.df["_segment_idx"].value_counts(normalize=True).sort_index()
    print(
        "segment mix         : "
        + ", ".join(f"{base.segment_names[i]} {v:.0%}" for i, v in seg_mix.items())
    )

    lam = model.daily_lambda(day)
    units = model.sample(lam, rng)
    by_store = base.active_by_store(day)

    all_orders, all_items = [], []
    for si in range(len(cfg.stores)):
        o, it = asm.assemble(si, units[si], day, by_store[si], rng)
        all_orders.append(o)
        all_items.append(it)
    orders = pd.concat(all_orders, ignore_index=True)
    items = pd.concat(all_items, ignore_index=True)

    print(f"\nunits demanded      : {units.sum():,}")
    print(f"orders assembled    : {len(orders):,}")
    print(f"order lines         : {len(items):,}")
    print(f"units in baskets    : {items['qty'].sum():,}  (must equal units demanded)")
    print(f"mean basket (units) : {orders['n_units'].mean():.2f}")
    print(f"late deliveries     : {orders['is_late'].mean():.1%}")

    # churn responds to experience
    rows = np.flatnonzero(base.active_mask(day))[:4000]
    quiet = base.hazard(day)[rows].mean()
    base.record("stockout", rows)
    base.record("late", rows)
    rough = base.hazard(day)[rows].mean()
    print(f"\nmonthly churn hazard: {quiet:.3%} after a quiet month")
    print(f"                      {rough:.3%} after a stockout and a late delivery")
