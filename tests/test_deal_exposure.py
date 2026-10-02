"""The deal rail's customer-level holdout (task D1).

Sprint 5 randomises STORES, which answers "what if we rolled this out to half
the estate". It cannot answer "who is this worth showing to", because every
customer in a treated store is treated. D4's uplift model needs a control group
at the grain the decision is made at, and that grain is the customer.

**What this file checks is assignment, not effect.** Whether the rail pays is
D2's question and whether it pays differently per customer is D4's. The only
thing that has to be true here is that the two arms were comparable before the
rail touched either of them, and that the rail then touched exactly one.

**Balance is checked on pre-treatment attributes only, and that is not a
weakening.** Signup date, segment, home store, acquisition channel, device and
membership are all fixed before assignment, so an imbalance in them is a broken
randomisation. RFM is the opposite: recency and frequency are what the rail is
supposed to move, so requiring them to balance after exposure would be
requiring the treatment not to work. The plan's gate originally said "RFM
features", which was wrong for that reason and is corrected here.

    python -m pytest tests/test_deal_exposure.py
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from simulator.config_loader import load_sim_config
from simulator.customers import CustomerBase

ROOT = Path(__file__).resolve().parent.parent
RAW = Path(os.environ.get("FRESHFLOW_RAW_DIR", ROOT / "data" / "raw"))

# Cohen's convention: below 0.10 is negligible imbalance, and it is the
# threshold trial reporting uses for a covariate table. Tighter would fail on
# sampling noise alone at this sample size; looser would let a real tilt through.
MAX_SMD = 0.10


@pytest.fixture(scope="module")
def base() -> CustomerBase:
    return CustomerBase(load_sim_config(), seed=42)


def _smd(treated: np.ndarray, control: np.ndarray) -> float:
    """Standardised mean difference, pooled-SD form.

    Zero variance in both arms means the covariate is constant and therefore
    perfectly balanced - returning 0 rather than dividing by it.
    """
    pooled = np.sqrt((treated.var(ddof=1) + control.var(ddof=1)) / 2)
    if pooled == 0:
        return 0.0
    return float(abs(treated.mean() - control.mean()) / pooled)


# ================================================== assignment
def test_the_holdout_is_the_share_the_config_asked_for(base: CustomerBase) -> None:
    """Within sampling noise of 20%, not exactly 20%.

    The assignment is an independent draw per customer rather than a shuffled
    partition, because a partition would make one customer's arm depend on
    another's - which is not how a real flag is set, and would bite as soon as
    the customer base grows mid-run.
    """
    share = base.cfg.deal_holdout_share
    realised = 1.0 - base.exposed_share()
    n = len(base.df)
    tolerance = 4 * np.sqrt(share * (1 - share) / n)
    assert abs(realised - share) < tolerance, (
        f"holdout is {realised:.4f}, config asked for {share} "
        f"(tolerance {tolerance:.4f} at n={n:,})"
    )


def test_assignment_is_stable_for_a_seed(base: CustomerBase) -> None:
    """A rebuild must not move customers between arms.

    Every retention figure D3 produces is a comparison across these two groups
    over months. If a rebuild reshuffled them, the pre-period and the post-period
    would describe different populations and the difference-in-differences would
    be measuring the reshuffle.
    """
    again = CustomerBase(load_sim_config(), seed=42)
    assert np.array_equal(base._deal_exposed, again._deal_exposed)


def test_a_different_seed_assigns_differently(base: CustomerBase) -> None:
    """Otherwise the previous test passes for the wrong reason."""
    other = CustomerBase(load_sim_config(), seed=7)
    assert not np.array_equal(base._deal_exposed, other._deal_exposed)


def test_the_holdout_does_not_disturb_the_customer_master() -> None:
    """Assignment draws from its own stream, so it shifts nothing upstream.

    The customer master is generated from `[seed, 7001]` and the arm from
    `[seed, 7002]`. If the arm had been drawn from the generation stream, adding
    this holdout would have silently changed every customer's segment, signup
    date and home store - and every historical figure in the repo with them.
    """
    expected = np.random.default_rng([42, 7002]).random(48770) >= 0.20
    base = CustomerBase(load_sim_config(), seed=42)
    assert len(base.df) == 48770, "customer count moved - the master was disturbed"
    assert np.array_equal(base._deal_exposed, expected)


# ================================================== balance
@pytest.mark.parametrize(
    "covariate",
    ["_segment_idx", "_home_store_idx", "signup_ordinal", "is_member"],
)
def test_pre_treatment_covariates_are_balanced(base: CustomerBase, covariate: str) -> None:
    """The gate. An imbalance here is a broken randomisation, not a finding."""
    if covariate == "signup_ordinal":
        values = base._signup_ord.astype(float)
    elif covariate == "is_member":
        values = base.df["is_member"].to_numpy().astype(float)
    else:
        values = base.df[covariate].to_numpy().astype(float)

    exposed = values[base._deal_exposed]
    holdout = values[~base._deal_exposed]
    smd = _smd(exposed, holdout)
    assert smd < MAX_SMD, (
        f"{covariate} is imbalanced across arms: SMD {smd:.4f} >= {MAX_SMD}. "
        f"exposed mean {exposed.mean():.4f} (n={exposed.size:,}), "
        f"holdout mean {holdout.mean():.4f} (n={holdout.size:,})"
    )


def test_every_segment_appears_in_both_arms(base: CustomerBase) -> None:
    """A segment missing from the holdout has no counterfactual.

    D4 ranks customers by predicted uplift. A segment present only in the
    treated arm would get a prediction with nothing behind it, and the Qini
    curve would be scored on an extrapolation.
    """
    for idx, name in enumerate(base.segment_names):
        in_arm = base._segment_idx == idx
        assert (in_arm & base._deal_exposed).sum() > 0, f"{name} absent from the exposed arm"
        assert (in_arm & ~base._deal_exposed).sum() > 0, f"{name} absent from the holdout"


def test_the_response_is_heterogeneous(base: CustomerBase) -> None:
    """A uniform response would make the uplift chapter pointless.

    If every segment moved the same amount, the best targeting policy would be
    "everyone or nobody" and ranking customers would add nothing. The spread is
    what D4 has to find - and it has to find it from behaviour, because the
    segment that generated it is stripped before anything is emitted.
    """
    response = base.segment_response()
    spread = float(response.max() - response.min())
    # The realised population mean, not the config's unweighted one: a wide
    # spread concentrated in a 2% segment would not be worth targeting on.
    mean_excess = abs(float(response[base._segment_idx].mean()) - 1.0)

    # Relative, not absolute. D2 recalibrated these down by 6.5x because the
    # first pass made the rail return 25x and the targeting question moot; an
    # absolute threshold would have failed that fix for being too small, when
    # what actually matters is that the spread dwarfs the average effect. It is
    # the gap between segments, not the level, that makes ranking customers pay.
    assert spread > 4 * mean_excess, (
        f"deal response spans {response.min():.3f}-{response.max():.3f} (spread {spread:.3f}) "
        f"against a mean effect of {mean_excess:.3f}. Too flat relative to the average for "
        f"targeting to beat showing it to everyone."
    )
    assert (response < 1.0).any(), (
        "no segment responds negatively - the holdout can never find the effect "
        "every deal programme claims does not exist"
    )


def test_the_segment_never_reaches_the_source_feed(base: CustomerBase) -> None:
    """Ground truth stays out of the data the model is allowed to see."""
    for frame in (base.to_bronze(), base.to_exposure_bronze()):
        leaked = [c for c in frame.columns if c.startswith("_")]
        assert not leaked, f"latent columns emitted as source data: {leaked}"
    assert "deal_arm" in base.to_exposure_bronze().columns


# ================================================== the rail respects the arm
@pytest.mark.needs_warehouse
def test_no_deal_line_ever_reaches_a_held_out_customer() -> None:
    """The invariant the whole design rests on.

    If one deal-priced line lands on a holdout customer, the control group is
    contaminated and every number D2 and D4 produce is measuring a blend of the
    two arms rather than the difference between them.
    """
    exposure = RAW / "crm_deal_exposure"
    if not exposure.exists():
        pytest.skip(f"no exposure feed at {exposure} - run `python tasks.py simulate`")
    duckdb = pytest.importorskip("duckdb")

    items = (RAW / "pos_order_items").as_posix()
    orders = (RAW / "pos_orders").as_posix()
    arms = exposure.as_posix()

    con = duckdb.connect()
    try:
        # The burn-in runs the rail for EVERYONE, so a held-out customer having
        # deal lines before their assignment date is the design working, not a
        # leak. Bounding on that date is the whole correctness of this check -
        # without it this test fails on any build whose window is shorter than
        # the burn-in, which is exactly what CI's 30-day slice is.
        after = con.execute(
            f"""
            select count(*)
            from read_parquet('{items}/**/*.parquet') i
            join read_parquet('{orders}/**/*.parquet') o using (order_id)
            join read_parquet('{arms}/**/*.parquet') e using (customer_id)
            where cast(o.order_ts as date) >= e.assigned_date
            """
        ).fetchone()[0]
        if after == 0:
            pytest.skip(
                "this build ends before the holdout starts - the rail ran for everyone "
                "throughout, so there is no arm difference to violate yet"
            )

        leaked = con.execute(
            f"""
            select count(*)
            from read_parquet('{items}/**/*.parquet') i
            join read_parquet('{orders}/**/*.parquet') o using (order_id)
            join read_parquet('{arms}/**/*.parquet') e using (customer_id)
            where i.promo_id = 'PROMO-DEAL11'
              and e.deal_arm = 'holdout'
              and cast(o.order_ts as date) >= e.assigned_date
            """
        ).fetchone()[0]
    finally:
        con.close()

    assert leaked == 0, (
        f"{leaked:,} deal-priced lines reached held-out customers after assignment. The "
        f"control arm is contaminated and no uplift estimate from this build can be trusted."
    )
