"""Contract for the simulator configuration (task S1.1).

The configs are the vocabulary the whole simulated world is written in. A
demand_weight that does not sum to 1.0, or a supplier share that leaves a
category 42% unsupplied, would not crash anything - it would quietly distort
every downstream number. So the shares are asserted, not assumed.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest
import yaml

from simulator.config_loader import CONFIG_DIR, ConfigError, load_sim_config

cfg = load_sim_config()

STORES = cfg.stores
SEGMENTS = cfg.segments
SUPPLIERS = cfg.suppliers
SUBCATEGORIES = cfg.subcategories


# ------------------------------------------------------------------ shape
def test_network_shape_matches_the_plan() -> None:
    assert len(cfg.stores) == 14
    assert cfg.total_sku_count == 1500
    assert len(cfg.categories) == 12
    assert len(cfg.segments) == 5
    assert cfg.window_days == 365


def test_perishable_and_private_label_counts_are_plausible() -> None:
    """The project is about perishables, so a thin perishable tail would gut it."""
    assert 280 <= cfg.perishable_sku_count <= 420, (
        f"{cfg.perishable_sku_count} perishable SKUs - the expiry problem needs real mass"
    )
    assert 100 <= cfg.expected_private_label_count <= 160


def test_every_category_has_at_least_one_subcategory() -> None:
    for c in cfg.categories:
        assert c.subcategories, f"category '{c.l1}' has no subcategories"


# ------------------------------------------------------------------ shares
def test_category_demand_weights_sum_to_one() -> None:
    assert sum(c.demand_weight for c in cfg.categories) == pytest.approx(1.0, abs=1e-9)


def test_segment_shares_sum_to_one() -> None:
    assert sum(s["share"] for s in SEGMENTS) == pytest.approx(1.0, abs=1e-9)


def test_branded_supplier_shares_sum_to_one_per_category() -> None:
    totals: dict[str, float] = {}
    for s in SUPPLIERS:
        if s.get("private_label_only"):
            continue
        for cat, share in s["category_share"].items():
            totals[cat] = totals.get(cat, 0.0) + share
    assert set(totals) == cfg.category_names, "some category has no branded supplier"
    for cat, total in totals.items():
        assert total == pytest.approx(1.0, abs=1e-9), f"{cat} branded share sums to {total}"


def test_substitution_shares_sum_to_one() -> None:
    sub = cfg.raw["catalog"]["substitution"]
    base = {k: sub[k] for k in ("within_subcategory", "to_private_label", "lost")}
    assert sum(base.values()) == pytest.approx(1.0, abs=1e-9)
    for cat, override in sub["overrides"].items():
        assert sum(override.values()) == pytest.approx(1.0, abs=1e-9), cat


# ------------------------------------------------------------------ calendar
@pytest.mark.parametrize("name", ["morning", "evening", "flat"])
def test_hour_curves_are_valid_distributions(name: str) -> None:
    curve = cfg.calendar["hour_curves"][name]
    assert len(curve) == 24
    assert sum(curve) == pytest.approx(1.0, abs=1e-3)
    assert all(v >= 0 for v in curve)


def test_hour_curves_encode_genuinely_different_peaks() -> None:
    """If morning and evening peaked at the same hour, hourly modelling is pointless."""
    morning = cfg.calendar["hour_curves"]["morning"]
    evening = cfg.calendar["hour_curves"]["evening"]
    assert morning.index(max(morning)) < 12, "morning curve does not peak in the morning"
    assert evening.index(max(evening)) >= 18, "evening curve does not peak in the evening"


def test_every_festival_overlaps_the_simulation_window() -> None:
    start, end = cfg.window
    for f in cfg.calendar["festivals"]:
        span_start = f["date"] - dt.timedelta(days=f["lead_days"])
        span_end = f["date"] + dt.timedelta(days=f["duration_days"] - 1)
        assert span_end >= start and span_start <= end, f"{f['name']} falls outside the window"


def test_navratri_suppresses_non_vegetarian_demand() -> None:
    """A festival model that only ever multiplies upward is not modelling behaviour."""
    navratri = next(f for f in cfg.calendar["festivals"] if f["name"] == "Navratri")
    assert navratri["categories"]["Meat, Fish & Seafood"] < 0.5
    assert navratri["categories"]["Staples & Packaged Food"] > 1.5


def test_monsoon_raises_demand_while_cutting_fresh_supply() -> None:
    """The squeeze that makes availability hard: more orders, less produce."""
    monsoon = cfg.calendar["monsoon"]
    assert monsoon["order_volume_factor"] > 1.0
    assert monsoon["fresh_supply_factor"] < 1.0


# ------------------------------------------------------------------ stores
def test_store_ids_are_unique() -> None:
    ids = [s["store_id"] for s in STORES]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("store", STORES, ids=[s["store_id"] for s in STORES])
def test_store_is_well_formed(store: dict) -> None:
    assert store["demand_index"] > 0
    assert store["catchment_tier"] in {"premium", "mid", "mass"}
    assert 18.8 < store["lat"] < 19.4, "outside the Mumbai metropolitan region"
    assert 72.7 < store["lon"] < 73.1
    assert len(store["pincode"]) == 6
    for cat in store.get("category_affinity") or {}:
        assert cat in cfg.category_names


def test_one_store_opens_mid_window() -> None:
    """A network where every store has a clean 365-day history is unrealistically easy."""
    start, end = cfg.window
    mid_window = [s for s in STORES if start < s["opened_date"] <= end]
    assert mid_window, "no store opens during the window - forecasting never sees a cold start"


# ------------------------------------------------------------------ catalogue
@pytest.mark.parametrize("sub", SUBCATEGORIES, ids=[f"{s.l1}/{s.name}" for s in SUBCATEGORIES])
def test_subcategory_ranges_are_ordered(sub) -> None:
    assert sub.shelf_life_days[0] < sub.shelf_life_days[1]
    assert sub.price[0] < sub.price[1]
    assert sub.margin[0] < sub.margin[1]
    assert 0 < sub.margin[1] < 1, "margin is a fraction, not a percentage"
    assert sub.shelf_life_days[0] >= 1


def test_elasticity_ordering_matches_retail_intuition() -> None:
    """Staples are inelastic, discretionary snacks are not. If this inverts, the
    markdown optimiser will learn the wrong lesson from the generated data."""
    staples = cfg.category("Staples & Packaged Food").elasticity
    snacks = cfg.category("Snacks & Namkeen").elasticity
    dairy = cfg.category("Dairy & Eggs").elasticity
    assert snacks < dairy < staples < 0


def test_freshness_acceptance_falls_as_shelf_life_runs_out() -> None:
    curve = cfg.raw["catalog"]["freshness_acceptance"]["curve"]
    fractions = sorted(curve, reverse=True)
    values = [curve[f] for f in fractions]
    assert values == sorted(values, reverse=True)
    assert values[0] == pytest.approx(1.0)
    assert values[-1] < 0.35, "near-expiry stock should be hard to shift even at a discount"


def test_sku_popularity_is_concentrated_like_real_retail() -> None:
    """Zipf, not uniform: a handful of SKUs carry most of the volume.

    This is what makes ABC/XYZ classification meaningful and the long tail hard
    to forecast. A uniform catalogue would make the forecasting work trivial and
    the project dishonest.
    """
    pop = cfg.raw["catalog"]["popularity"]
    assert pop["distribution"] == "zipf"

    n = cfg.total_sku_count
    weights = 1.0 / np.arange(1, n + 1) ** pop["alpha"]
    weights /= weights.sum()
    top_decile_share = weights[: n // 10].sum()

    assert 0.35 < top_decile_share < 0.80, (
        f"top 10% of SKUs carry {top_decile_share:.1%} of demand - "
        "outside the range that looks like real grocery retail"
    )


# ------------------------------------------------------------------ segments
@pytest.mark.parametrize("seg", SEGMENTS, ids=[s["name"] for s in SEGMENTS])
def test_segment_is_well_formed(seg: dict) -> None:
    assert 0 < seg["share"] < 1
    assert seg["orders_per_month"][0] < seg["orders_per_month"][1]
    assert seg["basket_items"][0] < seg["basket_items"][1]
    assert seg["elasticity_multiplier"] > 0
    for cat in seg.get("category_bias") or {}:
        assert cat in cfg.category_names


def test_segments_are_behaviourally_distinct() -> None:
    """Five segments that behave alike is one segment with extra steps."""
    by_name = {s["name"]: s for s in SEGMENTS}
    assert by_name["deal_hunter"]["elasticity_multiplier"] > 2.0
    assert by_name["premium"]["elasticity_multiplier"] < 0.6
    assert (
        by_name["convenience"]["orders_per_month"][1]
        > by_name["bulk_planner"]["orders_per_month"][1]
    )
    assert (
        by_name["bulk_planner"]["basket_value_index"] > by_name["convenience"]["basket_value_index"]
    )


def test_churn_hazard_responds_to_operational_failures() -> None:
    """The mechanism that lets better inventory decisions *cause* retention.

    Without it, any retention lift in the experiment would be asserted rather
    than measured - see plan section 10.
    """
    churn = cfg.raw["segments"]["churn"]
    assert churn["worsened_by"]["stockout_on_favourite_sku"] > 1.0
    assert churn["worsened_by"]["received_low_dte_item"] > 1.0
    assert churn["improved_by"]["consistent_availability_30d"] < 1.0
    assert all(0 < h < 1 for h in churn["base_monthly_hazard"].values())


def test_deal_hunters_churn_fastest() -> None:
    """The segment that makes problem P7 worth solving: high GMV, low loyalty."""
    hazards = cfg.raw["segments"]["churn"]["base_monthly_hazard"]
    assert hazards["deal_hunter"] == max(hazards.values())


# ------------------------------------------------------------------ suppliers
@pytest.mark.parametrize("sup", SUPPLIERS, ids=[s["supplier_id"] for s in SUPPLIERS])
def test_supplier_is_well_formed(sup: dict) -> None:
    assert 0 < sup["otif_rate"] <= 1
    assert sup["lead_time_mean_days"] > 0
    assert sup["lead_time_sd_days"] >= 0
    mean, sd = sup["inbound_freshness_pct"]
    assert 0 < mean <= 1
    assert 0 <= sd < 0.5
    for cat in sup["category_share"]:
        assert cat in cfg.category_names


def test_inbound_freshness_varies_enough_to_be_a_finding() -> None:
    """Supplier quality has to differ, or 'which supplier drives our wastage?'
    has no answer and an entire root-cause analysis disappears."""
    dairy = [
        s
        for s in SUPPLIERS
        if "Dairy & Eggs" in s["category_share"] and not s.get("private_label_only")
    ]
    freshness = [s["inbound_freshness_pct"][0] for s in dairy]
    assert max(freshness) - min(freshness) > 0.15, (
        "dairy suppliers ship near-identical freshness - no root cause to find"
    )


def test_private_label_is_cheaper_to_source() -> None:
    """The commercial reason problem P5 exists."""
    nomi = next(s for s in SUPPLIERS if s.get("private_label_only"))
    assert nomi["cost_index"] < 0.85


# ------------------------------------------------------------------ negative
def _write_broken(tmp_path, filename: str, mutate) -> None:
    """Copy the real configs to tmp_path, then corrupt one of them."""
    for name in ("stores", "catalog", "calendar", "segments", "suppliers", "policies"):
        doc = yaml.safe_load((CONFIG_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
        if name == filename:
            mutate(doc)
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_rejects_demand_weights_that_do_not_sum_to_one(tmp_path) -> None:
    _write_broken(tmp_path, "catalog", lambda d: d["categories"][0].update(demand_weight=0.9))
    with pytest.raises(ConfigError, match="demand_weight sums to"):
        load_sim_config(config_dir=tmp_path)


def test_rejects_a_festival_outside_the_window(tmp_path) -> None:
    def mutate(d):
        d["festivals"][0]["date"] = dt.date(2030, 1, 1)

    _write_broken(tmp_path, "calendar", mutate)
    with pytest.raises(ConfigError, match="outside the simulation window"):
        load_sim_config(config_dir=tmp_path)


def test_rejects_a_category_left_partly_unsupplied(tmp_path) -> None:
    def mutate(d):
        d["suppliers"][0]["category_share"]["Dairy & Eggs"] = 0.20

    _write_broken(tmp_path, "suppliers", mutate)
    with pytest.raises(ConfigError, match="category_share for 'Dairy & Eggs'"):
        load_sim_config(config_dir=tmp_path)


def test_rejects_positive_elasticity(tmp_path) -> None:
    _write_broken(tmp_path, "catalog", lambda d: d["categories"][0].update(elasticity=0.4))
    with pytest.raises(ConfigError, match="expected negative"):
        load_sim_config(config_dir=tmp_path)


def test_rejects_a_store_biased_toward_an_unknown_category(tmp_path) -> None:
    def mutate(d):
        d["stores"][0]["category_affinity"] = {"Fine Wines": 1.5}

    _write_broken(tmp_path, "stores", mutate)
    with pytest.raises(ConfigError, match="unknown category 'Fine Wines'"):
        load_sim_config(config_dir=tmp_path)
