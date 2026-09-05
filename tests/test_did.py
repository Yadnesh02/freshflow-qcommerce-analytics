"""The DiD estimator recovers a known effect, and its trends check can fail (task S5.2).

Tested against constructed panels rather than simulator output, because the
point is whether the estimator is right, and a test that runs the simulator
would only tell us the two agree with each other.

The parallel-trends check gets the most attention. A check that cannot fail is
the failure this project has now hit four times - G4's sweep at an inelastic
coefficient, the private-label floor at `int(0.30 * 3)`, S5.1's original CRN
gate, and the SUTVA filter with no cross-boundary arc to drop. A trends check
that passes on every input would let a broken design through while looking like
diligence, so there is a test that builds diverging groups and requires it to
say so.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analytics.experiment.did import estimate, seed_did, seed_pre_trend_gap, trends_are_parallel

PRE_DAYS, POST_DAYS, STORES = 20, 40, 14


def panel(
    effect: float = 0.0,
    treated_slope: float = 0.0,
    control_slope: float = 0.0,
    level_gap: float = 0.0,
    seeds: int = 6,
    noise: float = 0.0,
    metric: str = "units_expired",
) -> pd.DataFrame:
    """A synthetic holdout panel with a known effect and known pre-period slopes."""
    rng = np.random.default_rng(0)
    rows = []
    for seed in range(seeds):
        for store in range(STORES):
            treated = store < STORES // 2
            for day_rel in range(-PRE_DAYS, POST_DAYS):
                post = day_rel >= 0
                slope = treated_slope if treated else control_slope
                value = 100.0 + level_gap * treated + slope * day_rel
                if post and treated:
                    value += effect
                if noise:
                    value += rng.normal(0, noise)
                rows.append(
                    {
                        "seed": seed,
                        "store_id": f"S{store:02d}",
                        "day_rel": day_rel,
                        "post": post,
                        "treated": treated,
                        metric: value,
                        "revenue": value,
                        "cogs": 0.0,
                    }
                )
    return pd.DataFrame(rows)


# ==================================================== the estimate
def test_a_known_effect_is_recovered_exactly() -> None:
    """No noise, no trend: the estimator must return the number that was put in."""
    frame = panel(effect=-7.5)
    assert seed_did(frame[frame["seed"] == 0], "units_expired") == pytest.approx(-7.5)
    assert estimate(frame, "units_expired").effect == pytest.approx(-7.5)


def test_a_level_difference_between_groups_is_not_an_effect() -> None:
    """The whole reason for differencing twice.

    If the treated stores are simply bigger, a post-period comparison of levels
    attributes that difference to the policy. Differencing each group against its
    own pre-period removes it, and this asserts the removal is exact.
    """
    frame = panel(effect=0.0, level_gap=45.0)
    assert estimate(frame, "units_expired").effect == pytest.approx(0.0, abs=1e-9)


def test_a_shock_that_hits_both_groups_is_not_an_effect() -> None:
    """A festival, a monsoon week, a supplier failure - the thing DiD is for.

    Applied to every store in the post-period, so a before-and-after comparison
    would read it as a large effect and the difference-in-differences must read
    it as none.
    """
    frame = panel(effect=0.0)
    frame.loc[frame["post"], "units_expired"] += 60.0
    naive = (
        frame[frame["post"] & frame["treated"]]["units_expired"].mean()
        - frame[~frame["post"] & frame["treated"]]["units_expired"].mean()
    )
    assert naive == pytest.approx(60.0), "the shock is not where the test thinks it is"
    assert estimate(frame, "units_expired").effect == pytest.approx(0.0, abs=1e-9)


def test_unequal_groups_do_not_bias_the_estimate() -> None:
    """Means, not sums. An odd store count does not always split evenly.

    Differencing totals would make the estimate a function of how many stores
    landed in each arm, which the randomisation is entitled to vary.
    """
    frame = panel(effect=-5.0)
    trimmed = frame[~((frame["store_id"] == "S00") & frame["treated"])]
    assert estimate(trimmed, "units_expired").effect == pytest.approx(-5.0)


# ==================================================== parallel trends
def test_parallel_groups_are_reported_parallel() -> None:
    frame = panel(effect=-5.0, treated_slope=0.4, control_slope=0.4, noise=0.5)
    gaps = np.array([seed_pre_trend_gap(g, "units_expired") for _, g in frame.groupby("seed")])
    ok, _ = trends_are_parallel(gaps)
    assert ok, "identically sloped groups were called non-parallel"


def test_diverging_groups_are_caught() -> None:
    """The test that makes the check worth running.

    The treated group is already falling faster than the control before anything
    happens. A DiD run over this attributes the pre-existing divergence to the
    policy, and its p-value will look excellent while doing so. If this ever
    stops failing, the trends check has become decoration.
    """
    frame = panel(effect=0.0, treated_slope=-0.8, control_slope=0.2, noise=0.5)
    gaps = np.array([seed_pre_trend_gap(g, "units_expired") for _, g in frame.groupby("seed")])
    ok, p = trends_are_parallel(gaps)
    assert not ok, f"a 1.0/day divergence was called parallel (p={p:.3f})"
    assert np.nanmean(gaps) == pytest.approx(-1.0, abs=0.05)


def test_the_check_needs_enough_seeds_to_mean_anything() -> None:
    """Two seeds cannot establish anything, and must not claim to.

    Reported as not-parallel rather than parallel: the check exists to license
    the estimate below it, and licensing that on no evidence is the worse error.
    """
    ok, _ = trends_are_parallel(np.array([0.0, 0.0]))
    assert not ok


def test_a_pre_period_divergence_contaminates_the_estimate() -> None:
    """Why a failed trends check invalidates the effect rather than merely flagging it.

    Constructed with no treatment effect at all and only a pre-period
    divergence. The estimator still returns a large, confidently non-zero
    number - which is exactly why the report says an effect under a failed
    trends check is uninterpretable however small its p-value.
    """
    frame = panel(effect=0.0, treated_slope=-0.8, control_slope=0.2, noise=0.5)
    result = estimate(frame, "units_expired")
    assert abs(result.effect) > 5.0, "the divergence should show up as a spurious effect"
    assert result.p_value < 0.05, "and it should look statistically convincing"


def test_the_combined_panel_is_readable_not_just_the_per_seed_files(tmp_path) -> None:
    """The artifact anybody downloads is the merged one, and it must just work.

    `holdout.yml` writes `seed_NNN.parquet` on each matrix runner and uploads a
    merged `holdout.parquet` from the collecting job. Only the merged file
    survives an artifact download, and the first version of `load` globbed
    `seed_*` alone - so `tasks.py did` reported "no holdout runs" while looking
    directly at the output its own workflow had just produced.
    """
    from analytics.experiment.did import load

    frame = panel(effect=-5.0, seeds=4)
    (tmp_path / "holdout.parquet").write_bytes(b"")  # replaced below, just reserving the name
    frame.to_parquet(tmp_path / "holdout.parquet", index=False)

    loaded = load(tmp_path)
    assert loaded["seed"].nunique() == 4
    assert estimate(loaded, "units_expired").effect == pytest.approx(-5.0)


def test_having_both_spellings_present_does_not_double_count(tmp_path) -> None:
    """A download next to a local run must not tighten the interval by duplication.

    Concatenating the per-seed files and the merged panel would count every seed
    twice, which halves the standard error and doubles the apparent precision
    while changing no estimate - the most flattering way a bug like this could
    present itself.
    """
    from analytics.experiment.did import load

    frame = panel(effect=-5.0, seeds=4)
    frame.to_parquet(tmp_path / "holdout.parquet", index=False)
    for seed, group in frame.groupby("seed"):
        group.to_parquet(tmp_path / f"seed_{seed:03d}.parquet", index=False)

    loaded = load(tmp_path)
    assert len(loaded) == len(frame), "seeds were counted twice"
    assert loaded["seed"].nunique() == 4
