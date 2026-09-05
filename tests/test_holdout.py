"""The holdout design holds its two assumptions (task S5.2).

A difference-in-differences over a store-level holdout rests on two things, and
neither is guaranteed by writing the estimator correctly:

  1. **A clean pre-period.** Before the switch date every store, treated or not,
     must run exactly the baseline. If the groups differ beforehand, the
     parallel-trends check is testing the harness rather than the world.
  2. **SUTVA.** No treated store may change a holdout store's outcome. Policy B
     moves stock between stores, so this is a real hazard rather than a
     formality, and it is enforced by dropping any transfer whose ends fall in
     different groups.

The second is the one worth writing carefully, because a filter that never fires
is indistinguishable from a filter that works. The test below constructs arcs
that *must* be dropped rather than hoping the optimiser proposes one.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

import numpy as np
import pytest

from simulator.config_loader import load_sim_config
from simulator.policies.base import PolicyContext
from simulator.policies.baseline import BaselinePolicy
from simulator.policies.holdout import HoldoutPolicy, assign_treatment
from simulator.run import SimulationRun

cfg = load_sim_config()
SEED = 7


@pytest.fixture(scope="module")
def world():
    run = SimulationRun(cfg, seed=SEED, days=1, out_dir=Path(tempfile.gettempdir()))
    days = sorted(run.demand.factors)
    return run, days


@pytest.fixture(scope="module")
def policy(world):
    run, days = world
    treated = assign_treatment(run.S, seed=SEED)
    return HoldoutPolicy(cfg, run.catalog, treated, switch_date=days[10]), treated, days


def _context(run, day: dt.date) -> PolicyContext:
    rng = np.random.default_rng(0)
    return PolicyContext(
        date=day,
        on_hand=rng.integers(0, 40, (run.S, run.K)).astype(np.int64),
        on_order=np.zeros((run.S, run.K), dtype=np.int64),
        trailing_avg=rng.gamma(2.0, 1.5, (run.S, run.K)),
        min_dte=rng.integers(0, 12, (run.S, run.K)),
        store_open=np.ones(run.S, dtype=bool),
        catalog=run.catalog,
        rng=rng,
    )


# ============================================================ the pre-period
def test_before_the_switch_every_store_runs_the_baseline(world, policy) -> None:
    """Otherwise parallel trends is a property of the harness, not of the world.

    The pre-period exists so that a difference between the groups can be
    observed before the treatment rather than assumed away. If the treated
    stores were already on Policy B for any part of it, the check would compare
    two groups that were never alike and would still, misleadingly, pass or fail
    on its own terms.
    """
    run, days = world
    holdout, _, _ = policy
    baseline = BaselinePolicy(cfg, run.catalog)
    ctx = _context(run, days[0])

    assert np.array_equal(holdout.markdown(ctx), baseline.markdown(ctx))
    assert holdout.deal_slots(ctx) == baseline.deal_slots(ctx)
    assert holdout.transfers(ctx) == baseline.transfers(ctx)

    mine, theirs = holdout.replenish(ctx), baseline.replenish(ctx)
    assert np.array_equal(np.sort(mine.store_idx), np.sort(theirs.store_idx))
    assert mine.qty.sum() == theirs.qty.sum()


def test_after_the_switch_only_treated_stores_change(world, policy) -> None:
    """The holdout must be bit-identical to the baseline it is standing in for."""
    run, days = world
    holdout, treated, _ = policy
    baseline = BaselinePolicy(cfg, run.catalog)
    ctx = _context(run, days[20])

    control = np.setdiff1d(np.arange(run.S), treated)
    assert np.array_equal(holdout.markdown(ctx)[control], baseline.markdown(ctx)[control])

    holdout_slots = holdout.deal_slots(ctx)
    baseline_slots = baseline.deal_slots(ctx)
    for store in control:
        assert holdout_slots.get(int(store), []) == baseline_slots.get(int(store), [])


# ============================================================ SUTVA
def test_no_transfer_ever_crosses_the_groups(world, policy) -> None:
    """The assumption the whole control group depends on.

    A transfer out of a treated store into a holdout store hands the holdout
    inventory it would never have had. The control is then partly treated, the
    estimate is biased toward zero, and - worse than the size of the bias - it
    is invisible: the output shows a smaller effect, not a broken design.
    """
    run, days = world
    holdout, treated, _ = policy
    treated_set = set(treated.tolist())
    ctx = _context(run, days[20])

    for from_store, to_store, _sku, _qty in holdout.transfers(ctx):
        assert (from_store in treated_set) == (to_store in treated_set), (
            f"transfer {from_store} -> {to_store} crosses the treatment boundary"
        )


def test_the_sutva_filter_actually_drops_something(world, policy, monkeypatch) -> None:
    """A filter that never fires is indistinguishable from one that works.

    The optimiser proposes few transfers - three across the whole estate on the
    full-year warehouse - so a run in which none happens to cross the boundary
    would let the test above pass over an empty list forever. This hands the
    policy arcs that must be dropped, and checks the ones that must survive do.

    Same failure this project has hit three times: G4's sweep at an inelastic
    coefficient, the private-label floor at int(0.30 * 3), and S5.1's original
    CRN gate. A gate that can pass without its mechanism firing is not a gate.
    """
    run, days = world
    holdout, treated, _ = policy
    treated_set = sorted(treated.tolist())
    control = sorted(set(range(run.S)) - set(treated_set))
    assert treated_set and control, "the split must have both groups for this to mean anything"

    t0, t1 = treated_set[0], treated_set[1]
    c0, c1 = control[0], control[1]
    proposed = [
        (t0, c0, 0, 5),  # treated -> holdout: must be dropped
        (c0, t0, 1, 5),  # holdout -> treated: must be dropped
        (t0, t1, 2, 5),  # within treated: must survive
        (c0, c1, 3, 5),  # within holdout: must survive
    ]
    monkeypatch.setattr(holdout.optimized, "transfers", lambda ctx: proposed)

    kept = holdout.transfers(_context(run, days[20]))
    assert (t0, t1, 2, 5) in kept and (c0, c1, 3, 5) in kept, "a within-group arc was dropped"
    assert (t0, c0, 0, 5) not in kept and (c0, t0, 1, 5) not in kept, (
        "a cross-group transfer survived - the holdout is now partly treated"
    )
    assert len(kept) == 2


# ============================================================ assignment
def test_treatment_is_randomised_and_splits_the_estate(world) -> None:
    """Chosen stores would differ in ways that predict the outcome.

    Picking the busiest or the worst-performing stores makes the groups
    non-comparable in exactly the dimension being measured, and differencing
    does not repair that - it only removes what is common to both.
    """
    run, _ = world
    treated = assign_treatment(run.S, seed=SEED)
    assert 0 < len(treated) < run.S, "one group is empty"
    assert len(set(treated.tolist())) == len(treated), "a store was treated twice"
    assert (treated < run.S).all() and (treated >= 0).all()


def test_the_assignment_does_not_move_when_the_world_does(world) -> None:
    """Its generator is seeded apart from the simulation's, on purpose.

    Sharing a stream would mean that changing the world also reshuffles who was
    treated, so a difference between two seeds could be the world or the split
    and nothing in the output would say which.
    """
    run, _ = world
    first = assign_treatment(run.S, seed=SEED)
    second = assign_treatment(run.S, seed=SEED)
    assert np.array_equal(first, second), "the assignment is not reproducible from its seed"
    assert not np.array_equal(first, assign_treatment(run.S, seed=SEED + 1)), (
        "every seed produces the same split, so the randomisation does nothing"
    )
