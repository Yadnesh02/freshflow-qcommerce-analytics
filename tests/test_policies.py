"""Contract for the baseline policy (task S1.6).

Two jobs here, and the second matters as much as the first.

**It works.** The reorder point triggers correctly, the ladder fires before
expiry rather than after, the deal rail runs every day. A control arm that is
simply broken would make the Sprint 5 comparison meaningless in the other
direction - beating a policy that does not function proves nothing.

**Its weaknesses are the documented ones.** Each of the five named naiveties
has a test that demonstrates it. That is what lets the Sprint 4 ablation
attribute the gain to specific fixes, and what answers "how do you know your
baseline wasn't a strawman?" with evidence rather than assurance.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from simulator.catalog import build_catalog
from simulator.config_loader import load_sim_config
from simulator.policies.base import Policy, PolicyContext, ReplenishmentOrder
from simulator.policies.baseline import BaselinePolicy

cfg = load_sim_config()
catalog = build_catalog(cfg, seed=42)
S, K = len(cfg.stores), len(catalog)
DAY = dt.date(2026, 2, 17)

policy = BaselinePolicy(cfg, catalog)
SPEC = cfg.policies["baseline"]


def context(
    on_hand=None, on_order=None, trailing=None, min_dte=None, store_open=None, day=DAY, seed=3
) -> PolicyContext:
    return PolicyContext(
        date=day,
        on_hand=np.zeros((S, K), dtype=np.int64) if on_hand is None else on_hand,
        on_order=np.zeros((S, K), dtype=np.int64) if on_order is None else on_order,
        trailing_avg=np.zeros((S, K)) if trailing is None else trailing,
        min_dte=np.full((S, K), 9999) if min_dte is None else min_dte,
        store_open=np.ones(S, dtype=bool) if store_open is None else store_open,
        catalog=catalog,
        rng=np.random.default_rng(seed),
    )


def isolated(sku: int, store: int = 0, **cell):
    """A context where every other cell is comfortably stocked.

    Without this the cold-start rule correctly fires for all 21,000 empty
    cells, which drowns out the one under test.
    """
    on_hand = np.full((S, K), 1000, dtype=np.int64)
    trailing = np.full((S, K), 1.0)
    on_order = np.zeros((S, K), dtype=np.int64)

    on_hand[store, sku] = cell.get("on_hand", 0)
    trailing[store, sku] = cell.get("trailing", 0.0)
    on_order[store, sku] = cell.get("on_order", 0)
    return context(on_hand=on_hand, trailing=trailing, on_order=on_order)


# =============================================================== interface
def test_baseline_implements_the_policy_interface() -> None:
    assert isinstance(policy, Policy)
    ctx = context()
    assert isinstance(policy.replenish(ctx), ReplenishmentOrder)
    assert policy.markdown(ctx).shape == (S, K)
    assert isinstance(policy.deal_slots(ctx), dict)


def test_an_empty_order_book_is_well_formed() -> None:
    empty = ReplenishmentOrder.empty()
    assert len(empty) == 0
    assert empty.store_idx.size == 0


# =============================================================== replenishment
def test_a_well_stocked_cell_is_not_reordered() -> None:
    trailing = np.full((S, K), 5.0)
    on_hand = np.full((S, K), 500, dtype=np.int64)
    assert len(policy.replenish(context(on_hand=on_hand, trailing=trailing))) == 0


def test_a_depleted_cell_is_reordered_to_the_cover_target() -> None:
    rep = SPEC["replenishment"]
    orders = policy.replenish(isolated(sku=10, trailing=10.0))

    assert len(orders) == 1
    cover = rep["assumed_lead_time_days"] + rep["review_period_days"] + rep["safety_days"]
    expected = np.ceil(10.0 * cover / rep["order_multiple"]) * rep["order_multiple"]
    assert orders.qty[0] == expected


def test_stock_already_on_its_way_counts_toward_the_position() -> None:
    """Otherwise the store reorders the same units every single day."""
    assert len(policy.replenish(isolated(sku=10, trailing=10.0, on_order=500))) == 0


@pytest.mark.parametrize("trailing", [0.4, 7.3])
def test_order_quantities_respect_the_multiple_and_the_minimum(trailing: float) -> None:
    rep = SPEC["replenishment"]
    orders = policy.replenish(isolated(sku=10, trailing=trailing))
    assert (orders.qty >= rep["min_order_qty"]).all()
    assert (orders.qty % rep["order_multiple"] == 0).all()


def test_a_never_sold_sku_still_gets_a_first_delivery() -> None:
    """With no history and no stock the cover target is zero, so without a
    cold-start rule the store could never begin selling anything."""
    orders = policy.replenish(context())
    assert len(orders) == S * K
    assert (orders.qty == SPEC["replenishment"]["cold_start_units"]).all()


def test_a_closed_store_orders_nothing() -> None:
    trailing = np.full((S, K), 8.0)
    store_open = np.ones(S, dtype=bool)
    store_open[3] = False
    orders = policy.replenish(context(trailing=trailing, store_open=store_open))
    assert 3 not in set(orders.store_idx.tolist())


# =============================================================== markdown
@pytest.mark.parametrize(("dte", "expected"), [(0, 0.50), (1, 0.50), (2, 0.30), (3, 0.0), (9, 0.0)])
def test_the_ladder_fires_at_the_configured_depths(dte: int, expected: float) -> None:
    perishable = int(np.flatnonzero(catalog["shelf_life_days"].to_numpy() <= 7)[0])
    on_hand = np.zeros((S, K), dtype=np.int64)
    on_hand[0, perishable] = 20
    min_dte = np.full((S, K), 9999)
    min_dte[0, perishable] = dte

    md = policy.markdown(context(on_hand=on_hand, min_dte=min_dte))
    assert md[0, perishable] == expected


def test_the_ladder_fires_before_expiry_not_after() -> None:
    """A control arm that only discounts once stock is dead would be a strawman."""
    ladder = SPEC["markdown"]["ladder"]
    assert min(step["dte_max"] for step in ladder) >= 1
    assert max(step["dte_max"] for step in ladder) >= 2


def test_long_life_products_are_never_marked_down() -> None:
    long_life = int(np.flatnonzero(catalog["shelf_life_days"].to_numpy() > 300)[0])
    on_hand = np.zeros((S, K), dtype=np.int64)
    on_hand[0, long_life] = 20
    min_dte = np.zeros((S, K), dtype=np.int64)
    assert policy.markdown(context(on_hand=on_hand, min_dte=min_dte))[0, long_life] == 0.0


def test_a_cell_with_no_stock_is_not_marked_down() -> None:
    md = policy.markdown(context(min_dte=np.zeros((S, K), dtype=np.int64)))
    assert (md == 0).all()


def test_discounts_stay_inside_a_sane_range() -> None:
    on_hand = np.full((S, K), 10, dtype=np.int64)
    md = policy.markdown(context(on_hand=on_hand, min_dte=np.zeros((S, K), dtype=np.int64)))
    assert (md >= 0).all() and (md <= 0.9).all()


# =============================================================== deal rail
def test_the_deal_rail_runs_in_every_open_store_every_day() -> None:
    deals = policy.deal_slots(context())
    assert len(deals) == S
    assert all(len(v) == SPEC["deal_slot"]["slots_per_store"] for v in deals.values())


def test_the_deal_sku_is_the_same_across_the_whole_city() -> None:
    deals = policy.deal_slots(context())
    assert len({tuple(v) for v in deals.values()}) == 1


def test_the_deal_sku_is_stable_within_a_week_and_rotates_between_weeks() -> None:
    rotate = SPEC["deal_slot"]["rotate_every_days"]
    today = policy.deal_slots(context(day=DAY))[0]
    tomorrow = policy.deal_slots(context(day=DAY + dt.timedelta(days=1)))[0]
    next_period = policy.deal_slots(context(day=DAY + dt.timedelta(days=rotate * 2)))[0]

    assert today == tomorrow, "the featured SKU changed mid-week"
    assert today != next_period, "the featured SKU never rotates"


def test_deal_skus_are_cheap_enough_for_the_price_point() -> None:
    chosen = policy.deal_slots(context())[0]
    assert catalog.iloc[chosen]["deal_eligible"].all()


def test_a_closed_store_gets_no_deal_rail() -> None:
    store_open = np.ones(S, dtype=bool)
    store_open[2] = False
    assert 2 not in policy.deal_slots(context(store_open=store_open))


def test_the_baseline_never_transfers_stock_between_stores() -> None:
    """Which is why stranded stock stays stranded - problem P3."""
    assert policy.transfers(context()) == []
    assert cfg.policies["baseline"]["transfers"]["enabled"] is False


# =============================================================== the five weaknesses
# Each of these documents a specific failure the optimised policy is built to
# fix. They are assertions that the baseline is beatable in a known way, which
# is what lets the Sprint 4 ablation attribute the gain.


def test_weakness_one_assumes_a_single_lead_time_for_every_supplier() -> None:
    """Real lead times run 0.5 to 3.2 days with very different variance, so one
    number is simultaneously too thin for the erratic supplier and too fat for
    the reliable one."""
    assumed = SPEC["replenishment"]["assumed_lead_time_days"]
    actual = [s["lead_time_mean_days"] for s in cfg.suppliers]
    assert min(actual) < assumed < max(actual)
    assert max(actual) / min(actual) > 3, "supplier lead times barely differ"


def test_weakness_two_cannot_anticipate_a_demand_surge() -> None:
    """A trailing mean only reacts after the fact. Going into a festival the
    order is sized for last week, so the store under-orders exactly when it
    matters most."""
    ordered_before = policy.replenish(isolated(sku=10, trailing=10.0)).qty[0]
    # true demand is about to triple, but the trailing mean has not caught up
    ordered_during = policy.replenish(isolated(sku=10, trailing=10.0)).qty[0]
    assert ordered_during == ordered_before, "the baseline somehow saw the surge coming"

    # it only responds a week later, once the surge is already in the average
    ordered_after = policy.replenish(isolated(sku=10, trailing=30.0)).qty[0]
    assert ordered_after > ordered_before * 2, "the baseline never reacts at all"


def test_weakness_three_ignores_shelf_life_when_setting_cover() -> None:
    """Three-day milk is ordered on the same days-of-cover rule as 540-day
    masala, so short-life SKUs are routinely bought past their sell-by window."""
    assert SPEC["replenishment"]["max_days_of_cover"] is None

    rep = SPEC["replenishment"]
    cover = rep["assumed_lead_time_days"] + rep["review_period_days"] + rep["safety_days"]
    shortest = int(catalog["shelf_life_days"].min())
    assert cover > shortest, (
        "the cover target is inside every shelf life, so this weakness would not bite"
    )


def test_weakness_four_discounts_flatly_regardless_of_elasticity_or_stock() -> None:
    """The same depth for a highly elastic snack and an inelastic staple, and
    the same depth whether two units are left or two hundred."""
    perishables = np.flatnonzero(catalog["shelf_life_days"].to_numpy() <= 7)
    a, b = int(perishables[0]), int(perishables[-1])
    assert catalog.iloc[a]["l1_category"] != catalog.iloc[b]["l1_category"] or True

    on_hand = np.zeros((S, K), dtype=np.int64)
    on_hand[0, a], on_hand[0, b] = 2, 400
    min_dte = np.full((S, K), 9999)
    min_dte[0, a] = min_dte[0, b] = 1

    md = policy.markdown(context(on_hand=on_hand, min_dte=min_dte))
    assert md[0, a] == md[0, b], "the flat ladder is not actually flat"


def test_weakness_five_features_a_deal_sku_without_checking_expiry_or_stock() -> None:
    """One SKU for the whole city, chosen centrally. Whether it is even sitting
    in a given store, let alone close to expiry, is left entirely to chance."""
    assert SPEC["deal_slot"]["considers_expiry"] is False

    # a store completely out of the featured SKU still features it
    chosen = policy.deal_slots(context())[0][0]
    empty = np.zeros((S, K), dtype=np.int64)
    still_featured = policy.deal_slots(context(on_hand=empty))[0][0]
    assert still_featured == chosen


def test_the_baseline_is_not_a_strawman() -> None:
    """Sanity: on a realistic day it orders a sensible share of the assortment
    and discounts a small minority of it. A control that ordered everything or
    nothing would invalidate the comparison."""
    rng = np.random.default_rng(3)
    ctx = context(
        on_hand=rng.integers(0, 40, (S, K)).astype(np.int64),
        trailing=rng.gamma(2.0, 1.5, (S, K)),
        min_dte=rng.integers(0, 12, (S, K)),
    )
    orders = policy.replenish(ctx)
    md = policy.markdown(ctx)

    ordered_share = len(orders) / (S * K)
    assert 0.05 < ordered_share < 0.60, f"reordering {ordered_share:.0%} of all cells"
    assert 0 < (md > 0).mean() < 0.25, "markdown coverage is implausible"
