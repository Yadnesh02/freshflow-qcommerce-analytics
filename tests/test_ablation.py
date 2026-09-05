"""An ablation removes exactly one component and nothing else (task S5.4).

The whole attribution rests on a difference being caused by the one decision
that was swapped. Two ways that goes wrong quietly: the ablation swaps more than
it claims, so the difference includes something unattributed; or it swaps
nothing, so a component reads as contributing zero when it was never removed.

The second is the failure this project keeps meeting - a check that cannot fire.
An ablation of a component whose two implementations happen to agree is
indistinguishable from an ablation that did not happen, and it would report a
tidy "this component contributes nothing" either way.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from simulator.config_loader import load_sim_config
from simulator.policies.ablation import COMPONENTS, AblatedPolicy
from simulator.policies.base import PolicyContext
from simulator.policies.baseline import BaselinePolicy
from simulator.policies.optimized import NO_STOCK_DTE, OptimizedPolicy

cfg = load_sim_config()


@pytest.fixture(scope="module")
def catalog():
    from simulator.catalog import build_catalog

    return build_catalog(cfg, seed=42)


@pytest.fixture(scope="module")
def context(catalog):
    rng = np.random.default_rng(7)
    shape = (3, len(catalog))
    on_hand = rng.integers(0, 60, size=shape).astype(float)
    return PolicyContext(
        date=dt.date(2026, 3, 1),
        on_hand=on_hand,
        on_order=np.zeros(shape),
        trailing_avg=rng.gamma(2.0, 1.5, size=shape),
        min_dte=np.where(on_hand > 0, rng.integers(0, 9, size=shape), NO_STOCK_DTE).astype(float),
        store_open=np.ones(3, dtype=bool),
        catalog=catalog,
        rng=rng,
    )


def _decisions(policy, ctx) -> dict[str, object]:
    order = policy.replenish(ctx)
    return {
        "replenish": (order.store_idx.tolist(), order.sku_idx.tolist(), order.qty.tolist()),
        "markdown": policy.markdown(ctx).tolist(),
        "deal_slots": policy.deal_slots(ctx),
        "transfers": policy.transfers(ctx),
    }


@pytest.mark.parametrize("component", COMPONENTS)
def test_the_ablated_component_matches_the_baseline(catalog, context, component) -> None:
    """The swapped decision must be the baseline's, exactly."""
    ablated = _decisions(AblatedPolicy(cfg, catalog, component), context)
    baseline = _decisions(BaselinePolicy(cfg, catalog), context)
    assert ablated[component] == baseline[component], (
        f"ablating {component} did not hand that decision to the baseline"
    )


@pytest.mark.parametrize("component", COMPONENTS)
def test_every_other_component_is_untouched(catalog, context, component) -> None:
    """Everything else must still be Policy B, or the difference is unattributable.

    An ablation that quietly changed two decisions would still produce a clean
    number, and that number would be the sum of two effects presented as one.
    """
    ablated = _decisions(AblatedPolicy(cfg, catalog, component), context)
    optimized = _decisions(OptimizedPolicy(cfg, catalog), context)
    for other in COMPONENTS:
        if other == component:
            continue
        assert ablated[other] == optimized[other], (
            f"ablating {component} also changed {other}, so neither can be attributed"
        )


def test_an_ablation_that_changes_nothing_is_visible(catalog, context) -> None:
    """A component whose two implementations agree contributes nothing measurably.

    That is not a bug, but it is indistinguishable from an ablation that failed
    to swap anything, and the two demand opposite responses. Markdown is the
    live case: at the declared zero disposal cost Policy B marks nothing down,
    and the baseline's flat ladder does - so if these two ever agree, the
    ablation has stopped removing anything and the readout must not report a
    confident zero.
    """
    optimized = _decisions(OptimizedPolicy(cfg, catalog), context)
    baseline = _decisions(BaselinePolicy(cfg, catalog), context)
    identical = [c for c in COMPONENTS if optimized[c] == baseline[c]]
    assert "markdown" not in identical, (
        "Policy B and the baseline now make the same markdown decision, so ablating it "
        "removes nothing - the attribution for that component would be a vacuous zero"
    )


def test_an_unknown_component_is_refused(catalog) -> None:
    """Typos must not silently produce an unablated policy reported as ablated."""
    with pytest.raises(ValueError, match="unknown component"):
        AblatedPolicy(cfg, catalog, "replenishment")  # the real name is `replenish`
