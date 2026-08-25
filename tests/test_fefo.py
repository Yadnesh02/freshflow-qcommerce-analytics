"""Contract for batch inventory, FEFO allocation and substitution (task S1.5).

This file carries **checkpoint gate G1** from the execution plan: on-hand never
goes negative, FEFO strictly consumes the first-expiring batch, and the
movement ledger reconciles to the stock counters exactly. Those three
properties are what make every inventory number downstream trustworthy, and
the reconciliation is the same one dbt will assert against the warehouse in
Sprint 2.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from simulator.catalog import build_catalog
from simulator.config_loader import load_sim_config
from simulator.fefo import (
    EXPIRY_WRITEOFF,
    INBOUND,
    SALE,
    Fulfiller,
    InventoryLedger,
    to_movement_frame,
)

cfg = load_sim_config()
catalog = build_catalog(cfg, seed=42)
N_STORES, N_SKUS = len(cfg.stores), len(catalog)
TODAY = dt.date(2026, 2, 17).toordinal()


def ledger() -> InventoryLedger:
    return InventoryLedger(N_STORES, N_SKUS)


def receive_one(led: InventoryLedger, store, sku, qty, expires_in, shelf=10, cost=20.0, day=TODAY):
    return led.receive(
        np.array([store]),
        np.array([sku]),
        np.array([qty]),
        np.array([day + expires_in]),
        np.array([shelf]),
        np.array([cost]),
        day,
    )


# =============================================================== FEFO ordering
def test_the_first_expiring_batch_is_consumed_first() -> None:
    """Not FIFO. Once inbound freshness varies by supplier, a batch that arrived
    later routinely expires sooner - and FIFO would sell it last, guaranteeing
    the write-off."""
    led = ledger()
    receive_one(led, 0, 5, 40, expires_in=9)  # arrived first, expires later
    receive_one(led, 0, 5, 25, expires_in=3)  # arrived second, expires sooner

    allocs, short = led.allocate(0, 5, 30, TODAY)
    assert short == 0
    assert allocs[0].dte_at_sale == 3, "FEFO did not take the soonest-expiring batch first"
    assert allocs[0].qty == 25
    assert allocs[1].dte_at_sale == 9
    assert allocs[1].qty == 5


def test_ties_on_expiry_break_on_arrival_order() -> None:
    led = ledger()
    first = receive_one(led, 0, 5, 10, expires_in=4)[0]
    second = receive_one(led, 0, 5, 10, expires_in=4)[0]
    allocs, _ = led.allocate(0, 5, 15, TODAY)
    assert allocs[0].batch_row == first
    assert allocs[1].batch_row == second


def test_allocation_spans_as_many_batches_as_it_needs() -> None:
    led = ledger()
    for i in range(4):
        receive_one(led, 0, 5, 10, expires_in=2 + i)
    allocs, short = led.allocate(0, 5, 35, TODAY)
    assert short == 0
    assert [a.qty for a in allocs] == [10, 10, 10, 5]
    assert [a.dte_at_sale for a in allocs] == [2, 3, 4, 5]


def test_days_to_expiry_and_shelf_life_fraction_are_recorded() -> None:
    led = ledger()
    receive_one(led, 0, 5, 10, expires_in=3, shelf=12)
    allocs, _ = led.allocate(0, 5, 1, TODAY)
    assert allocs[0].dte_at_sale == 3
    assert allocs[0].shelf_life_fraction == pytest.approx(3 / 12)


# =============================================================== G1 invariants
def test_on_hand_never_goes_negative_on_over_allocation() -> None:
    led = ledger()
    receive_one(led, 0, 5, 10, expires_in=5)
    allocs, short = led.allocate(0, 5, 25, TODAY)
    assert short == 15
    assert sum(a.qty for a in allocs) == 10
    assert led.on_hand(0, 5) == 0


def test_allocating_from_an_empty_cell_is_a_clean_shortfall() -> None:
    led = ledger()
    allocs, short = led.allocate(3, 77, 12, TODAY)
    assert allocs == [] and short == 12
    assert led.on_hand(3, 77) == 0


def test_zero_quantity_batches_do_not_slide_the_row_indices() -> None:
    """A skipped zero-qty batch used to shift every later row by one, silently
    attributing movements to the wrong batch."""
    led = ledger()
    rows = led.receive(
        np.array([0, 0, 0]),
        np.array([1, 2, 3]),
        np.array([5, 0, 7]),
        np.array([TODAY + 4] * 3),
        np.array([10] * 3),
        np.array([20.0] * 3),
        TODAY,
    )
    assert list(rows) == [0, 1, 2]
    allocs, _ = led.allocate(0, 3, 7, TODAY)
    assert allocs[0].batch_row == 2, "row index drifted past a zero-quantity batch"


def test_the_movement_ledger_reconciles_to_the_stock_counters() -> None:
    """Gate G1. The same reconciliation dbt will run against the warehouse."""
    led, rng = ledger(), np.random.default_rng(4)
    for _ in range(400):
        receive_one(
            led,
            int(rng.integers(N_STORES)),
            int(rng.integers(200)),
            int(rng.integers(1, 60)),
            int(rng.integers(1, 12)),
        )
    for _ in range(600):
        led.allocate(
            int(rng.integers(N_STORES)), int(rng.integers(200)), int(rng.integers(1, 30)), TODAY
        )
    led.expire(TODAY + 6)

    assert np.array_equal(led.reconcile(), led.on_hand_matrix)
    assert (led.on_hand_matrix >= 0).all()

    # and the event log itself sums to the same place
    from collections import defaultdict

    per_batch = defaultdict(int)
    for _kind, row, delta, _ord, _seq in led.movements:
        per_batch[row] += delta
    assert all(v >= 0 for v in per_batch.values()), "a batch was over-consumed"


# =============================================================== expiry
def test_expired_stock_is_written_off_exactly_once() -> None:
    led = ledger()
    receive_one(led, 0, 5, 30, expires_in=2)
    first = led.expire(TODAY + 3)
    assert first == [(0, 30)]
    assert led.on_hand(0, 5) == 0
    assert led.expire(TODAY + 30) == [], "the same batch was written off twice"


def test_stock_is_not_written_off_before_it_expires() -> None:
    led = ledger()
    receive_one(led, 0, 5, 30, expires_in=5)
    assert led.expire(TODAY + 5) == []
    assert led.on_hand(0, 5) == 30
    assert led.expire(TODAY + 6) == [(0, 30)]


def test_expiry_only_writes_off_what_is_left_after_sales() -> None:
    led = ledger()
    receive_one(led, 0, 5, 30, expires_in=1)
    led.allocate(0, 5, 18, TODAY)
    assert led.expire(TODAY + 2) == [(0, 12)]


def test_write_offs_are_logged_as_movements() -> None:
    led = ledger()
    receive_one(led, 0, 5, 30, expires_in=1)
    led.allocate(0, 5, 10, TODAY)
    led.expire(TODAY + 2)
    kinds = [m[0] for m in led.movements]
    assert kinds == [INBOUND, SALE, EXPIRY_WRITEOFF]
    assert sum(m[2] for m in led.movements) == 0


def test_draining_movements_empties_the_log() -> None:
    led = ledger()
    receive_one(led, 0, 5, 10, expires_in=3)
    drained = led.drain_movements()
    assert len(drained) == 1
    assert led.movements == []


def test_movement_frame_carries_batch_ids_and_dates() -> None:
    led = ledger()
    receive_one(led, 0, 5, 10, expires_in=3)
    led.allocate(0, 5, 4, TODAY)
    frame = to_movement_frame(led.drain_movements(), ["BAT-00000000"])
    assert list(frame["event_type"]) == [INBOUND, SALE]
    assert list(frame["qty_delta"]) == [10, -4]
    assert frame["batch_id"].nunique() == 1
    assert frame["event_date"].iloc[0] == dt.date.fromordinal(TODAY)


def test_an_empty_movement_log_still_produces_the_right_columns() -> None:
    frame = to_movement_frame([], [])
    assert frame.empty
    assert {"batch_id", "event_type", "qty_delta", "event_date"} <= set(frame.columns)


# =============================================================== substitution
def _milk_setup(stock_all_but_first: bool = True):
    led = ledger()
    ful = Fulfiller(cfg, catalog)
    milk = catalog.index[catalog["l2_subcategory"] == "Milk"].to_numpy()
    stocked = milk[1:] if stock_all_but_first else milk
    led.receive(
        np.zeros(len(stocked), dtype=int),
        stocked,
        np.full(len(stocked), 400),
        np.full(len(stocked), TODAY + 3),
        np.full(len(stocked), 3),
        np.full(len(stocked), 30.0),
        TODAY,
    )
    return led, ful, milk


def test_a_stockout_routes_demand_to_substitutes_and_loss() -> None:
    led, ful, milk = _milk_setup()
    rng = np.random.default_rng(11)
    got, lost, hit = ful.fulfil_line(led, 0, int(milk[0]), 400, TODAY, rng)

    assert hit is True
    assert lost > 0, "nothing was lost - availability would not be a revenue problem"
    substituted = sum(a.qty for s, a in got if s != milk[0])
    assert substituted + lost == 400


def test_the_lost_share_matches_the_configured_matrix_when_substitutes_are_available() -> None:
    """Measured with the shelf deliberately deep, so nothing is lost merely
    because the substitute had also run out."""
    led = ledger()
    ful = Fulfiller(cfg, catalog)
    milk = catalog.index[catalog["l2_subcategory"] == "Milk"].to_numpy()
    stocked = milk[1:]
    led.receive(
        np.zeros(len(stocked), dtype=int),
        stocked,
        np.full(len(stocked), 20_000),
        np.full(len(stocked), TODAY + 3),
        np.full(len(stocked), 3),
        np.full(len(stocked), 30.0),
        TODAY,
    )
    rng = np.random.default_rng(13)
    _, lost, _ = ful.fulfil_line(led, 0, int(milk[0]), 4000, TODAY, rng)
    expected = cfg.raw["catalog"]["substitution"]["overrides"]["Dairy & Eggs"]["lost"]
    assert lost / 4000 == pytest.approx(expected, abs=0.03)


def test_a_stocked_out_substitute_pushes_realised_loss_above_the_intent_rate() -> None:
    """Cascading stockouts. The configured matrix is where demand *tries* to go;
    when the substitute is empty too, that demand is lost as well. Realised loss
    is therefore always at least the configured rate, and the gap is itself the
    cost of a thin shelf."""
    intent = cfg.raw["catalog"]["substitution"]["overrides"]["Dairy & Eggs"]["lost"]

    led, ful, milk = _milk_setup()  # substitutes stocked, but only shallowly
    rng = np.random.default_rng(13)
    _, lost, _ = ful.fulfil_line(led, 0, int(milk[0]), 4000, TODAY, rng)
    assert lost / 4000 > intent, "a shallow shelf did not increase realised loss"


def test_substitutes_stay_inside_the_subcategory() -> None:
    led, ful, milk = _milk_setup()
    rng = np.random.default_rng(17)
    got, _, _ = ful.fulfil_line(led, 0, int(milk[0]), 500, TODAY, rng)
    subs = {s for s, _ in got if s != milk[0]}
    assert subs <= set(milk.tolist()), "substitution escaped the subcategory"


def test_an_in_stock_line_reports_no_stockout() -> None:
    led, ful, milk = _milk_setup(stock_all_but_first=False)
    rng = np.random.default_rng(19)
    got, lost, hit = ful.fulfil_line(led, 0, int(milk[0]), 10, TODAY, rng)
    assert hit is False and lost == 0
    assert sum(a.qty for _, a in got) == 10


def test_everything_is_lost_when_the_whole_subcategory_is_out() -> None:
    led = ledger()
    ful = Fulfiller(cfg, catalog)
    milk = catalog.index[catalog["l2_subcategory"] == "Milk"].to_numpy()
    rng = np.random.default_rng(23)
    got, lost, hit = ful.fulfil_line(led, 0, int(milk[0]), 25, TODAY, rng)
    assert hit is True and lost == 25 and got == []


def test_substitution_consumes_the_substitute_stock() -> None:
    led, ful, milk = _milk_setup()
    rng = np.random.default_rng(29)
    before = int(led.on_hand_matrix[0, milk[1:]].sum())
    got, lost, _ = ful.fulfil_line(led, 0, int(milk[0]), 300, TODAY, rng)
    after = int(led.on_hand_matrix[0, milk[1:]].sum())
    assert before - after == sum(a.qty for s, a in got if s != milk[0])


# =============================================================== stress
def test_a_long_random_run_never_breaks_the_invariants() -> None:
    """Sixty days of receipts, sales, substitution and expiry. The strongest
    evidence for G1 - property assertions rather than worked examples."""
    led = ledger()
    ful = Fulfiller(cfg, catalog)
    rng = np.random.default_rng(101)
    skus = np.arange(300)
    sold_total = 0

    for day in range(60):
        d = TODAY + day
        n = 120
        led.receive(
            rng.integers(0, N_STORES, n),
            rng.choice(skus, n),
            rng.integers(0, 40, n),
            d + rng.integers(1, 10, n),
            np.full(n, 8),
            np.full(n, 15.0),
            d,
        )
        for _ in range(300):
            store = int(rng.integers(N_STORES))
            sku = int(rng.choice(skus))
            got, lost, _ = ful.fulfil_line(led, store, sku, int(rng.integers(1, 6)), d, rng)
            sold_total += sum(a.qty for _, a in got)

        led.expire(d)
        assert (led.on_hand_matrix >= 0).all(), f"negative stock on day {day}"

    assert np.array_equal(led.reconcile(), led.on_hand_matrix)
    assert sold_total > 0

    inbound = sum(m[2] for m in led.movements if m[0] == INBOUND)
    out = -sum(m[2] for m in led.movements if m[0] != INBOUND)
    assert inbound - out == led.on_hand_matrix.sum(), "movements do not reconcile to stock"


def test_expiry_actually_bites_over_a_long_run() -> None:
    """If nothing ever expired there would be no problem to solve."""
    led = ledger()
    rng = np.random.default_rng(7)
    for day in range(40):
        d = TODAY + day
        n = 60
        led.receive(
            rng.integers(0, N_STORES, n),
            rng.integers(0, 100, n),
            rng.integers(10, 40, n),
            d + rng.integers(1, 5, n),
            np.full(n, 6),
            np.full(n, 15.0),
            d,
        )
        for _ in range(40):
            led.allocate(
                int(rng.integers(N_STORES)), int(rng.integers(100)), int(rng.integers(1, 8)), d
            )
        led.expire(d)

    written = -sum(m[2] for m in led.movements if m[0] == EXPIRY_WRITEOFF)
    inbound = sum(m[2] for m in led.movements if m[0] == INBOUND)
    assert written > 0, "nothing expired across 40 days of short-life stock"
    assert written / inbound < 0.95, "everything expired - the model is not selling"
