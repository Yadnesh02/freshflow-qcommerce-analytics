"""Common random numbers hold, and the gate that says so is not vacuous (task S5.1).

The plan's wording for this gate was "same seed reproduces identical demand
under both policies - assert it", and that assertion cannot be made. Policy B
changes prices, demand is price-elastic, so identical demand across arms would
prove only that the policy did nothing. Worse, it would have *passed*: with the
fitted elasticities the markdown optimiser recommends no markdown at all, and an
inert arm produces exactly the equality the gate asked for.

That is the third gate in this project that could pass without its mechanism
firing - after G4's monotonicity sweep at an inelastic coefficient, and the
private-label floor at `int(0.30 * 3)`. So the gate here is three assertions
rather than one:

  1. the exogenous draws are identical across arms - the real CRN property;
  2. the arms nonetheless produce different outcomes, so the test cannot pass
     by the policy being inert;
  3. a component's draws do not move when another component consumes more,
     which is the specific failure that made CRN hold for exactly one day.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from simulator.config_loader import load_sim_config
from simulator.experiment import ARMS, common_random_numbers_hold, run_arm
from simulator.run import SimulationRun

SEED = 7
SCRATCH = Path(tempfile.gettempdir())

EXOGENOUS = (
    SimulationRun.DEMAND,
    SimulationRun.BASKETS,
    SimulationRun.FULFIL,
    SimulationRun.CLICKS,
    SimulationRun.SUPPLY,
)


@pytest.fixture(scope="module")
def runs():
    cfg = load_sim_config()
    return {
        arm: SimulationRun(cfg, seed=SEED, days=1, out_dir=SCRATCH, policy_name=arm) for arm in ARMS
    }


def test_both_arms_draw_the_same_exogenous_numbers(runs) -> None:
    """The property the experiment rests on, checked component by component.

    Not "the arms produce the same demand" - they must not - but "the arms are
    handed the same random numbers". Everything downstream of that is the
    policy, which is the only thing the experiment is entitled to attribute a
    difference to.
    """
    baseline, optimized = runs["baseline"], runs["optimized"]
    for day in (738000, 738120, 738300):  # arbitrary, spread across a year
        for component in EXOGENOUS:
            a = baseline._substream(day, component).random(16)
            b = optimized._substream(day, component).random(16)
            assert (a == b).all(), f"component {component} differs between arms on ordinal {day}"


def test_a_components_draws_do_not_move_when_another_consumes_more(runs) -> None:
    """The exact failure that made CRN hold for one day and no longer.

    Every component used to draw from one shared generator, and basket assembly
    and fulfilment consume a number of draws that depends on the data. Two arms
    diverge in that count on day one, so day two's demand came from a different
    stream position. Here the demand stream is asked for its numbers, then a
    different component is drained hard, then demand is asked again - and it has
    to answer identically.
    """
    run = runs["baseline"]
    before = run._substream(738120, SimulationRun.DEMAND).random(16)

    drained = run._substream(738120, SimulationRun.BASKETS)
    drained.random(1_000_000)

    after = run._substream(738120, SimulationRun.DEMAND).random(16)
    assert (before == after).all(), (
        "demand's draws moved because another component consumed more - the streams are shared"
    )


def test_each_component_is_a_different_stream(runs) -> None:
    """Independent, not merely separate.

    Deriving every component from the same (seed, day) with the component id
    ignored would satisfy the two tests above and hand every component the same
    numbers, which is a different bug with the same symptoms.
    """
    run = runs["baseline"]
    draws = {c: tuple(run._substream(738120, c).random(8)) for c in EXOGENOUS}
    assert len(set(draws.values())) == len(EXOGENOUS), "two components share a stream"


def test_the_days_are_different_streams_too(runs) -> None:
    """A per-day substream that ignores the day would freeze demand across the year."""
    run = runs["baseline"]
    days = [tuple(run._substream(d, SimulationRun.DEMAND).random(8)) for d in (738000, 738001)]
    assert days[0] != days[1], "consecutive days draw identical numbers"


def test_the_harness_reports_common_random_numbers_holding() -> None:
    """The check the harness runs before spending an hour of runner time."""
    assert common_random_numbers_hold(SEED, 738120)


@pytest.mark.slow
def test_the_arms_actually_diverge(tmp_path) -> None:
    """The vacuity guard, and the reason the plan's original gate was unusable.

    If this fails, every CRN assertion above is still true and still worthless:
    two arms that behave identically share their draws trivially. It is the same
    shape as G4 passing at an inelastic coefficient, where every markdown depth
    was zero and "non-decreasing" held over a flat line.

    Eight days is enough to see it - the deal-slot allocator diverges from the
    central pick on day one - and short enough to keep in the suite.
    """
    baseline = run_arm(SEED, 8, "baseline")
    optimized = run_arm(SEED, 8, "optimized")

    expired = (baseline["units_expired"].sum(), optimized["units_expired"].sum())
    assert expired[0] != expired[1], (
        f"both arms expired {expired[0]:,} units - the policies are not doing different things, "
        "so the common-random-numbers assertions above prove nothing"
    )
    assert not np.array_equal(
        baseline["units_demanded"].to_numpy(), optimized["units_demanded"].to_numpy()
    ), "the arms saw identical demand, which means the policy changed no price"
