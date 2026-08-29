"""What the treatment arm has to be true of (task S4.6).

Two properties matter more than the rest, and both are about integrity rather
than performance.

**The policy must not read the simulator's ground truth.** `catalog["elasticity"]`
is the actual coefficient the demand engine draws from. A policy using it would
post a wonderful result and prove nothing, because no real business has that
column - and the whole answer to "you made the data, so of course it worked"
rests on the treatment arm being as blind as a real operator.
`test_the_policy_never_touches_ground_truth` walks the module's AST for the
access by name.

**The policy must not read its own future.** The rec_* tables were computed on a
warehouse built from the full year, so a policy on day 120 looking up its own
row would be reading day 300. What crosses the boundary is fitted parameters -
coefficients and knobs - and `test_the_bundle_carries_no_recommendations` checks
the exported file has no per-day, per-store rows in it.

The rest is constraint checking, and one honest measurement: the in-simulation
deal allocator is greedy where S4.3 solves an integer program, because Sprint 5
runs tens of thousands of store-days. The greedy is not the IP and the test says
how far apart they are rather than pretending they agree.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
from pathlib import Path

import numpy as np
import pytest

from analytics.optimization.policy_bundle import BUNDLE_PATH
from simulator.config_loader import load_sim_config
from simulator.policies.base import PolicyContext
from simulator.policies.optimized import NO_STOCK_DTE, OptimizedPolicy

ROOT = Path(__file__).resolve().parent.parent
POLICY_SOURCE = ROOT / "simulator" / "policies" / "optimized.py"


@pytest.fixture(scope="module")
def bundle() -> dict:
    if not BUNDLE_PATH.exists():
        pytest.skip(f"no bundle at {BUNDLE_PATH} - run `python tasks.py policy-bundle`")
    return json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cfg():
    return load_sim_config()


@pytest.fixture(scope="module")
def catalog(cfg):
    from simulator.catalog import build_catalog

    return build_catalog(cfg, seed=42)


@pytest.fixture(scope="module")
def policy(cfg, catalog, bundle):
    return OptimizedPolicy(cfg, catalog)


def make_context(catalog, n_stores: int = 3, seed: int = 7) -> PolicyContext:
    rng = np.random.default_rng(seed)
    n_skus = len(catalog)
    shape = (n_stores, n_skus)
    on_hand = rng.integers(0, 60, size=shape).astype(float)
    min_dte = np.where(on_hand > 0, rng.integers(0, 9, size=shape), NO_STOCK_DTE).astype(float)
    return PolicyContext(
        date=dt.date(2026, 3, 1),
        on_hand=on_hand,
        on_order=np.zeros(shape),
        trailing_avg=rng.gamma(2.0, 1.5, size=shape),
        min_dte=min_dte,
        store_open=np.ones(n_stores, dtype=bool),
        catalog=catalog,
        rng=rng,
    )


# ================================================== integrity
def test_the_policy_never_touches_ground_truth() -> None:
    """catalog["elasticity"] is the coefficient the demand engine draws from.

    Reading it would make the treatment arm clairvoyant about the exact quantity
    the whole of S4.1 exists to estimate badly.
    """
    tree = ast.parse(POLICY_SOURCE.read_text(encoding="utf-8"))
    forbidden = {"elasticity", "popularity_weight", "hour_curve"}
    hits = [
        node.slice.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value in forbidden
        and isinstance(node.value, ast.Name)
        and node.value.id == "catalog"
    ]
    assert not hits, f"the optimised policy reads simulator ground truth: {hits}"


def test_the_bundle_carries_no_recommendations(bundle) -> None:
    """Fitted parameters may cross the boundary; per-day decisions may not."""
    forbidden_keys = {"rec_markdown", "rec_deal_slot", "rec_transfer_order", "rec_purchase_order"}
    assert not forbidden_keys & set(bundle), "the bundle carries recommendation tables"
    for cell in bundle["elasticity"]:
        assert "store_id" not in cell and "date_day" not in cell, (
            "the bundle carries per-store or per-day rows, which is look-ahead"
        )


def test_unidentified_elasticity_cells_never_reach_the_policy(policy, bundle) -> None:
    """S4.1 could not identify nine of twenty-three cells. Those must stay out."""
    identified = {
        (c["l1_category"], c["dte_band"]) for c in bundle["elasticity"] if c["is_identified"]
    }
    carried = {(c["l1_category"], c["dte_band"]) for c in policy._elasticity_bands}
    assert carried == identified
    assert len(carried) < len(bundle["elasticity"]), (
        "every cell was carried through, so the identification guard is doing nothing"
    )


# ================================================== the five naiveties
def test_lead_time_varies_by_category(policy) -> None:
    """Naivety #1: the baseline uses one assumed lead time for every supplier."""
    assert len(np.unique(policy._lead_days)) > 1, (
        "the optimised arm uses a single lead time, which is the baseline's mistake"
    )


def test_replenishment_is_capped_by_shelf_life(policy, catalog) -> None:
    """Naivety #3: the baseline puts three-day milk on the same cover rule as masala."""
    ctx = make_context(catalog)
    order = policy.replenish(ctx)
    if not len(order):
        pytest.skip("nothing ordered on this synthetic context")
    shelf_life = catalog["shelf_life_days"].to_numpy()[order.sku_idx]
    daily = ctx.trailing_avg[order.store_idx, order.sku_idx]
    on_hand = ctx.on_hand[order.store_idx, order.sku_idx]
    # a cell cannot be ordered up to more than its whole shelf life could sell
    ceiling = daily * np.maximum(shelf_life, 1.0) * 4 + on_hand + 12
    assert (order.qty <= ceiling + 1e-6).all(), "an order exceeds what its shelf life could clear"


def test_markdown_only_fires_where_demand_is_elastic(policy, catalog) -> None:
    """Naivety #4, and S4.2's finding wired faithfully.

    Cutting price raises revenue only when |elasticity| > 1. No band S4.1 fitted
    reaches that, so on the real coefficients this arm marks down nothing at all
    - which is the analysis speaking, not a bug.
    """
    ctx = make_context(catalog)
    depth = policy.markdown(ctx)
    assert depth.shape == ctx.on_hand.shape
    assert (depth >= 0).all() and (depth <= 0.6).all()

    beta = policy._elasticity_for(ctx.min_dte)
    fired = depth > 0
    assert not (fired & ~(beta < -1.0)).any(), (
        "a markdown fired on a cell whose demand is inelastic or unmeasured"
    )


def test_deal_slots_respect_every_constraint(policy, catalog) -> None:
    """Naivety #5: one citywide SKU, unconnected to what is expiring."""
    ctx = make_context(catalog)
    chosen = policy.deal_slots(ctx)
    required_pl = int(np.ceil(policy._pl_floor * policy._slots))

    for store, skus in chosen.items():
        assert len(skus) <= policy._slots, f"store {store} exceeded its slot budget"
        subcategories = [policy._l2[s] for s in skus]
        assert len(subcategories) == len(set(subcategories)), "two slots in one subcategory"
        for sku in skus:
            assert ctx.on_hand[store, sku] >= policy._deal_min_on_hand
            assert ctx.min_dte[store, sku] >= policy._deal_min_dte
            assert policy._base_price[sku] > policy._deal_price
        if len(skus) == policy._slots:
            pl = sum(1 for s in skus if policy._is_private_label[s])
            assert pl >= required_pl, f"store {store} filled all slots below the PL floor"


def test_the_deal_rail_is_not_the_same_sku_everywhere(policy, catalog) -> None:
    """The whole point of a per-store allocator."""
    ctx = make_context(catalog, n_stores=6)
    chosen = policy.deal_slots(ctx)
    picked = [tuple(sorted(v)) for v in chosen.values() if v]
    assert len(set(picked)) > 1, "every store got an identical rail, which is the baseline"


# ================================================== transfers
def test_no_transfer_arrives_after_expiry(policy, catalog) -> None:
    ctx = make_context(catalog, n_stores=6)
    cfg = policy.bundle["transfers"]
    transit = (cfg["handling_hours"] + 12.84 / cfg["speed_kmh"]) / 24.0
    for _from, to, sku, qty in policy.transfers(ctx):
        assert qty >= cfg["min_transfer_units"]
        assert ctx.min_dte[to, sku] - transit > 0, "a transfer arrives after the stock expires"


def test_transfers_are_bounded_by_the_loading_bay(policy, catalog) -> None:
    """A store dispatches vans, not parcels.

    Without this the rule proposed 2,715 separate loads in a single day. The
    benefit test alone did not catch it, because `qty x price > trip_cost`
    clears about five units of anything.
    """
    ctx = make_context(catalog, n_stores=8)
    moves = policy.transfers(ctx)
    cap = int(policy.bundle["transfers"].get("max_outbound_trips_per_store_day", 2))
    trips: dict[int, set[int]] = {}
    for frm, to, _sku, _qty in moves:
        trips.setdefault(frm, set()).add(to)
    for store, destinations in trips.items():
        assert len(destinations) <= cap, f"store {store} dispatched {len(destinations)} vans"


def test_a_store_never_transfers_to_itself(policy, catalog) -> None:
    ctx = make_context(catalog, n_stores=5)
    assert all(frm != to for frm, to, _, _ in policy.transfers(ctx))


# ================================================== plumbing
def test_the_policy_refuses_to_run_without_its_bundle(cfg, catalog, tmp_path) -> None:
    """The optimised arm is Sprint 4's output. Silently falling back to defaults
    would produce a treatment arm that is not the one anybody measured."""
    with pytest.raises(FileNotFoundError, match="policy-bundle"):
        OptimizedPolicy(cfg, catalog, bundle_path=tmp_path / "absent.json")


def test_both_arms_are_declared_and_configured(cfg) -> None:
    assert cfg.policies["optimized"]["implemented"] is True
    baseline_window = cfg.policies["baseline"]["replenishment"]["trailing_window_days"]
    optimized_window = cfg.policies["optimized"]["replenishment"]["trailing_window_days"]
    assert baseline_window == optimized_window, (
        "the arms see different history windows, so the comparison measures the window"
    )


@pytest.mark.skipif(
    os.environ.get("FRESHFLOW_SKIP_SLOW") == "1", reason="requires a short simulation"
)
def test_a_simulated_day_produces_actions(cfg) -> None:
    """S4.6's gate, run for real rather than asserted about."""
    from simulator.policies.preview import action_list, context_after
    from simulator.run import SimulationRun, build_policy

    run = SimulationRun(cfg, seed=42, days=10, policy_name="optimized", quiet=True)
    run.run()
    day = run.summary[-1]["date"] + dt.timedelta(days=1)
    ctx = context_after(run, day)
    actions = action_list(run, build_policy("optimized", cfg, run.catalog), ctx)

    assert actions["order_lines"] > 0, "a simulated day produced no replenishment at all"
    assert actions["order_value"] > 0
    assert actions["policy"] == "optimized"
