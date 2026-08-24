"""Ground-truth demand process (task S1.2).

Expected demand for a store, SKU and day is a product of independent factors:

    lambda(store, sku, day) =
          base_popularity[sku]              Zipf - a few SKUs carry most volume
        * store_scale[store]                catchment size
        * store_category_affinity           Powai buys ready-to-eat, Dadar buys staples
        * day_of_week
        * day_of_month                      salary week up, month end down
        * monsoon                           more orders, less produce
        * festival                          overall lift plus a category mix shift
        * ipl_matchnight                    snacks and cold drinks
        * store_ramp                        a new store does not open at steady state

Realised demand is then drawn from a **negative binomial**, not a Poisson.
Real retail demand is over-dispersed; a Poisson generator would make the
forecast in Sprint 3 look considerably better than it deserves to.

Two multipliers are applied later rather than here, because they depend on
state this module does not own: price (needs the markdown decision) and
freshness (needs the batch's remaining shelf life). Both are exposed as
functions so the order-generation step can apply them per batch.

Nothing in analytics/ may import this module - see tests/test_import_boundary.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from simulator.config_loader import SimConfig

DOW_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass
class DayFactors:
    """Everything the calendar says about one date."""

    date: dt.date
    overall: float
    by_category: np.ndarray  # (n_categories,)
    is_festival: bool
    festival_name: str | None
    is_monsoon: bool
    is_ipl_matchday: bool
    is_salary_week: bool


@dataclass
class DemandModel:
    """Expected and realised demand across the store x SKU grid."""

    cfg: SimConfig
    catalog: pd.DataFrame
    seed: int = 42

    store_ids: list[str] = field(init=False)
    category_names: list[str] = field(init=False)
    sku_category_idx: np.ndarray = field(init=False)
    base: np.ndarray = field(init=False)  # (n_stores, n_skus) units per average day
    factors: dict[dt.date, DayFactors] = field(init=False)

    def __post_init__(self) -> None:
        self.store_ids = [s["store_id"] for s in self.cfg.stores]
        self.category_names = [c.l1 for c in self.cfg.categories]
        cat_index = {name: i for i, name in enumerate(self.category_names)}
        self.sku_category_idx = self.catalog["l1_category"].map(cat_index).to_numpy()

        self._open_dates = np.array(
            [s["opened_date"].toordinal() for s in self.cfg.stores], dtype=np.int64
        )
        self._demand_cfg = self.cfg.raw["catalog"]["demand"]

        self.factors = self._precompute_day_factors()
        self.base = self._build_base_matrix()

    # ------------------------------------------------------------------ setup
    def _precompute_day_factors(self) -> dict[dt.date, DayFactors]:
        cal = self.cfg.calendar
        start, end = self.cfg.window
        n_cat = len(self.category_names)
        cat_index = {name: i for i, name in enumerate(self.category_names)}

        dow = cal["day_of_week_factor"]
        dom = cal["day_of_month_factor"]
        monsoon = cal["monsoon"]
        ipl = cal["ipl"]

        # IPL fixtures are not nightly, and which nights they fall on must be
        # stable across runs or the two policy arms would face different worlds
        ipl_rng = np.random.default_rng([self.seed, 991])
        ipl_days: set[dt.date] = set()
        d = ipl["start_date"]
        while d <= ipl["end_date"]:
            if ipl_rng.random() < ipl["matchday_probability"]:
                ipl_days.add(d)
            d += dt.timedelta(days=1)

        # expand each festival across its lead-up and duration
        festival_days: dict[dt.date, list[dict]] = {}
        for f in cal["festivals"]:
            span = range(-f["lead_days"], f["duration_days"])
            for offset in span:
                day = f["date"] + dt.timedelta(days=offset)
                # the run-up builds; the festival itself is the peak
                ramp = 0.45 + 0.55 * (offset + f["lead_days"] + 1) / (f["lead_days"] + 1)
                weight = min(ramp, 1.0) if offset < 0 else 1.0
                festival_days.setdefault(day, []).append({**f, "_weight": weight})

        out: dict[dt.date, DayFactors] = {}
        day = start
        while day <= end:
            overall = dow[DOW_NAMES[day.weekday()]]

            dnum = day.day
            for block in dom.values():
                lo, hi = block["days"]
                if lo <= dnum <= hi:
                    overall *= block["factor"]
                    break
            is_salary_week = dom["salary_week"]["days"][0] <= dnum <= dom["salary_week"]["days"][1]

            by_cat = np.ones(n_cat)

            is_monsoon = day.month in monsoon["months"]
            if is_monsoon:
                overall *= monsoon["order_volume_factor"]
                for name, mult in monsoon["categories_boosted"].items():
                    by_cat[cat_index[name]] *= mult
                for name, mult in monsoon["categories_suppressed"].items():
                    by_cat[cat_index[name]] *= mult

            fests = festival_days.get(day, [])
            for f in fests:
                w = f["_weight"]
                # blend toward 1.0 during the run-up rather than applying full force
                overall *= 1.0 + (f["overall_factor"] - 1.0) * w
                for name, mult in (f.get("categories") or {}).items():
                    by_cat[cat_index[name]] *= 1.0 + (mult - 1.0) * w

            is_ipl = day in ipl_days
            if is_ipl:
                for name, mult in ipl["categories_boosted"].items():
                    by_cat[cat_index[name]] *= mult

            out[day] = DayFactors(
                date=day,
                overall=overall,
                by_category=by_cat,
                is_festival=bool(fests),
                festival_name=fests[0]["name"] if fests else None,
                is_monsoon=is_monsoon,
                is_ipl_matchday=is_ipl,
                is_salary_week=is_salary_week,
            )
            day += dt.timedelta(days=1)
        return out

    def _build_base_matrix(self) -> np.ndarray:
        popularity = self.catalog["popularity_weight"].to_numpy()
        scale = np.array([s["demand_index"] for s in self.cfg.stores])

        affinity = np.ones((len(self.store_ids), len(self.category_names)))
        cat_index = {name: i for i, name in enumerate(self.category_names)}
        for si, store in enumerate(self.cfg.stores):
            for name, mult in (store.get("category_affinity") or {}).items():
                affinity[si, cat_index[name]] = mult

        base = scale[:, None] * popularity[None, :] * affinity[:, self.sku_category_idx]
        base /= base.sum()
        base *= self._demand_cfg["network_daily_units_baseline"]

        # The calendar factors average well above 1.0, so an uncalibrated model
        # would overshoot the target volume by ~20%. Divide it back out, and the
        # annual total lands where the scale anchor says it should.
        return base / self._mean_calendar_multiplier(base)

    def _mean_calendar_multiplier(self, base: np.ndarray) -> float:
        cat_share = np.zeros(len(self.category_names))
        per_sku = base.sum(axis=0)
        for ci in range(len(self.category_names)):
            cat_share[ci] = per_sku[self.sku_category_idx == ci].sum()
        cat_share /= cat_share.sum()

        daily = [f.overall * float(cat_share @ f.by_category) for f in self.factors.values()]
        return float(np.mean(daily))

    # ------------------------------------------------------------------ query
    def store_ramp(self, date: dt.date) -> np.ndarray:
        """A store opening mid-window ramps up; before opening it sells nothing."""
        ramp_days = self._demand_cfg["new_store_ramp_days"]
        opening = self._demand_cfg["new_store_opening_factor"]

        age = date.toordinal() - self._open_dates
        return np.where(
            age < 0,
            0.0,
            np.minimum(1.0, opening + (1 - opening) * np.clip(age / ramp_days, 0, 1)),
        )

    def daily_lambda(self, date: dt.date) -> np.ndarray:
        """Expected units by store x SKU for one day, before price and freshness."""
        f = self.factors[date]
        cat_mult = f.by_category[self.sku_category_idx]
        return self.base * f.overall * cat_mult[None, :] * self.store_ramp(date)[:, None]

    def sample(self, lam: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        """Draw realised demand. Negative binomial, so variance exceeds the mean."""
        k_hi = self._demand_cfg["dispersion_k"]
        k_lo = self._demand_cfg["dispersion_k_low_volume"]
        threshold = self._demand_cfg["low_volume_threshold_units"]

        # thin, slow-moving SKUs are lumpier than fast movers
        k = np.where(lam < threshold, k_lo, k_hi)
        safe = np.maximum(lam, 1e-12)
        p = k / (k + safe)
        drawn = rng.negative_binomial(k, p)
        return np.where(lam <= 0, 0, drawn).astype(np.int32)

    # ------------------------------------------------------------- multipliers
    def hour_weights(self) -> np.ndarray:
        """(n_skus, 24) intraday allocation, by the curve each subcategory rides."""
        curves = self.cfg.calendar["hour_curves"]
        lookup = {name: np.asarray(vals) for name, vals in curves.items()}
        return np.vstack([lookup[c] for c in self.catalog["hour_curve"]])

    def expected_annual_units(self) -> float:
        return float(sum(self.daily_lambda(d).sum() for d in self.factors))


# --------------------------------------------------------------------- helpers
def price_multiplier(price_ratio: np.ndarray | float, elasticity: np.ndarray | float):
    """Constant-elasticity demand response: (p / p_base) ** elasticity.

    A 20% markdown on an elasticity of -1.4 lifts demand by about 35%. The
    markdown optimiser has to recover these elasticities from observed price
    variation, which is confounded - prices moved for reasons correlated with
    demand - and that confounding is deliberate.
    """
    return np.power(np.maximum(price_ratio, 1e-6), elasticity)


def freshness_multiplier(remaining_fraction: np.ndarray | float, cfg: SimConfig):
    """P(buy) as remaining shelf life runs out, even at a discount.

    This is why a flat 'X% off at D-2' ladder fails: the discount that clears a
    five-day-old yoghurt will not clear a one-day-old one, because the customer
    is not only weighing price.
    """
    curve = cfg.raw["catalog"]["freshness_acceptance"]["curve"]
    xs = np.array(sorted(curve))
    ys = np.array([curve[x] for x in xs])
    return np.interp(np.clip(remaining_fraction, 0.0, 1.0), xs, ys)


if __name__ == "__main__":
    from simulator.catalog import build_catalog
    from simulator.config_loader import load_sim_config

    cfg = load_sim_config()
    model = DemandModel(cfg, build_catalog(cfg))
    rng = np.random.default_rng(7)

    annual = model.expected_annual_units()
    print(f"expected annual units : {annual / 1e6:.2f}M")
    print(f"average day           : {annual / 365:,.0f} units")

    def day_total(d: dt.date) -> float:
        return float(model.daily_lambda(d).sum())

    interesting = [
        ("Diwali", dt.date(2025, 10, 20)),
        ("Navratri", dt.date(2025, 9, 25)),
        ("ordinary Tuesday", dt.date(2026, 2, 17)),
        ("month end", dt.date(2026, 2, 26)),
        ("salary week Saturday", dt.date(2026, 2, 7)),
    ]
    baseline = annual / 365
    print()
    for label, d in interesting:
        print(f"  {label:<22} {day_total(d):>9,.0f} units   ({day_total(d) / baseline:.2f}x)")

    lam = model.daily_lambda(dt.date(2026, 2, 17))
    drawn = model.sample(lam, rng)
    print(f"\nsampled units on a normal day: {drawn.sum():,}  (lambda {lam.sum():,.0f})")
    print(
        f"variance / mean across cells : {drawn.var() / drawn.mean():.2f}  (Poisson would be 1.0)"
    )
