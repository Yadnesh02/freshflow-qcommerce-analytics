"""Checkpoint gate G1 as a test (task S1.9).

`simulator/verify.py` is the tool you run against the real 365-day dataset.
This file proves the tool itself works - that each invariant genuinely fails
when it should, rather than passing because the query was wrong.

A gate that cannot fail is not a gate. So most of what follows deliberately
corrupts a raw layer and asserts the corresponding check goes red.
"""

from __future__ import annotations

import shutil

import pandas as pd
import pytest

from simulator.config_loader import load_sim_config
from simulator.dirt import DirtInjector
from simulator.run import SimulationRun
from simulator.verify import EXPLAINED, FAIL, PASS, Gate

cfg = load_sim_config()
DAYS = 10


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    out = tmp_path_factory.mktemp("gate") / "raw"
    SimulationRun(cfg, seed=42, days=DAYS, out_dir=out, quiet=True).run()
    return out


def statuses(raw) -> dict[str, str]:
    gate = Gate(raw, quiet=True)
    gate.run()
    return {c.name: c.status for c in gate.checks}


def corrupt(clean, tmp_path, source: str, mutate) -> object:
    """Copy the raw layer, break one feed, and return the gate's verdict."""
    broken = tmp_path / "broken"
    shutil.copytree(clean, broken)
    for path in sorted((broken / source).glob("dt=*/*.parquet"))[:3]:
        frame = pd.read_parquet(path)
        mutate(frame).to_parquet(path, index=False)
    return statuses(broken)


# =============================================================== happy path
def test_a_clean_raw_layer_passes_every_check(clean) -> None:
    gate = Gate(clean, quiet=True)
    gate.run()
    failures = [c for c in gate.checks if not c.ok]
    assert not failures, [f"{c.name}: {c.detail}" for c in failures]
    assert gate.report() is True


def test_the_gate_runs_both_kinds_of_check(clean) -> None:
    gate = Gate(clean, quiet=True)
    gate.run()
    kinds = {c.kind for c in gate.checks}
    assert kinds == {"invariant", "deviation"}
    assert sum(c.kind == "invariant" for c in gate.checks) >= 8


def test_fefo_is_actually_verified_not_skipped(clean) -> None:
    """A sampled check that samples nothing would pass silently."""
    gate = Gate(clean, quiet=True)
    gate.check_fefo_order_is_respected()
    check = gate.checks[0]
    assert check.status == PASS
    sampled = int(check.detail.split(" of ")[1].split()[0].replace(",", ""))
    assert sampled > 500, f"only {sampled} sales replayed"


# =============================================================== it can fail
def test_negative_stock_is_caught(clean, tmp_path) -> None:
    def mutate(frame):
        frame.loc[frame.index[:50], "qty_delta"] = -99_999
        return frame

    assert (
        corrupt(clean, tmp_path, "wms_inventory_movement", mutate)[
            "stock never goes negative (intact batches)"
        ]
        == FAIL
    )


def test_selling_expired_stock_is_caught(clean, tmp_path) -> None:
    def mutate(frame):
        frame.loc[frame.index[:20], "dte_at_sale"] = -3
        return frame

    assert (
        corrupt(clean, tmp_path, "pos_order_items", mutate)["no expired stock is ever sold"] == FAIL
    )


def test_cross_store_fulfilment_is_caught(clean, tmp_path) -> None:
    def mutate(frame):
        frame.loc[frame.index[:30], "store_id"] = "FF-XXX-99"
        return frame

    assert (
        corrupt(clean, tmp_path, "wms_inventory_batch", mutate)[
            "stock is only sold by the store that held it"
        ]
        == FAIL
    )


def test_an_order_line_with_no_header_is_caught(clean, tmp_path) -> None:
    """Deleting order headers must go red, not quietly shrink the join."""

    def mutate(frame):
        return frame.iloc[25:]

    assert (
        corrupt(clean, tmp_path, "pos_orders", mutate)["every order line resolves to a real order"]
        == FAIL
    )


def test_an_orphaned_batch_reference_is_caught(clean, tmp_path) -> None:
    def mutate(frame):
        frame.loc[frame.index[:25], "batch_id"] = "BAT-DOES-NOT-EXIST"
        return frame

    assert (
        corrupt(clean, tmp_path, "pos_order_items", mutate)[
            "every order line resolves to a real batch"
        ]
        == FAIL
    )


def test_an_impossible_batch_date_is_caught(clean, tmp_path) -> None:
    def mutate(frame):
        frame.loc[frame.index[:10], "expiry_date"] = frame.loc[frame.index[:10], "received_date"]
        return frame

    assert (
        corrupt(clean, tmp_path, "wms_inventory_batch", mutate)[
            "batch dates and quantities are coherent"
        ]
        == FAIL
    )


def test_a_delivery_arriving_before_it_was_ordered_is_caught(clean, tmp_path) -> None:
    def mutate(frame):
        frame.loc[frame.index[:10], "received_date"] = pd.Timestamp("2020-01-01").date()
        return frame

    assert (
        corrupt(clean, tmp_path, "wms_purchase_orders", mutate)[
            "deliveries arrive after they were ordered"
        ]
        == FAIL
    )


def test_a_missing_partition_is_caught(clean, tmp_path) -> None:
    broken = tmp_path / "gap"
    shutil.copytree(clean, broken)
    shutil.rmtree(sorted((broken / "pos_order_items").glob("dt=*"))[3])
    assert statuses(broken)["core feeds have a partition for every day"] == FAIL


def test_out_of_order_fefo_is_caught(clean, tmp_path) -> None:
    """Invert the clock and the check must notice.

    The replay reads the sales feed, so the way to break FEFO is to make stock
    leave in the wrong sequence: reversing each day's timestamps means the last
    basket of the evening is now the first of the morning, and the batches it
    took are no longer the soonest-expiring ones available.
    """
    broken = tmp_path / "fefo"
    shutil.copytree(clean, broken)
    for path in sorted((broken / "pos_orders").glob("dt=*/*.parquet")):
        frame = pd.read_parquet(path)
        frame["order_ts"] = frame["order_ts"].sort_values(ascending=False).to_numpy()
        frame.to_parquet(path, index=False)
    assert statuses(broken)["FEFO order is respected"] == FAIL


# =============================================================== deviations
def test_documented_defects_are_explained_not_failed(clean, tmp_path) -> None:
    """The point of the two-tier design: injected damage must reconcile against
    the defect log rather than simply failing."""
    dirty = tmp_path / "dirty"
    shutil.copytree(clean, dirty)
    DirtInjector(dirty, seed=42, quiet=True, min_partitions_for_outage=DAYS).apply()

    gate = Gate(dirty, quiet=True)
    gate.run()
    deviations = [c for c in gate.checks if c.kind == "deviation"]
    assert any(c.status == EXPLAINED for c in deviations), (
        "no deviation was explained - the defect log is not being consulted"
    )
    assert all(c.ok for c in deviations), [f"{c.name}: {c.detail}" for c in deviations if not c.ok]


def test_damage_beyond_the_documented_amount_still_fails(clean, tmp_path) -> None:
    """The failure mode a plain 'does it reconcile?' check would hide: dirt that
    is real, but far more of it than the log accounts for."""
    dirty = tmp_path / "extra"
    shutil.copytree(clean, dirty)
    DirtInjector(dirty, seed=42, quiet=True, min_partitions_for_outage=DAYS).apply()

    for path in sorted((dirty / "wms_inventory_movement").glob("dt=*/*.parquet")):
        frame = pd.read_parquet(path)
        frame.loc[frame.index[: len(frame) // 3], "batch_id"] = None
        frame.to_parquet(path, index=False)

    assert statuses(dirty)["movements with no batch match the defect log"] == FAIL


def test_the_gate_without_a_defect_log_expects_a_pristine_layer(clean, tmp_path) -> None:
    dirty = tmp_path / "nolog"
    shutil.copytree(clean, dirty)
    DirtInjector(dirty, seed=42, quiet=True, min_partitions_for_outage=DAYS).apply()
    shutil.rmtree(dirty.parent / "_manifest")

    gate = Gate(dirty, quiet=True)
    gate.run()
    assert any(c.status == FAIL for c in gate.checks if c.kind == "deviation"), (
        "with no defect log, injected damage should read as unexplained"
    )
