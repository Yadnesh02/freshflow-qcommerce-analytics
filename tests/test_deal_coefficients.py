"""The allocator's coefficients, re-derived rather than trusted (task D5).

**This file exists because of how D2 started.** `deal_slots.py` carried a
standalone P&L in prose, nothing could execute it, and it was wrong for a sprint
and a half - Rs 13.37 a unit on an Rs 11 deal. D5 has just written four more
numbers into that same file. Writing them as constants and walking away would
repeat the mistake exactly, so each one is re-derived here from the warehouse
and checked against what the module claims.

**What is asserted is membership, not equality.** The causal basket margin has a
95% CI spanning two orders of magnitude; demanding the constant match a point
estimate to the paisa would fail on every rebuild and teach everyone to ignore
it. The useful property is that the number in the module still sits inside the
interval the data supports - and that the default is still the conservative end
of it rather than the middle.

    python -m pytest tests/test_deal_coefficients.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from analytics.deal import cannibalisation as cann
from analytics.deal.attach import itt_effects, per_customer
from analytics.optimization.deal_slots import (
    CANNIBALISATION_RATE,
    CAUSAL_BASKET_MARGIN,
    CAUSAL_BASKET_MARGIN_CI,
    DEFAULT_BASKET_MARGIN,
    DEFAULT_REACTIVATION_VALUE,
    score,
)

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

pytestmark = pytest.mark.needs_warehouse


@pytest.fixture(scope="module")
def con():
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    connection = duckdb.connect(str(WAREHOUSE), read_only=True)
    connection.execute("set enable_progress_bar = false")
    yield connection
    connection.close()


def _post_assignment(con) -> None:
    try:
        assigned, last = con.execute(
            """
            select
                (select max(assigned_date) from staging.stg_crm__deal_exposure),
                (select max(date_day) from marts.fct_order_item)
            """
        ).fetchone()
    except duckdb.CatalogException:
        pytest.skip("this build predates the D1 holdout")
    if assigned is None or last is None or last < assigned:
        pytest.skip(f"build ends {last}, holdout starts {assigned} - nothing measured yet")


# ================================================== the coefficients
def test_the_basket_margin_still_sits_inside_its_measured_interval(con) -> None:
    """The number in the module against the number in the warehouse."""
    _post_assignment(con)
    effects = {e.metric: e for e in itt_effects(per_customer(con))}
    attach = effects["nondeal_margin"]
    units = con.execute(
        """
        select sum(signed_qty)
        from marts.fct_order_item i,
             (select max(assigned_date) as d from staging.stg_crm__deal_exposure) a
        where i.promo_id = 'PROMO-DEAL11' and i.date_day >= a.d
        """
    ).fetchone()[0]
    if not units:
        pytest.skip("no post-assignment deal units to divide by")

    low = attach.ci_low * attach.n_exposed / units
    high = attach.ci_high * attach.n_exposed / units
    assert low <= CAUSAL_BASKET_MARGIN <= high, (
        f"deal_slots.py claims Rs {CAUSAL_BASKET_MARGIN:.2f} per deal unit; this build measures "
        f"an interval of [{low:.2f}, {high:.2f}]. Re-derive it - `python tasks.py deal-attach` "
        f"prints the figures."
    )


def test_the_default_is_the_conservative_end_not_the_middle(con) -> None:
    """A point estimate this wide does not belong in an objective function.

    At the central estimate the basket term is roughly fifty times the old
    constant and dwarfs clearance and subsidy together, so the allocator stops
    choosing on anything else and simply ranks by uptake - on a number whose
    lower bound is two orders of magnitude smaller.
    """
    assert CAUSAL_BASKET_MARGIN_CI[0] == DEFAULT_BASKET_MARGIN
    assert DEFAULT_BASKET_MARGIN < CAUSAL_BASKET_MARGIN


def test_the_cannibalisation_rate_still_matches_the_event_study(con) -> None:
    """A term the objective did not have until D5, so it has no history to drift from."""
    _post_assignment(con)
    try:
        summary = cann.summarise(cann.event_panel(con))
    except ValueError:
        pytest.skip("too few clean slot blocks in this build")
    if not summary["in_block_lift"]:
        pytest.skip("no in-block lift to normalise against")

    measured = -summary["net_cannibalisation"] / summary["in_block_lift"]
    # Loose on purpose: this moved from 0.029 to 0.115 between the pre- and
    # post-D1 datasets, so the property worth holding is the order of magnitude
    # and the sign, not the third decimal.
    assert 0.0 <= measured <= 0.5, f"cannibalisation rate measures {measured:.4f}, outside [0, 0.5]"
    assert abs(CANNIBALISATION_RATE - measured) < 0.10, (
        f"deal_slots.py claims {CANNIBALISATION_RATE:.4f}; this build measures {measured:.4f}. "
        f"`python tasks.py deal-cannibalisation` prints it."
    )


def test_reactivation_stays_zero_because_the_evidence_points_the_other_way(con) -> None:
    """D3 turned this default from caution into a finding.

    The rail now has a counterfactual, and measured against it the exposed arm
    churns more rather than less. A positive reactivation value is not merely
    unsupported here; it has the wrong sign.
    """
    assert DEFAULT_REACTIVATION_VALUE == 0.0

    _post_assignment(con)
    from analytics.deal.retention import _compare, outcomes

    churn = _compare(outcomes(con), "churned")
    assert churn.diff > -churn.mde, (
        f"churn moved {churn.diff * 100:+.2f}pp, below minus the MDE "
        f"({-churn.mde * 100:.2f}pp). The rail may now genuinely retain customers - "
        f"re-read D3 before leaving reactivation_value at zero."
    )


# ================================================== the objective
def test_cannibalisation_is_reported_but_not_charged(con) -> None:
    """The double-count D5 nearly shipped, pinned so it cannot come back.

    The first cut added cannibalisation to `slot_value` as a cost the objective
    was missing. It is not missing. The causal basket margin comes from an
    intent-to-treat comparison of the exposed arm against the holdout, so a
    normal-price sale the rail destroyed among exposed customers is already
    inside that difference; charging it again bills the same loss twice.

    The drag stays as a column because an allocator that cannot show how much
    of its own basket figure is being eaten is asking to be trusted - but it
    must never reach the objective.
    """
    import pandas as pd

    candidates = pd.DataFrame(
        {
            "sku_id": ["SKU-1"],
            "base_units_per_day": [10.0],
            "on_hand_units": [500.0],
            "units_at_risk": [0.0],
            "unit_landed_cost": [20.0],
            "base_price": [100.0],
        }
    )
    scored = score(candidates, reactivation_value=0.0)

    assert "cannibalisation_drag" in scored.columns, "the diagnostic was dropped entirely"
    assert scored["cannibalisation_drag"].iloc[0] < 0, "drag must read as a cost"

    components = (
        scored["clearance_value"]
        + scored["basket_value"]
        + scored["reactivation_value"]
        + scored["item_margin_delta"]
    ).iloc[0]
    assert scored["slot_value"].iloc[0] == pytest.approx(components), (
        "slot_value includes cannibalisation_drag. The causal basket margin already "
        "nets it, so this charges the same loss twice."
    )
