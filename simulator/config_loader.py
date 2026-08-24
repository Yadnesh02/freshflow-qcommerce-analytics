"""Load and validate the simulator configuration (task S1.1).

Five YAML files define the whole simulated world: the store network, the
product catalogue, the calendar of demand-shaping events, customer segments and
suppliers. They are the vocabulary everything downstream is written in, so they
are validated hard and early - a demand_weight that does not sum to 1.0 would
quietly distort every number in the project.

    from simulator.config_loader import load_sim_config

    cfg = load_sim_config()
    cfg.total_sku_count          # 1500
    cfg.category("Dairy & Eggs").elasticity

Named config_loader rather than config because simulator/config/ is a directory
and the two would shadow each other on import.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).parent / "config"
FILES = ("stores", "catalog", "calendar", "segments", "suppliers", "policies")

# how far a set of shares may sum from 1.0 before it is a bug rather than
# floating-point noise
SHARE_TOLERANCE = 1e-6


class ConfigError(ValueError):
    """A simulator config file is internally inconsistent."""


@dataclass(frozen=True)
class Subcategory:
    l1: str
    name: str
    sku_count: int
    shelf_life_days: tuple[int, int]
    price: tuple[float, float]
    margin: tuple[float, float]
    hour_curve: str

    @property
    def is_perishable(self) -> bool:
        return self.shelf_life_days[1] <= 14


@dataclass(frozen=True)
class Category:
    l1: str
    temp_zone: str
    demand_weight: float
    elasticity: float
    private_label_share: float
    subcategories: tuple[Subcategory, ...]

    @property
    def sku_count(self) -> int:
        return sum(s.sku_count for s in self.subcategories)


@dataclass(frozen=True)
class SimConfig:
    stores: list[dict[str, Any]]
    categories: tuple[Category, ...]
    calendar: dict[str, Any]
    segments: list[dict[str, Any]]
    suppliers: list[dict[str, Any]]
    policies: dict[str, Any]
    raw: dict[str, dict[str, Any]]

    # ---------------------------------------------------------------- lookups
    def category(self, l1: str) -> Category:
        for c in self.categories:
            if c.l1 == l1:
                return c
        raise KeyError(f"unknown category '{l1}'")

    @property
    def category_names(self) -> set[str]:
        return {c.l1 for c in self.categories}

    @property
    def subcategories(self) -> list[Subcategory]:
        return [s for c in self.categories for s in c.subcategories]

    @property
    def total_sku_count(self) -> int:
        return sum(c.sku_count for c in self.categories)

    @property
    def perishable_sku_count(self) -> int:
        return sum(s.sku_count for s in self.subcategories if s.is_perishable)

    @property
    def expected_private_label_count(self) -> int:
        return sum(round(c.sku_count * c.private_label_share) for c in self.categories)

    @property
    def store_ids(self) -> list[str]:
        return [s["store_id"] for s in self.stores]

    @property
    def window(self) -> tuple[dt.date, dt.date]:
        w = self.calendar["window"]
        return w["start_date"], w["end_date"]

    @property
    def window_days(self) -> int:
        start, end = self.window
        return (end - start).days + 1


# --------------------------------------------------------------------- helpers
def _read(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must parse to a mapping")
    return data


def _check_shares(values: dict[str, float], label: str, expected: float = 1.0) -> None:
    total = sum(values.values())
    if abs(total - expected) > SHARE_TOLERANCE:
        worst = ", ".join(f"{k}={v}" for k, v in sorted(values.items())[:4])
        raise ConfigError(
            f"{label} sums to {total:.6f}, expected {expected}. First few: {worst} ..."
        )


def _check_range(rng: Any, label: str, *, allow_equal: bool = False) -> tuple[float, float]:
    if not (isinstance(rng, list | tuple) and len(rng) == 2):
        raise ConfigError(f"{label} must be a two-element [min, max], got {rng!r}")
    lo, hi = rng
    if lo > hi or (lo == hi and not allow_equal):
        raise ConfigError(f"{label} has min {lo} >= max {hi}")
    return float(lo), float(hi)


# --------------------------------------------------------------------- parsing
def _parse_categories(catalog: dict[str, Any], hour_curves: set[str]) -> tuple[Category, ...]:
    out: list[Category] = []
    for c in catalog["categories"]:
        l1 = c["l1"]
        subs: list[Subcategory] = []
        for s in c["subcategories"]:
            where = f"{l1} / {s['name']}"
            shelf = _check_range(s["shelf_life_days"], f"{where} shelf_life_days")
            price = _check_range(s["price"], f"{where} price")
            margin = _check_range(s["margin"], f"{where} margin")
            if s["hour_curve"] not in hour_curves:
                raise ConfigError(
                    f"{where} uses hour_curve '{s['hour_curve']}', "
                    f"not defined in calendar.yaml ({sorted(hour_curves)})"
                )
            if s["sku_count"] < 1:
                raise ConfigError(f"{where} has sku_count {s['sku_count']}")
            subs.append(
                Subcategory(
                    l1=l1,
                    name=s["name"],
                    sku_count=int(s["sku_count"]),
                    shelf_life_days=(int(shelf[0]), int(shelf[1])),
                    price=price,
                    margin=margin,
                    hour_curve=s["hour_curve"],
                )
            )
        if c["elasticity"] >= 0:
            raise ConfigError(
                f"category '{l1}' has elasticity {c['elasticity']}, expected negative"
            )
        out.append(
            Category(
                l1=l1,
                temp_zone=c["temp_zone"],
                demand_weight=float(c["demand_weight"]),
                elasticity=float(c["elasticity"]),
                private_label_share=float(c["private_label_share"]),
                subcategories=tuple(subs),
            )
        )
    return tuple(out)


# ------------------------------------------------------------------ validation
def _validate_calendar(cal: dict[str, Any]) -> None:
    start, end = cal["window"]["start_date"], cal["window"]["end_date"]
    if not isinstance(start, dt.date) or not isinstance(end, dt.date):
        raise ConfigError("calendar window dates must parse as dates")
    if start >= end:
        raise ConfigError(f"calendar window start {start} is not before end {end}")

    dow = cal["day_of_week_factor"]
    if len(dow) != 7:
        raise ConfigError(f"day_of_week_factor has {len(dow)} entries, expected 7")

    for name, curve in cal["hour_curves"].items():
        if len(curve) != 24:
            raise ConfigError(f"hour curve '{name}' has {len(curve)} entries, expected 24")
        total = sum(curve)
        if abs(total - 1.0) > 1e-3:
            raise ConfigError(f"hour curve '{name}' sums to {total:.4f}, expected 1.0")


def _validate_festivals(cal: dict[str, Any], category_names: set[str]) -> None:
    """A festival outside the window shapes nothing and is almost always a typo."""
    start, end = cal["window"]["start_date"], cal["window"]["end_date"]
    seen: set[str] = set()

    for f in cal["festivals"]:
        name = f["name"]
        if name in seen:
            raise ConfigError(f"duplicate festival '{name}'")
        seen.add(name)

        # the shopping run-up counts as part of the festival's span
        span_start = f["date"] - dt.timedelta(days=f["lead_days"])
        span_end = f["date"] + dt.timedelta(days=f["duration_days"] - 1)
        if span_end < start or span_start > end:
            raise ConfigError(
                f"festival '{name}' spans {span_start}..{span_end}, entirely outside the "
                f"simulation window {start}..{end} - it would have no effect"
            )
        if f["overall_factor"] <= 0:
            raise ConfigError(f"festival '{name}' has non-positive overall_factor")
        unknown = set(f.get("categories") or {}) - category_names
        if unknown:
            raise ConfigError(f"festival '{name}' targets unknown categories {sorted(unknown)}")

    for block in ("monsoon", "ipl"):
        for key in ("categories_boosted", "categories_suppressed"):
            unknown = set(cal[block].get(key) or {}) - category_names
            if unknown:
                raise ConfigError(f"{block}.{key} references unknown categories {sorted(unknown)}")


def _validate_catalog(catalog: dict[str, Any], categories: tuple[Category, ...]) -> None:
    _check_shares({c.l1: c.demand_weight for c in categories}, "catalog demand_weight")

    names = {c.l1 for c in categories}
    if len(names) != len(categories):
        raise ConfigError("duplicate category l1 names in catalog.yaml")

    sub = catalog["substitution"]
    _check_shares(
        {k: sub[k] for k in ("within_subcategory", "to_private_label", "lost")},
        "substitution shares",
    )
    for cat, override in (sub.get("overrides") or {}).items():
        if cat not in names:
            raise ConfigError(f"substitution override for unknown category '{cat}'")
        _check_shares(override, f"substitution override for '{cat}'")

    curve = catalog["freshness_acceptance"]["curve"]
    fractions = sorted(curve, reverse=True)
    acceptances = [curve[f] for f in fractions]
    if acceptances != sorted(acceptances, reverse=True):
        raise ConfigError(
            "freshness_acceptance curve is not monotonic - acceptance must fall as "
            "remaining shelf life falls"
        )


def _validate_stores(stores: list[dict[str, Any]], category_names: set[str]) -> None:
    ids = [s["store_id"] for s in stores]
    if len(set(ids)) != len(ids):
        raise ConfigError("duplicate store_id in stores.yaml")
    for s in stores:
        if s["demand_index"] <= 0:
            raise ConfigError(f"{s['store_id']} has non-positive demand_index")
        if s["catchment_tier"] not in {"premium", "mid", "mass"}:
            raise ConfigError(f"{s['store_id']} has unknown tier '{s['catchment_tier']}'")
        for cat in s.get("category_affinity") or {}:
            if cat not in category_names:
                raise ConfigError(f"{s['store_id']} has affinity for unknown category '{cat}'")


def _validate_segments(seg_doc: dict[str, Any], category_names: set[str]) -> None:
    segments = seg_doc["segments"]
    _check_shares({s["name"]: s["share"] for s in segments}, "segment shares")

    names = {s["name"] for s in segments}
    for s in segments:
        _check_range(s["orders_per_month"], f"segment '{s['name']}' orders_per_month")
        _check_range(s["basket_items"], f"segment '{s['name']}' basket_items")
        for cat in s.get("category_bias") or {}:
            if cat not in category_names:
                raise ConfigError(f"segment '{s['name']}' biases unknown category '{cat}'")

    hazards = seg_doc["churn"]["base_monthly_hazard"]
    missing = names - set(hazards)
    if missing:
        raise ConfigError(f"segments with no churn hazard defined: {sorted(missing)}")
    for name, h in hazards.items():
        if not 0 < h < 1:
            raise ConfigError(f"churn hazard for '{name}' is {h}, expected between 0 and 1")

    _check_shares(seg_doc["acquisition"]["channel_mix"], "acquisition channel_mix")


def _validate_suppliers(
    sup_doc: dict[str, Any], category_names: set[str], categories: tuple[Category, ...]
) -> None:
    suppliers = sup_doc["suppliers"]
    ids = [s["supplier_id"] for s in suppliers]
    if len(set(ids)) != len(ids):
        raise ConfigError("duplicate supplier_id in suppliers.yaml")

    # every category needs branded supply whose shares sum to 1.0. Private-label
    # suppliers are excluded: Nomi SKUs are sourced separately and would
    # otherwise double-count every category.
    by_category: dict[str, dict[str, float]] = {}
    private_label_categories: set[str] = set()
    for s in suppliers:
        for cat in s["category_share"]:
            if cat not in category_names:
                raise ConfigError(f"{s['supplier_id']} supplies unknown category '{cat}'")
        if s.get("private_label_only"):
            private_label_categories |= set(s["category_share"])
            continue
        for cat, share in s["category_share"].items():
            by_category.setdefault(cat, {})[s["supplier_id"]] = share

    uncovered = category_names - set(by_category)
    if uncovered:
        raise ConfigError(f"categories with no branded supplier: {sorted(uncovered)}")
    for cat, shares in by_category.items():
        _check_shares(shares, f"supplier category_share for '{cat}'")

    # a category with private-label SKUs but no private-label supplier would
    # silently produce Nomi products nobody manufactures
    missing_pl = {c.l1 for c in categories if c.private_label_share > 0} - private_label_categories
    if missing_pl:
        raise ConfigError(f"categories with private label but no PL supplier: {sorted(missing_pl)}")

    for s in suppliers:
        if not 0 < s["otif_rate"] <= 1:
            raise ConfigError(f"{s['supplier_id']} has otif_rate {s['otif_rate']}")
        mean, sd = s["inbound_freshness_pct"]
        if not 0 < mean <= 1 or sd < 0:
            raise ConfigError(f"{s['supplier_id']} has invalid inbound_freshness_pct")
        if s["lead_time_sd_days"] < 0 or s["lead_time_mean_days"] <= 0:
            raise ConfigError(f"{s['supplier_id']} has invalid lead time parameters")

    for shock in sup_doc.get("shocks") or []:
        affects = shock["affects_categories"]
        if affects != "all":
            unknown = set(affects) - category_names
            if unknown:
                raise ConfigError(f"shock '{shock['name']}' affects unknown {sorted(unknown)}")


# ----------------------------------------------------------------------- entry
def _validate_policies(doc: dict[str, Any]) -> None:
    if "baseline" not in doc:
        raise ConfigError("policies.yaml must define a 'baseline' policy")

    rep = doc["baseline"]["replenishment"]
    for key in ("review_period_days", "trailing_window_days", "assumed_lead_time_days"):
        if rep[key] <= 0:
            raise ConfigError(f"baseline replenishment '{key}' must be positive")
    if rep["safety_days"] < 0:
        raise ConfigError("baseline safety_days cannot be negative")
    if rep["order_multiple"] < 1 or rep["min_order_qty"] < 1:
        raise ConfigError("baseline order sizing must be at least one unit")

    ladder = doc["baseline"]["markdown"]["ladder"]
    if not ladder:
        raise ConfigError("baseline markdown ladder is empty")
    # deeper discounts must sit closer to expiry, or the ladder is upside down
    steps = sorted(ladder, key=lambda r: r["dte_max"])
    discounts = [r["discount"] for r in steps]
    if discounts != sorted(discounts, reverse=True):
        raise ConfigError(
            "baseline markdown ladder is not monotonic - discount must deepen as "
            "days to expiry fall"
        )
    for r in ladder:
        if not 0 < r["discount"] < 1:
            raise ConfigError(f"markdown discount {r['discount']} must be between 0 and 1")

    if doc["baseline"]["deal_slot"]["slots_per_store"] < 1:
        raise ConfigError("baseline must run at least one deal slot")


def load_sim_config(config_dir: Path = CONFIG_DIR) -> SimConfig:
    """Load all five config files and validate them against each other."""
    global CONFIG_DIR  # noqa: PLW0603 - lets tests point at a fixture directory
    previous, CONFIG_DIR = CONFIG_DIR, config_dir
    try:
        raw = {name: _read(name) for name in FILES}
    finally:
        CONFIG_DIR = previous

    calendar = raw["calendar"]
    _validate_calendar(calendar)

    hour_curves = set(calendar["hour_curves"])
    categories = _parse_categories(raw["catalog"], hour_curves)
    _validate_catalog(raw["catalog"], categories)

    names = {c.l1 for c in categories}
    _validate_festivals(calendar, names)
    _validate_stores(raw["stores"]["stores"], names)
    _validate_segments(raw["segments"], names)
    _validate_suppliers(raw["suppliers"], names, categories)

    _validate_policies(raw["policies"])

    return SimConfig(
        stores=raw["stores"]["stores"],
        categories=categories,
        calendar=calendar,
        segments=raw["segments"]["segments"],
        suppliers=raw["suppliers"]["suppliers"],
        policies=raw["policies"],
        raw=raw,
    )


if __name__ == "__main__":
    cfg = load_sim_config()
    start, end = cfg.window
    print(f"stores            {len(cfg.stores)}")
    print(f"categories        {len(cfg.categories)}")
    print(f"subcategories     {len(cfg.subcategories)}")
    print(f"SKUs              {cfg.total_sku_count}")
    print(f"  perishable      {cfg.perishable_sku_count} (shelf life <= 14 days)")
    print(f"  private label   ~{cfg.expected_private_label_count}")
    print(f"segments          {len(cfg.segments)}")
    print(f"suppliers         {len(cfg.suppliers)}")
    print(f"window            {start} to {end}  ({cfg.window_days} days)")
    print(f"festivals         {len(cfg.calendar['festivals'])}")
    print()
    for c in cfg.categories:
        peri = sum(s.sku_count for s in c.subcategories if s.is_perishable)
        print(
            f"  {c.l1:<26} {c.sku_count:>4} SKUs  "
            f"weight {c.demand_weight:>5.3f}  elasticity {c.elasticity:>6.2f}  "
            f"perishable {peri:>3}"
        )
