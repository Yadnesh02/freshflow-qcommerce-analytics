"""Contract for the customer base, baskets and the churn hazard (task S1.4).

The churn assertions matter more than the rest. If churn did not respond to
stockouts and to receiving near-expiry stock, then better inventory management
could not *cause* retention, and the retention lift reported in the Sprint 5
experiment would be an artefact rather than a result.

The basket assertions defend a different invariant: segment preference decides
which customer buys a unit, never how many units a store sells. Getting that
backwards would double-count the store category affinity that the demand model
has already applied, and quietly break every category-mix number downstream.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from simulator.catalog import build_catalog
from simulator.config_loader import load_sim_config
from simulator.customers import BasketAssembler, CustomerBase
from simulator.demand import DemandModel

cfg = load_sim_config()
catalog = build_catalog(cfg, seed=42)

DAY = dt.date(2026, 2, 17)
base = CustomerBase(cfg, seed=42)
model = DemandModel(cfg, catalog, seed=42)


def assemble_day(day: dt.date = DAY, seed: int = 5):
    """Assemble a full network day and return orders joined to buyer segment."""
    import pandas as pd

    b = CustomerBase(cfg, seed=42)
    asm = BasketAssembler(cfg, catalog, b)
    rng = np.random.default_rng(seed)
    units = model.sample(model.daily_lambda(day), rng)
    by_store = b.active_by_store(day)

    orders, items = [], []
    for si in range(len(cfg.stores)):
        o, it = asm.assemble(si, units[si], day, by_store[si], rng)
        orders.append(o)
        items.append(it)
    orders = pd.concat(orders, ignore_index=True)
    items = pd.concat(items, ignore_index=True)
    orders["segment"] = [b.segment_names[i] for i in b._segment_idx[orders["customer_row"]]]
    return b, orders, items, units


_b, _orders, _items, _units = assemble_day()


# =============================================================== customer base
def test_customer_base_is_deterministic() -> None:
    again = CustomerBase(cfg, seed=42)
    assert again.df["customer_id"].tolist() == base.df["customer_id"].tolist()
    assert again.df["home_store_id"].tolist() == base.df["home_store_id"].tolist()
    assert again.df["_segment_idx"].tolist() == base.df["_segment_idx"].tolist()


def test_population_is_the_configured_size() -> None:
    acq = cfg.raw["segments"]["acquisition"]
    assert len(base.df) > acq["initial_customers"]
    assert 40_000 < len(base.df) < 60_000


def test_segment_mix_matches_the_config() -> None:
    mix = base.df["_segment_idx"].value_counts(normalize=True).sort_index()
    for i, seg in enumerate(cfg.segments):
        assert mix[i] == pytest.approx(seg["share"], abs=0.01)


def test_acquisition_channel_mix_matches_the_config() -> None:
    mix = base.df["acquisition_channel"].value_counts(normalize=True)
    for channel, share in cfg.raw["segments"]["acquisition"]["channel_mix"].items():
        assert mix[channel] == pytest.approx(share, abs=0.015)


def test_home_store_id_and_index_agree() -> None:
    """These were drawn twice from separate RNG calls and silently disagreed."""
    ids = np.array(base.store_ids)[base.df["_home_store_idx"].to_numpy()]
    assert (ids == base.df["home_store_id"].to_numpy()).all()


def test_nobody_is_homed_to_a_store_that_had_not_opened() -> None:
    opened = {s["store_id"]: s["opened_date"] for s in cfg.stores}
    late = base.df[base.df["home_store_id"] == "FF-GOR-01"]
    assert len(late) > 0, "the mid-window store acquired nobody"
    assert (late["signup_date"] >= opened["FF-GOR-01"]).all()


def test_the_newest_store_is_still_the_smallest() -> None:
    """It opened in January, so it cannot have the network's largest base."""
    counts = base.df["home_store_id"].value_counts()
    assert counts["FF-GOR-01"] < counts.median()


def test_nobody_is_active_before_they_sign_up() -> None:
    early = dt.date(2025, 9, 15)
    active = base.active_mask(early)
    assert (base.df.loc[active, "signup_date"] <= early).all()


def test_bronze_export_hides_the_latent_segment() -> None:
    bronze = base.to_bronze()
    assert not [c for c in bronze.columns if c.startswith("_")]
    assert "customer_id" in bronze.columns and "churn_date" in bronze.columns


# =============================================================== churn hazard
def test_a_quiet_month_gives_the_configured_base_hazard() -> None:
    b = CustomerBase(cfg, seed=42)
    h = b.hazard(DAY)
    for i, name in enumerate(b.segment_names):
        seg = h[b._segment_idx == i].mean()
        configured = cfg.raw["segments"]["churn"]["base_monthly_hazard"][name]
        # tenure protection pulls the realised hazard below the base rate
        assert 0.4 * configured < seg <= configured


def test_deal_hunters_churn_fastest_and_premium_slowest() -> None:
    b = CustomerBase(cfg, seed=42)
    h = b.hazard(DAY)
    by_seg = {n: h[b._segment_idx == i].mean() for i, n in enumerate(b.segment_names)}
    assert by_seg["deal_hunter"] == max(by_seg.values())
    assert by_seg["premium"] == min(by_seg.values())


@pytest.mark.parametrize("event", ["stockout", "low_dte", "late", "cancelled"])
def test_bad_experiences_raise_the_churn_hazard(event: str) -> None:
    """The causal path from inventory management to retention."""
    b = CustomerBase(cfg, seed=42)
    rows = np.arange(3000)
    before = b.hazard(DAY)[rows].mean()
    b.record(event, rows)
    after = b.hazard(DAY)[rows].mean()
    assert after > before * 1.02, f"'{event}' did not move the hazard"


def test_a_redeemed_deal_lowers_the_churn_hazard() -> None:
    b = CustomerBase(cfg, seed=42)
    rows = np.arange(3000)
    before = b.hazard(DAY)[rows].mean()
    b.record("deal", rows)
    assert b.hazard(DAY)[rows].mean() < before


def test_a_clean_month_for_an_active_shopper_is_retentive() -> None:
    """Consistent availability is worth something, not merely the absence of harm."""
    b = CustomerBase(cfg, seed=42)
    rows = np.arange(3000)
    before = b.hazard(DAY)[rows].mean()
    b.record("order", rows)
    assert b.hazard(DAY)[rows].mean() < before


def test_harm_from_bad_experiences_is_capped() -> None:
    """A very bad month should not drive churn to a certainty."""
    b = CustomerBase(cfg, seed=42)
    rows = np.arange(500)
    for _ in range(25):
        b.record("stockout", rows)
        b.record("cancelled", rows)
    cap = cfg.raw["segments"]["churn"]["max_worsening_multiplier"]
    ratio = b.hazard(DAY)[rows].mean() / CustomerBase(cfg, seed=42).hazard(DAY)[rows].mean()
    assert ratio <= cap * 1.01


def test_tenure_protects_long_standing_customers() -> None:
    b = CustomerBase(cfg, seed=42)
    h = b.hazard(DAY)
    tenure_days = DAY.toordinal() - b._signup_ord
    same_segment = b._segment_idx == b._segment_idx[0]
    old = h[same_segment & (tenure_days > 600)].mean()
    new = h[same_segment & (tenure_days < 90)].mean()
    assert old < new


def test_stepping_a_month_retires_customers_and_clears_the_counters() -> None:
    b = CustomerBase(cfg, seed=42)
    rng = np.random.default_rng(1)
    before_active = b.active_mask(DAY).sum()
    b.record("stockout", np.arange(5000))

    churned = b.step_month(DAY, rng)
    assert churned > 0
    assert b.active_mask(DAY).sum() == before_active - churned
    assert b._ev_stockout.sum() == 0, "event counters were not reset"


def test_churn_is_irreversible_for_the_individual() -> None:
    """Per customer, not in aggregate - the total active count keeps rising
    because acquisition continues after people churn."""
    b = CustomerBase(cfg, seed=42)
    rng = np.random.default_rng(1)
    before = b.active_mask(DAY)
    b.step_month(DAY, rng)
    churned = np.flatnonzero(before & ~b.active_mask(DAY))
    assert churned.size > 0

    for offset in (1, 30, 200):
        later = b.active_mask(DAY + dt.timedelta(days=offset))
        assert not later[churned].any(), "a churned customer came back to life"


# =============================================================== baskets
def test_every_demanded_unit_ends_up_in_a_basket() -> None:
    """The invariant that keeps one source of truth for volume."""
    assert int(_items["qty"].sum()) == int(_units.sum())


def test_lines_never_exceed_units_and_quantities_are_positive() -> None:
    assert len(_items) <= int(_units.sum())
    assert (_items["qty"] >= 1).all()


def test_order_totals_reconcile_with_their_lines() -> None:
    per_order = _items.groupby("order_id")["qty"].sum()
    joined = _orders.set_index("order_id")["n_units"]
    assert (per_order.reindex(joined.index) == joined).all()


def test_order_ids_are_unique() -> None:
    assert _orders["order_id"].is_unique


def test_basket_size_differs_by_segment_as_configured() -> None:
    by_seg = _orders.groupby("segment")["n_units"].mean()
    assert by_seg["bulk_planner"] > by_seg["convenience"] * 1.8
    assert by_seg["convenience"] < 4


def test_segment_category_preference_shows_up_in_baskets() -> None:
    import pandas as pd

    joined = _items.merge(_orders[["order_id", "segment"]], on="order_id", how="left").merge(
        catalog[["sku_id", "l1_category"]], on="sku_id", how="left"
    )
    share = pd.crosstab(joined["segment"], joined["l1_category"], normalize="index")
    assert (
        share.loc["bulk_planner", "Staples & Packaged Food"]
        > share.loc["convenience", "Staples & Packaged Food"]
    ), "bulk planners are not buying more staples than convenience shoppers"


def test_private_label_affinity_shows_up_in_baskets() -> None:
    joined = _items.merge(_orders[["order_id", "segment"]], on="order_id", how="left").merge(
        catalog[["sku_id", "is_private_label"]], on="sku_id", how="left"
    )
    share = joined.groupby("segment")["is_private_label"].mean()
    assert share["price_sensitive"] > share["premium"] * 1.3, (
        "private-label affinity is not differentiating the segments"
    )


def test_segment_preference_does_not_change_store_category_totals() -> None:
    """Preference reallocates units between customers; it must not create or
    destroy demand, or the store affinity in demand.py gets double-counted."""
    joined = _items.merge(catalog[["sku_id", "l1_category"]], on="sku_id", how="left")
    by_cat = joined.groupby("l1_category")["qty"].sum()

    truth = {}
    for i, name in enumerate(model.category_names):
        truth[name] = int(_units[:, model.sku_category_idx == i].sum())
    for name, total in truth.items():
        if total > 500:
            assert by_cat[name] == total, f"{name} totals drifted"


def test_delivery_lateness_is_plausible_and_peak_hours_are_worse() -> None:
    assert 0.02 < _orders["is_late"].mean() < 0.20
    hours = _orders["order_ts"].apply(lambda t: t.hour)
    peak = _orders.loc[hours.isin([8, 9, 10, 19, 20, 21, 22]), "is_late"].mean()
    off = _orders.loc[~hours.isin([8, 9, 10, 19, 20, 21, 22]), "is_late"].mean()
    assert peak > off


def test_late_orders_are_delivered_after_the_promise() -> None:
    late = _orders[_orders["is_late"]]
    assert (late["delivered_ts"] > late["promised_ts"]).all()
    on_time = _orders[~_orders["is_late"]]
    assert (on_time["delivered_ts"] <= on_time["promised_ts"]).mean() > 0.95


def test_orders_follow_the_intraday_curve() -> None:
    hours = _orders["order_ts"].apply(lambda t: t.hour)
    assert hours.between(0, 23).all()
    # the dead hours before dawn should be nearly empty
    assert (hours.isin([2, 3, 4])).mean() < 0.02


def test_a_store_with_no_active_customers_produces_nothing() -> None:
    b = CustomerBase(cfg, seed=42)
    asm = BasketAssembler(cfg, catalog, b)
    rng = np.random.default_rng(2)
    orders, items = asm.assemble(
        0, np.ones(len(catalog), dtype=int), DAY, np.array([], dtype=int), rng
    )
    assert orders.empty and items.empty


def test_zero_demand_produces_no_orders() -> None:
    b = CustomerBase(cfg, seed=42)
    asm = BasketAssembler(cfg, catalog, b)
    rng = np.random.default_rng(2)
    rows = b.active_by_store(DAY)[0]
    orders, items = asm.assemble(0, np.zeros(len(catalog), dtype=int), DAY, rows, rng)
    assert orders.empty and items.empty
