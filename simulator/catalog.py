"""Expand the catalogue templates into a concrete SKU master (task S1.2).

`catalog.yaml` describes 58 subcategory templates; this turns them into 1,500
individual SKUs deterministically from a seed. Same seed, same catalogue -
which the policy A/B backtest depends on, since both arms must shop the same
shelves.

    from simulator.catalog import build_catalog
    skus = build_catalog(cfg, seed=42)

GROUND_TRUTH_COLUMNS never leave the simulator. They are the parameters the
analytics layer is supposed to *infer* - if they were emitted to the raw feed,
the elasticity estimation and ABC classification would be reading the answer
key instead of doing the work.
"""

from __future__ import annotations

import zlib

import numpy as np
import pandas as pd

from simulator.config_loader import SimConfig, Subcategory

# columns the simulator uses internally and must strip before emitting Bronze
GROUND_TRUTH_COLUMNS = ("popularity_weight", "elasticity", "hour_curve")

# Fictional brands. Deliberately not real Indian FMCG names - the project must
# not imply a relationship with any actual company.
BRAND_POOLS: dict[str, tuple[str, ...]] = {
    "Dairy & Eggs": ("Konkan Gold", "Sahyadri Fresh", "Milkmoor", "Ghatvale", "Pure Meadow"),
    "Bakery": ("Mahim Bakehouse", "Crustly", "Golden Loaf", "Bandra Bake Co"),
    "Fruits & Vegetables": ("Farmlane", "Harvest Direct", "Green Basket", "Nashik Valley"),
    "Meat, Fish & Seafood": ("Coastline", "Prime Cut", "Harbour Fresh", "Deonar Select"),
    "Snacks & Namkeen": ("Crunchvale", "Namkeen Nagar", "Chatpata Co", "Munchbox", "Golden Bite"),
    "Beverages": ("Fizzwell", "Orchard Press", "Chaiwala Co", "Aquapure", "Boltz"),
    "Staples & Packaged Food": ("Annapurna Mills", "Sarva Grains", "Sunfield", "Masalakart"),
    "Ready to Eat & Frozen": ("Coldstar", "QuickPlate", "Frostbite", "Tiffin Express"),
    "Personal Care": ("Velure", "Herbaline", "Purelle", "Nivara", "Glowset"),
    "Household & Cleaning": ("Shinekart", "Sparkle Pro", "Homeguard", "Freshnest"),
    "Baby & Pet Care": ("Tinytots", "Cuddleco", "Pawfect", "Nurture"),
    "Health & Wellness": ("Vitalis", "Ayurmed", "Wellcore", "Medikit"),
}

# Pack size options by category, so a SKU name reads like a real shelf label
PACK_SIZES: dict[str, tuple[tuple[str, str], ...]] = {
    "Dairy & Eggs": (("200", "ml"), ("500", "ml"), ("1", "L"), ("100", "g"), ("400", "g")),
    "Bakery": (("200", "g"), ("400", "g"), ("6", "pc"), ("12", "pc")),
    "Fruits & Vegetables": (("250", "g"), ("500", "g"), ("1", "kg"), ("6", "pc")),
    "Meat, Fish & Seafood": (("250", "g"), ("450", "g"), ("500", "g"), ("1", "kg")),
    "Snacks & Namkeen": (("30", "g"), ("60", "g"), ("150", "g"), ("400", "g")),
    "Beverages": (("250", "ml"), ("600", "ml"), ("1", "L"), ("2", "L")),
    "Staples & Packaged Food": (("100", "g"), ("500", "g"), ("1", "kg"), ("5", "kg")),
    "Ready to Eat & Frozen": (("200", "g"), ("400", "g"), ("750", "g")),
    "Personal Care": (("75", "ml"), ("100", "ml"), ("200", "ml"), ("400", "ml")),
    "Household & Cleaning": (("500", "ml"), ("1", "L"), ("1", "kg"), ("2", "kg")),
    "Baby & Pet Care": (("200", "g"), ("400", "g"), ("1", "kg"), ("24", "pc")),
    "Health & Wellness": (("30", "pc"), ("60", "pc"), ("100", "ml"), ("200", "ml")),
}

VARIANTS: tuple[str, ...] = (
    "Classic",
    "Premium",
    "Value",
    "Select",
    "Daily",
    "Gold",
    "Fresh",
    "Original",
    "Special",
    "Everyday",
    "Pro",
    "Natural",
    "Choice",
    "Reserve",
)

# the deal rail only works on items cheap enough for a Rs 11 price point to be
# a discount rather than a giveaway
DEAL_ELIGIBLE_MAX_PRICE = 120.0

# how strongly popularity pulls price down. 0 = price independent of demand,
# 1 = perfectly rank-correlated. A middling value creates the price/demand
# confounding that makes elasticity estimation an actual statistical problem
# rather than a lookup.
PRICE_POPULARITY_COUPLING = 0.60


def _child_rng(seed: int, key: str) -> np.random.Generator:
    """Stable per-subcategory RNG, so adding a category does not reshuffle others."""
    return np.random.default_rng([seed, zlib.crc32(key.encode())])


def _zipf_weights(n: int, alpha: float) -> np.ndarray:
    w = 1.0 / np.arange(1, n + 1) ** alpha
    return w / w.sum()


def _round_price(x: float) -> float:
    """Retail prices cluster on round numbers, not two decimal places."""
    if x < 50:
        return float(round(x))
    if x < 200:
        return float(round(x / 5) * 5)
    return float(round(x / 10) * 10)


def _build_subcategory(
    sub: Subcategory,
    category_weight_share: float,
    cat_meta: dict,
    alpha: float,
    pl_cfg: dict,
    gst: float,
    seed: int,
    start_index: int,
) -> list[dict]:
    rng = _child_rng(seed, f"{sub.l1}/{sub.name}")
    n = sub.sku_count

    popularity = _zipf_weights(n, alpha) * category_weight_share
    # rank 0 is the most popular SKU
    rank_u = (np.arange(n) + 0.5) / n
    noise = rng.random(n)
    price_u = np.clip(
        PRICE_POPULARITY_COUPLING * rank_u + (1 - PRICE_POPULARITY_COUPLING) * noise, 0.0, 1.0
    )

    lo, hi = sub.price
    prices = lo * (hi / lo) ** price_u  # log-uniform, cheaper where more popular
    margins = rng.uniform(sub.margin[0], sub.margin[1], n)
    shelf = rng.integers(sub.shelf_life_days[0], sub.shelf_life_days[1] + 1, n)

    brands = BRAND_POOLS[sub.l1]
    packs = PACK_SIZES[sub.l1]

    # private label SKUs are spread across the popularity distribution rather
    # than dumped in the tail - a PL range that only contains unpopular items
    # could never take share
    n_private = int(round(n * cat_meta["private_label_share"]))
    private_idx = set(rng.choice(n, size=n_private, replace=False).tolist()) if n_private else set()

    rows: list[dict] = []
    for i in range(n):
        is_pl = i in private_idx
        pack_qty, uom = packs[rng.integers(len(packs))]
        variant = VARIANTS[rng.integers(len(VARIANTS))]
        brand = pl_cfg["brand_name"] if is_pl else brands[rng.integers(len(brands))]

        price = float(prices[i])
        margin = float(margins[i])
        if is_pl:
            price *= 1 - pl_cfg["price_discount_vs_brand"]
            margin = min(margin + pl_cfg["margin_uplift_pct"], 0.62)

        base_price = _round_price(price)
        landed_cost = round(base_price * (1 - margin), 2)
        mrp = _round_price(base_price * rng.uniform(1.02, 1.28))

        rows.append(
            {
                "sku_id": f"SKU-{start_index + i:05d}",
                "sku_name": f"{brand} {sub.name} {variant} {pack_qty} {uom}",
                "brand": brand,
                "is_private_label": is_pl,
                "l1_category": sub.l1,
                "l2_subcategory": sub.name,
                "temp_zone": cat_meta["temp_zone"],
                "pack_qty": float(pack_qty),
                "uom": uom,
                "shelf_life_days": int(shelf[i]),
                "mrp": max(mrp, base_price),
                "base_price": base_price,
                "landed_cost": landed_cost,
                "gst_rate": gst,
                "deal_eligible": bool(base_price <= DEAL_ELIGIBLE_MAX_PRICE),
                "popularity_weight": float(popularity[i]),
                "elasticity": cat_meta["elasticity"],
                "hour_curve": sub.hour_curve,
            }
        )
    return rows


def build_catalog(cfg: SimConfig, seed: int = 42) -> pd.DataFrame:
    """Expand the templates into the full SKU master. Deterministic in `seed`."""
    alpha = cfg.raw["catalog"]["popularity"]["alpha"]
    pl_cfg = cfg.raw["catalog"]["private_label"]
    gst_by_cat = cfg.raw["catalog"]["gst_rate_by_category"]

    rows: list[dict] = []
    index = 1
    for cat in cfg.categories:
        cat_meta = {
            "temp_zone": cat.temp_zone,
            "elasticity": cat.elasticity,
            "private_label_share": cat.private_label_share,
        }
        # a subcategory's slice of its category's demand is proportional to how
        # much shelf it occupies
        for sub in cat.subcategories:
            share = cat.demand_weight * (sub.sku_count / cat.sku_count)
            rows.extend(
                _build_subcategory(
                    sub, share, cat_meta, alpha, pl_cfg, gst_by_cat[cat.l1], seed, index
                )
            )
            index += sub.sku_count

    df = pd.DataFrame(rows)
    # popularity is a probability distribution over the whole catalogue
    df["popularity_weight"] /= df["popularity_weight"].sum()
    return df


def to_bronze(catalog: pd.DataFrame) -> pd.DataFrame:
    """Strip generator ground truth before the catalogue is emitted as source data."""
    return catalog.drop(columns=list(GROUND_TRUTH_COLUMNS), errors="ignore")


if __name__ == "__main__":
    from simulator.config_loader import load_sim_config

    cfg = load_sim_config()
    skus = build_catalog(cfg)

    pd.set_option("display.width", 160)
    print(f"SKUs: {len(skus)}   private label: {int(skus['is_private_label'].sum())}")
    print(f"deal eligible: {int(skus['deal_eligible'].sum())}")
    print(f"perishable (<=14d shelf life): {int((skus['shelf_life_days'] <= 14).sum())}\n")

    top = skus.nlargest(8, "popularity_weight")[
        ["sku_id", "sku_name", "base_price", "landed_cost", "shelf_life_days", "popularity_weight"]
    ]
    print("most popular SKUs:")
    print(top.to_string(index=False))

    cum = skus["popularity_weight"].sort_values(ascending=False).cumsum()
    n = len(skus)
    print(
        f"\nconcentration: top 10% of SKUs = {cum.iloc[n // 10 - 1]:.1%} of demand, "
        f"top 20% = {cum.iloc[n // 5 - 1]:.1%}"
    )
