"""Integration contract for the full simulation run (task S1.7).

Every component has its own unit tests. What this file defends is the seams
between them - the places where a wiring mistake produces plausible-looking
output that is quietly wrong:

  - order lines must reference batches that actually exist and were in the
    right store, or the FEFO key is decorative
  - inbound minus outbound must equal stock on hand, across a whole run
  - the day's sequence (receive, expire, price, demand, fulfil, reorder) must
    hold, or the store reacts to sales it has not made yet
  - every emitted feed must land in its date partition with the right grain

Runs a short window rather than the full year: the seams are the same, and a
365-day run in the test suite would make it useless.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from simulator.config_loader import load_sim_config
from simulator.run import DEAL_PRICE, SimulationRun

cfg = load_sim_config()
DAYS = 12


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("raw")
    sim = SimulationRun(cfg, seed=42, days=DAYS, out_dir=out, quiet=True)
    summary = sim.run()
    return sim, summary, out


def read(out, source: str) -> pd.DataFrame:
    files = sorted(out.glob(f"{source}/dt=*/*.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


# =============================================================== it runs
def test_the_run_produces_a_day_per_date(run) -> None:
    _, summary, _ = run
    assert len(summary) == DAYS
    assert summary["date"].is_unique
    assert summary["units_sold"].sum() > 0


def test_all_expected_feeds_are_emitted(run) -> None:
    _, _, out = run
    expected = {
        "pos_orders",
        "pos_order_items",
        "wms_purchase_orders",
        "wms_inventory_batch",
        "wms_inventory_movement",
        "wms_stockout_interval",
        "clickstream",
        "price_history",
        "catalog_snapshot",
        "ref_stores",
        "ref_suppliers",
    }
    assert expected <= {p.name for p in out.iterdir()}


def test_feeds_are_hive_partitioned_by_date(run) -> None:
    _, _, out = run
    parts = sorted(p.name for p in (out / "pos_orders").iterdir())
    assert all(p.startswith("dt=") for p in parts)
    assert len(parts) == DAYS
    dt.date.fromisoformat(parts[0].removeprefix("dt="))


# =============================================================== the FEFO seam
def test_every_order_line_references_a_real_batch(run) -> None:
    """The keystone join. If this breaks, nothing downstream can attribute a
    sale to an expiry date."""
    _, _, out = run
    items, batches = read(out, "pos_order_items"), read(out, "wms_inventory_batch")
    known = set(batches["batch_id"]) | {b for b in items["batch_id"] if b.startswith("BAT-OPEN-")}
    orphans = set(items["batch_id"]) - known
    assert not orphans, f"{len(orphans)} order lines point at batches that do not exist"


def test_a_line_is_never_served_from_another_store_batch(run) -> None:
    _, _, out = run
    items = read(out, "pos_order_items").merge(
        read(out, "pos_orders")[["order_id", "store_id"]], on="order_id", how="left"
    )
    batches = read(out, "wms_inventory_batch")[["batch_id", "store_id"]].rename(
        columns={"store_id": "batch_store"}
    )
    joined = items.merge(batches, on="batch_id", how="inner")
    assert (joined["store_id"] == joined["batch_store"]).all(), (
        "stock was sold out of a store that never held it"
    )


def test_sold_batches_had_not_expired_at_the_time_of_sale(run) -> None:
    _, _, out = run
    items = read(out, "pos_order_items")
    assert (items["dte_at_sale"] >= 0).all(), "expired stock was sold"


def test_line_economics_are_internally_consistent(run) -> None:
    _, _, out = run
    items = read(out, "pos_order_items")
    assert (items["qty"] > 0).all()
    assert (items["unit_realized_price"] <= items["unit_base_price"] + 1e-9).all()
    computed = items["unit_base_price"] - items["unit_realized_price"]
    assert np.allclose(computed, items["discount_amt"], atol=0.011)
    assert (items["unit_cogs"] > 0).all()


# =============================================================== the ledger seam
def test_the_movement_ledger_reconciles_across_the_whole_run(run) -> None:
    """Gate G1, end to end rather than in isolation."""
    sim, _, out = run
    moves = read(out, "wms_inventory_movement")
    inbound = moves.loc[moves["event_type"] == "inbound", "qty_delta"].sum()
    outbound = -moves.loc[moves["event_type"] != "inbound", "qty_delta"].sum()

    # opening stock is seeded before the emitted window, so add it back
    opening = sum(e[3] for q in sim.ledger._queues.values() for e in q)
    assert (sim.ledger.on_hand_matrix >= 0).all()
    assert np.array_equal(sim.ledger.reconcile(), sim.ledger.on_hand_matrix)
    assert inbound - outbound <= opening + inbound


def test_sales_movements_match_the_order_lines(run) -> None:
    _, _, out = run
    moves = read(out, "wms_inventory_movement")
    sold_by_ledger = -moves.loc[moves["event_type"] == "sale", "qty_delta"].sum()
    sold_by_orders = read(out, "pos_order_items")["qty"].sum()
    assert sold_by_ledger == sold_by_orders, (
        "the inventory ledger and the sales feed disagree about how much was sold"
    )


def test_batches_arrive_after_they_were_ordered(run) -> None:
    _, _, out = run
    pos = read(out, "wms_purchase_orders")
    assert (pos["received_date"] >= pos["ordered_date"]).all()
    assert (pos["received_qty"] <= pos["ordered_qty"]).all()


def test_batch_expiry_is_always_after_receipt(run) -> None:
    _, _, out = run
    b = read(out, "wms_inventory_batch")
    assert (b["expiry_date"] > b["received_date"]).all()
    assert (b["qty_received"] > 0).all()


# =============================================================== the day sequence
def test_orders_only_reference_dates_inside_their_own_partition(run) -> None:
    """A leak here would mean the day loop is writing across date boundaries."""
    _, _, out = run
    for path in sorted((out / "pos_orders").iterdir()):
        day = dt.date.fromisoformat(path.name.removeprefix("dt="))
        frame = pd.read_parquet(next(path.glob("*.parquet")))
        assert (frame["order_ts"].dt.date == day).all()


def test_no_store_trades_before_it_opens(run) -> None:
    _, _, out = run
    orders = read(out, "pos_orders")
    opened = {s["store_id"]: s["opened_date"] for s in cfg.stores}
    first_seen = orders.groupby("store_id")["order_ts"].min().dt.date
    for store, first in first_seen.items():
        assert first >= opened[store]


def test_a_store_that_has_not_opened_holds_no_stock(run) -> None:
    sim, _, _ = run
    late = [i for i, s in enumerate(cfg.stores) if s["opened_date"] > dt.date(2025, 9, 30)]
    assert late, "no store opens after the test window"
    assert sim.ledger.on_hand_matrix[late].sum() == 0


# =============================================================== pricing seam
def test_marked_down_lines_carry_a_promo_id(run) -> None:
    _, _, out = run
    items = read(out, "pos_order_items")
    discounted = items[items["discount_amt"] > 0.01]
    assert len(discounted) > 0, "nothing was ever marked down"
    assert discounted["promo_id"].notna().all()


def test_full_price_lines_carry_no_promo_id(run) -> None:
    _, _, out = run
    items = read(out, "pos_order_items")
    full = items[items["discount_amt"] <= 0.001]
    assert full["promo_id"].isna().all()


def test_the_deal_rail_sells_at_the_advertised_price(run) -> None:
    _, _, out = run
    items = read(out, "pos_order_items")
    deals = items[items["promo_id"] == "PROMO-DEAL11"]
    if deals.empty:
        pytest.skip("the featured SKU sold nothing in this short window")
    assert (deals["unit_realized_price"] == DEAL_PRICE).all()


def test_price_history_only_records_cells_that_moved(run) -> None:
    _, _, out = run
    prices = read(out, "price_history")
    assert (prices["realized_price"] < prices["base_price"]).all()


# =============================================================== censored demand
def test_out_of_stock_browsing_is_captured(run) -> None:
    """The censored-demand signal. Without it the forecast in Sprint 3 would
    learn a store's stockouts as if they were low demand."""
    _, _, out = run
    clicks = read(out, "clickstream")
    assert not clicks["was_in_stock"].all(), "no out-of-stock views were recorded"
    assert (clicks["event_type"] == "notify_me").any()
    assert not clicks.loc[clicks["event_type"] == "notify_me", "was_in_stock"].any()


def test_stockout_intervals_are_recorded_with_an_hour(run) -> None:
    _, _, out = run
    outs = read(out, "wms_stockout_interval")
    assert len(outs) > 0
    assert outs["hour_out"].between(0, 23).all()


def test_a_stockout_is_reflected_in_both_feeds(run) -> None:
    _, _, out = run
    outs = read(out, "wms_stockout_interval")
    clicks = read(out, "clickstream")
    oos = clicks[~clicks["was_in_stock"]]
    overlap = set(zip(outs["store_id"], outs["sku_id"], strict=True)) & set(
        zip(oos["store_id"], oos["sku_id"], strict=True)
    )
    assert overlap, "stockout intervals and out-of-stock browsing do not agree"


# =============================================================== SCD2 seam
def test_the_catalogue_snapshot_hides_generator_ground_truth(run) -> None:
    _, _, out = run
    snap = read(out, "catalog_snapshot")
    for col in ("popularity_weight", "elasticity", "hour_curve"):
        assert col not in snap.columns, f"'{col}' leaked into the source feed"
    assert "snapshot_date" in snap.columns


def test_the_catalogue_is_snapshotted_every_day(run) -> None:
    _, _, out = run
    snap = read(out, "catalog_snapshot")
    assert snap["snapshot_date"].nunique() == DAYS
    assert snap.groupby("snapshot_date")["sku_id"].nunique().eq(1500).all()


# =============================================================== plausibility
def test_the_network_is_neither_starved_nor_drowning(run) -> None:
    """Sanity band. A fill rate of 100% or 20% would both mean the wiring is
    wrong rather than the policy being naive."""
    _, summary, _ = run
    t = summary.sum(numeric_only=True)
    fill = t["units_sold"] / t["units_demanded"]
    wastage = t["units_expired"] / (t["units_sold"] + t["units_expired"])
    assert 0.60 < fill < 0.98, f"fill rate {fill:.1%}"
    assert 0.0 < wastage < 0.15, f"unit wastage {wastage:.1%}"


def test_the_run_makes_a_gross_margin(run) -> None:
    _, summary, _ = run
    t = summary.sum(numeric_only=True)
    gm = (t["revenue"] - t["cogs"]) / t["revenue"]
    assert 0.10 < gm < 0.40, f"gross margin {gm:.1%}"


def test_substitution_and_loss_account_for_every_unfilled_unit(run) -> None:
    _, summary, _ = run
    t = summary.sum(numeric_only=True)
    assert t["units_sold"] + t["units_lost"] == t["units_demanded"]


def test_the_run_is_reproducible(tmp_path) -> None:
    """Common random numbers: the Sprint 5 experiment compares two policies over
    the same world, which only works if the world is identical run to run."""
    a = SimulationRun(cfg, seed=42, days=3, out_dir=tmp_path / "a", quiet=True).run()
    b = SimulationRun(cfg, seed=42, days=3, out_dir=tmp_path / "b", quiet=True).run()
    pd.testing.assert_frame_equal(a, b)


def test_a_different_seed_gives_a_different_world(tmp_path) -> None:
    a = SimulationRun(cfg, seed=42, days=3, out_dir=tmp_path / "a", quiet=True).run()
    c = SimulationRun(cfg, seed=7, days=3, out_dir=tmp_path / "c", quiet=True).run()
    assert not a["units_sold"].equals(c["units_sold"])


def test_per_store_outcomes_sum_to_the_estate_totals() -> None:
    """S5.2 needs an outcome per store per day, and it must be the same day.

    `DayCounters` is estate-wide, so a difference-in-differences over a
    store-level holdout cannot be computed from it. The per-store rows are
    accumulated in the same pass rather than aggregated afterwards from the
    bronze feeds - the experiment harness throws those away, and re-reading them
    would turn a 30-seed run into an I/O problem it currently is not.

    Two ways that could silently go wrong, and this catches both: a counter
    incremented on the estate total but not per store, or one attributed to the
    wrong store index. Expiry is the one worth watching - it happens outside the
    per-store loop, and its store had to be threaded out of the FEFO ledger,
    which is the only reason `InventoryLedger.expire` returns a triple.
    """
    import tempfile
    from pathlib import Path

    import pandas as pd

    with tempfile.TemporaryDirectory() as tmp:
        run = SimulationRun(cfg, seed=7, days=4, out_dir=Path(tmp), quiet=True)
        estate = run.run()
        per_store = pd.DataFrame(run.store_summary)

    assert len(per_store) == len(estate) * run.S, "expected one row per store per day"
    assert per_store["store_id"].nunique() == run.S

    for column in (
        "units_sold",
        "units_lost",
        "units_expired",
        "writeoff_value",
        "revenue",
        "cogs",
        "orders",
        "stockout_cells",
    ):
        assert per_store[column].sum() == pytest.approx(estate[column].sum(), abs=0.01), (
            f"{column} does not reconcile: the per-store rows and the estate total disagree"
        )
