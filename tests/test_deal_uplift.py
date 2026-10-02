"""The uplift model and its evaluation (task D4).

**Qini is tested before the model is trusted.** The D4 gate is "beats random on
Qini", so a Qini implementation that quietly returns a positive number for noise
would pass the gate while proving nothing. The three synthetic tests below pin
it down: a known effect must register, pure noise must not, and an unbalanced
holdout must not be mistaken for an effect.

**The leakage test is the other load-bearing one.** D1 was amended for D4
specifically so features could come from a period before assignment. If that
boundary ever slips, the model trains on outcomes of the treatment and every
number downstream becomes meaningless while looking better than ever.

    python -m pytest tests/test_deal_uplift.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from analytics.deal.uplift import PANEL, qini

ROOT = Path(__file__).resolve().parent.parent


# ================================================== Qini
def test_qini_registers_an_effect_it_should_find() -> None:
    """Half the population responds, and the model ranks them first."""
    rng = np.random.default_rng(0)
    n = 4000
    responder = np.zeros(n, dtype=bool)
    responder[: n // 2] = True
    treated = (rng.random(n) < 0.8).astype(int)
    # responders gain 10 when treated; nobody else moves
    outcome = rng.normal(100, 5, n) + responder * treated * 10
    # a perfect ranker puts responders first
    uplift = responder.astype(float) + rng.random(n) * 1e-6

    result = qini(uplift, treated, outcome)
    assert result.beats_random, f"Qini {result.coefficient:.2f} on a clearly rankable effect"


def test_qini_does_not_reward_noise() -> None:
    """A random ranking must not look like a model.

    Run over many seeds rather than one: a single draw can land either side of
    zero, and a test that passes on a lucky seed is worse than no test.
    """
    rng = np.random.default_rng(1)
    coefficients = []
    for _ in range(40):
        n = 3000
        treated = (rng.random(n) < 0.8).astype(int)
        outcome = rng.normal(100, 5, n) + treated * 3  # uniform effect, nothing to rank
        coefficients.append(qini(rng.random(n), treated, outcome).coefficient)

    mean = float(np.mean(coefficients))
    sd = float(np.std(coefficients, ddof=1))
    standard_error = sd / len(coefficients) ** 0.5
    assert abs(mean) < 3 * standard_error, (
        f"random rankings average a Qini of {mean:.3f} (SE {standard_error:.3f}) over "
        f"{len(coefficients)} seeds. A metric that rewards noise cannot carry the D4 gate."
    )


def test_qini_rescales_for_an_unbalanced_holdout() -> None:
    """80/20 is not 50/50, and a raw difference of sums would say it was.

    With four times as many treated as control, comparing cumulative sums
    without rescaling by the arm ratio inside each cut reports a large effect on
    data that has none.
    """
    rng = np.random.default_rng(2)
    n = 5000
    treated = (rng.random(n) < 0.8).astype(int)
    outcome = rng.normal(50, 1, n)  # identical in both arms: zero true effect
    result = qini(rng.random(n), treated, outcome)
    # the total gain at 100% targeting should be near zero, not near 0.6 * sum
    total_gain = float(result.curve["gain"].iloc[-1])
    assert abs(total_gain) < 0.02 * float(outcome.sum()), (
        f"gain at full targeting is {total_gain:,.0f} on a zero-effect population - "
        f"the arm ratio is not being applied"
    )


# ================================================== leakage
def test_features_are_drawn_only_from_before_assignment() -> None:
    """The boundary D1 was amended to create, asserted on the SQL itself.

    A unit test cannot catch this on data - a leaked feature makes the model
    look better, not worse - so the check is that the pre-period CTE filters
    strictly before the assignment date and the post-period strictly on or
    after it.
    """
    pre = PANEL[PANEL.index("pre as (") : PANEL.index("post as (")]
    post = PANEL[PANEL.index("post as (") : PANEL.index("select\n    e.deal_arm")]
    assert "where i.date_day < a.d" in pre, "pre-period features are not bounded by assignment"
    assert "where i.date_day >= a.d" in post, "post-period outcomes are not bounded by assignment"
    assert "signup_date < a.d" in PANEL, (
        "customers who signed up after assignment have no pre-period and must be excluded"
    )


def test_no_post_treatment_column_is_offered_as_a_feature() -> None:
    """mart_customer_360's RFM would be the easy, wrong choice."""
    from analytics.deal.uplift import FEATURES

    forbidden = {
        "margin_post",
        "orders_post",
        "churned",
        "r_score",
        "f_score",
        "m_score",
        "rfm_segment",
        "discount_dependency_index",
        "orders_90d",
        "recency_days",
    }
    leaked = forbidden & set(FEATURES)
    assert not leaked, f"post-treatment columns offered as features: {sorted(leaked)}"


def test_every_feature_ends_in_pre_or_is_static() -> None:
    """A naming convention doing real work: anything time-varying says `_pre`."""
    from analytics.deal.uplift import FEATURES

    static = {"is_member", "tenure_days", "acquisition_channel", "device", "home_store_id"}
    for name in FEATURES:
        assert name in static or name.endswith("_pre"), (
            f"`{name}` is neither static nor marked as pre-assignment. If it is time-varying "
            f"and unmarked, it is probably leaking."
        )


# ================================================== the ceiling
@pytest.mark.needs_warehouse
def test_the_effect_is_there_even_though_no_estimator_finds_it() -> None:
    """An oracle bounds what targeting could achieve, so the null means something.

    D4's three estimators all lose to random. That is reported as the finding,
    but a null is only informative next to a ceiling: without one, "no model
    worked" and "there was nothing to find" are indistinguishable, and they have
    opposite implications for what to do next.

    This test may import the simulator because tests are outside the boundary
    `test_import_boundary.py` enforces on `analytics/`. It uses the latent
    segment that no production code can see, to show the signal exists.
    """
    import os

    import duckdb as ddb

    from analytics.deal.uplift import SEED, TEST_FRACTION, build_panel, qini

    warehouse = Path(
        os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
    )
    if not warehouse.exists():
        pytest.skip(f"no warehouse at {warehouse}")

    con = ddb.connect(str(warehouse), read_only=True)
    try:
        panel = build_panel(con)
    except Exception:
        pytest.skip("this build predates the D1 holdout")
    finally:
        con.close()

    from simulator.config_loader import load_sim_config
    from simulator.customers import CustomerBase

    base = CustomerBase(load_sim_config(), seed=42)
    segment = dict(zip(base.df["customer_id"], base._segment_idx, strict=True))

    rng = np.random.default_rng(SEED)
    held = rng.random(len(panel)) < TEST_FRACTION
    train, score = panel[~held].copy(), panel[held].copy()
    train["seg"] = train["customer_id"].map(segment)
    score["seg"] = score["customer_id"].map(segment)

    lift = train.groupby("seg").apply(
        lambda g: (
            g[g["treated"] == 1]["margin_post"].mean() - g[g["treated"] == 0]["margin_post"].mean()
        ),
        include_groups=False,
    )
    oracle = score["seg"].map(lift).to_numpy()
    result = qini(oracle, score["treated"].to_numpy(), score["margin_post"].to_numpy())
    assert result.beats_random, (
        f"even with the true segment the Qini is {result.coefficient:,.0f}. If this ever "
        f"fails, the heterogeneity has gone out of the simulation and D4's null stops "
        f"being a statement about estimators."
    )
