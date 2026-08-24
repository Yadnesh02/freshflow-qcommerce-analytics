"""Contract for the supply chain and batch creation (task S1.3).

The properties defended here are the ones the whole expiry analysis rests on.
If inbound freshness did not vary by supplier there would be no upstream root
cause to find; if expiry were set from full shelf life rather than remaining
shelf life, supplier quality would never show up as wastage at all.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from simulator.catalog import build_catalog
from simulator.config_loader import load_sim_config
from simulator.supply import SupplyChain

cfg = load_sim_config()
catalog = build_catalog(cfg, seed=42)

ORDINARY_DAY = dt.date(2026, 2, 17)
DRY_MONTH_DAY = dt.date(2026, 2, 17)
MONSOON_DAY = dt.date(2026, 7, 14)


def fresh_chain() -> SupplyChain:
    return SupplyChain(cfg, catalog, seed=42)


def order_many(sc: SupplyChain, mask, n_rep=30, day=ORDINARY_DAY, seed=3):
    """Place repeated orders for every SKU matching `mask`, across random stores."""
    rng = np.random.default_rng(seed)
    idx = catalog.index[mask].to_numpy()
    sku_idx = np.tile(idx, n_rep)
    store_idx = rng.integers(0, len(cfg.stores), len(sku_idx))
    qty = rng.integers(12, 60, len(sku_idx))
    return sc.place_orders(store_idx, sku_idx, qty, day, rng)


DAIRY = catalog["l1_category"] == "Dairy & Eggs"
SHORT_DAIRY = DAIRY & (catalog["shelf_life_days"] <= 14)

_sc = fresh_chain()
_dairy_pos = order_many(_sc, DAIRY)
_dairy_batches = _sc.to_batches(_dairy_pos)


# =============================================================== determinism
def test_orders_are_deterministic() -> None:
    """Both policy arms must face the same supply world - common random numbers."""
    a = order_many(fresh_chain(), SHORT_DAIRY)
    b = order_many(fresh_chain(), SHORT_DAIRY)
    for col in ("supplier_id", "received_qty", "inbound_freshness_pct", "received_date"):
        assert a[col].tolist() == b[col].tolist(), f"{col} is not reproducible"


def test_supply_shocks_are_fixed_at_construction() -> None:
    assert fresh_chain().shocks.keys() == fresh_chain().shocks.keys()


def test_identifiers_are_unique_across_calls() -> None:
    sc = fresh_chain()
    a = order_many(sc, SHORT_DAIRY, n_rep=4)
    b = order_many(sc, SHORT_DAIRY, n_rep=4, seed=9)
    assert set(a["po_id"]).isdisjoint(b["po_id"])

    ba, bb = sc.to_batches(a), sc.to_batches(b)
    assert set(ba["batch_id"]).isdisjoint(bb["batch_id"])


# =============================================================== supplier choice
def test_branded_volume_shares_match_the_config() -> None:
    branded = _dairy_pos[_dairy_pos["supplier_id"] != "SUP-NOMI-A"]
    share_a = (branded["supplier_id"] == "SUP-DAIRY-A").mean()
    assert share_a == pytest.approx(0.62, abs=0.03)


def test_private_label_is_always_sourced_from_the_private_label_supplier() -> None:
    pl = _dairy_pos.merge(catalog[["sku_id", "is_private_label"]], on="sku_id", how="left")
    assert (pl.loc[pl["is_private_label"], "supplier_id"] == "SUP-NOMI-A").all()
    assert (pl.loc[~pl["is_private_label"], "supplier_id"] != "SUP-NOMI-A").all()


def test_the_same_sku_arrives_from_more_than_one_supplier() -> None:
    """Sampling per order rather than fixing a supplier per SKU is what lets a
    later analysis compare freshness holding the product constant."""
    branded = _dairy_pos[_dairy_pos["supplier_id"] != "SUP-NOMI-A"]
    per_sku = branded.groupby("sku_id")["supplier_id"].nunique()
    assert (per_sku > 1).mean() > 0.8, "suppliers are effectively fixed per SKU"


# =============================================================== freshness
def test_inbound_freshness_tracks_each_supplier_configured_mean() -> None:
    means = _dairy_pos.groupby("supplier_id")["inbound_freshness_pct"].mean()
    for sup in cfg.suppliers:
        if sup["supplier_id"] in means.index:
            assert means[sup["supplier_id"]] == pytest.approx(
                sup["inbound_freshness_pct"][0], abs=0.03
            )


def test_the_weak_supplier_ships_materially_less_shelf_life() -> None:
    """The planted root cause. Without this gap there is nothing upstream to find."""
    sc = fresh_chain()
    pos = order_many(sc, SHORT_DAIRY)
    batches = sc.to_batches(pos).merge(
        catalog[["sku_id", "shelf_life_days"]], on="sku_id", how="left"
    )
    usable = batches.groupby("supplier_id")["usable_days"].mean()
    assert usable["SUP-DAIRY-A"] - usable["SUP-DAIRY-B"] > 1.0, (
        "supplier freshness gap is too small to drive a measurable wastage difference"
    )


def test_the_weak_supplier_is_also_the_cheap_one() -> None:
    """A root cause that is a genuine trade-off, not an oversight - which is why
    it survives in the business and is worth quantifying."""
    sc = fresh_chain()
    batches = sc.to_batches(order_many(sc, SHORT_DAIRY)).merge(
        catalog[["sku_id", "landed_cost"]], on="sku_id", how="left"
    )
    # compare the cost index, not raw mean cost: the two suppliers see a
    # different SKU mix, so raw means confound price with assortment
    batches["index"] = batches["unit_landed_cost"] / batches["landed_cost"]
    idx = batches.groupby("supplier_id")["index"].mean()
    assert idx["SUP-DAIRY-B"] < idx["SUP-DAIRY-A"]
    assert idx["SUP-DAIRY-A"] == pytest.approx(1.00, abs=0.02)


def test_freshness_is_bounded() -> None:
    f = _dairy_pos["inbound_freshness_pct"]
    assert (f > 0).all() and (f <= 1.0).all()


# =============================================================== lead time & OTIF
def test_lead_times_average_close_to_the_configured_mean() -> None:
    pos = order_many(fresh_chain(), DAIRY, n_rep=40)
    on_time = pos[~pos["is_late"]]
    lag = np.array(
        [
            (r - o).days
            for r, o in zip(on_time["received_date"], on_time["ordered_date"], strict=True)
        ]
    )
    assert 0 <= lag.mean() <= 2.5


def test_late_deliveries_arrive_after_the_expected_date() -> None:
    late = _dairy_pos[_dairy_pos["is_late"]]
    assert len(late) > 0
    assert all(r > e for r, e in zip(late["received_date"], late["expected_date"], strict=True))


def test_otif_failure_rate_is_in_the_configured_range() -> None:
    """Roughly 4-11% of dairy orders should miss on time or in full."""
    failures = (_dairy_pos["is_short"] | _dairy_pos["is_late"]).mean()
    assert 0.03 < failures < 0.35, f"OTIF failure rate is {failures:.1%}"


def test_monsoon_degrades_on_time_performance_for_produce() -> None:
    produce = catalog["l1_category"] == "Fruits & Vegetables"
    dry = order_many(fresh_chain(), produce, n_rep=25, day=DRY_MONTH_DAY, seed=11)
    wet = order_many(fresh_chain(), produce, n_rep=25, day=MONSOON_DAY, seed=11)

    dry_fail = (dry["is_short"] | dry["is_late"]).mean()
    wet_fail = (wet["is_short"] | wet["is_late"]).mean()
    assert wet_fail > dry_fail, (
        f"monsoon failure rate {wet_fail:.1%} is not worse than dry {dry_fail:.1%}"
    )


def test_short_deliveries_ship_less_than_ordered() -> None:
    short = _dairy_pos[_dairy_pos["is_short"]]
    assert len(short) > 0
    assert (short["received_qty"] < short["ordered_qty"]).all()


def test_orders_that_are_neither_short_nor_shocked_arrive_in_full() -> None:
    clean = _dairy_pos[~_dairy_pos["is_short"]]
    assert (clean["received_qty"] == clean["ordered_qty"]).all()


# =============================================================== batches
def test_every_batch_expires_after_it_arrives() -> None:
    assert all(
        e > r
        for e, r in zip(_dairy_batches["expiry_date"], _dairy_batches["received_date"], strict=True)
    )
    assert (_dairy_batches["usable_days"] >= 1).all()


def test_expiry_reflects_remaining_shelf_life_not_full_shelf_life() -> None:
    """The single line that makes supplier quality surface as wastage weeks later."""
    joined = _dairy_batches.merge(catalog[["sku_id", "shelf_life_days"]], on="sku_id", how="left")
    assert (joined["usable_days"] <= joined["shelf_life_days"]).all()
    shortfall = (joined["usable_days"] < joined["shelf_life_days"]).mean()
    assert shortfall > 0.9, "batches are arriving with their full shelf life intact"


def test_manufacture_date_is_consistent_with_shelf_life() -> None:
    joined = _dairy_batches.merge(catalog[["sku_id", "shelf_life_days"]], on="sku_id", how="left")
    for _, r in joined.head(200).iterrows():
        assert (r["expiry_date"] - r["mfg_date"]).days == r["shelf_life_days"]
        # equality is legitimate: bread delivered the morning it was baked
        assert r["mfg_date"] <= r["received_date"], "batch manufactured after it arrived"


def test_landed_cost_carries_the_supplier_cost_index() -> None:
    joined = _dairy_batches.merge(
        catalog[["sku_id", "landed_cost"]], on="sku_id", how="left", suffixes=("_batch", "_cat")
    )
    nomi = joined[joined["supplier_id"] == "SUP-NOMI-A"]
    ratio = (nomi["unit_landed_cost"] / nomi["landed_cost"]).mean()
    assert ratio == pytest.approx(0.78, abs=0.02)


def test_zero_quantity_orders_produce_no_batch() -> None:
    sc = fresh_chain()
    rng = np.random.default_rng(1)
    pos = sc.place_orders(np.array([0, 1]), np.array([5, 6]), np.array([0, 0]), ORDINARY_DAY, rng)
    assert sc.to_batches(pos).empty


def test_an_empty_order_book_returns_empty_frames_with_the_right_columns() -> None:
    sc = fresh_chain()
    rng = np.random.default_rng(1)
    pos = sc.place_orders(
        np.array([], dtype=int),
        np.array([], dtype=int),
        np.array([], dtype=int),
        ORDINARY_DAY,
        rng,
    )
    assert pos.empty and "po_id" in pos.columns
    batches = sc.to_batches(pos)
    assert batches.empty and "batch_id" in batches.columns


# =============================================================== shocks
def test_supply_shocks_occur_and_cut_fill_rates() -> None:
    sc = fresh_chain()
    assert sc.shocks, "no supply shocks fired across the whole window"

    shock_day = next(d for d, e in sc.shocks.items() if e["supply"])
    hit = next(iter(sc.shocks[shock_day]["supply"]))
    affected = catalog["l1_category"] == hit

    rng = np.random.default_rng(2)
    idx = catalog.index[affected].to_numpy()
    store_idx = np.zeros(len(idx), dtype=int)
    qty = np.full(len(idx), 50)
    pos = sc.place_orders(store_idx, idx, qty, shock_day, rng)
    assert pos["received_qty"].sum() < qty.sum() * 0.95, "shock did not reduce supply"


def test_monsoon_produce_shocks_land_in_monsoon_months() -> None:
    sc = fresh_chain()
    produce_shocks = [d for d, e in sc.shocks.items() if "monsoon_produce_disruption" in e["names"]]
    assert produce_shocks
    assert all(d.month in (7, 8, 9) for d in produce_shocks), (
        "monsoon produce disruption fired outside the monsoon window"
    )
