"""What the perishable newsvendor has to be true of (task S4.5).

The gate is "order-up-to level capped by shelf life", which - like every other
gate in this sprint - passes trivially if the cap never binds. So it is checked
twice: nothing exceeds its cap, and the cap demonstrably changes at least one
answer.

The property that actually matters is the shape. A newsvendor that gave the same
service level to two-day curd and four-year detergent would not be a perishable
newsvendor at all, it would be a constant with extra arithmetic. The critical
ratio has to rise with shelf life - 0.39 at 0-2 days through 0.97 above thirteen
months - and `test_the_critical_ratio_rises_with_shelf_life` is the test that
would fail if the spoilage term ever stopped reaching the objective.

Needs the order book built:

    python tasks.py build && python tasks.py forecast && python tasks.py expiry-risk
    python tasks.py newsvendor
    python -m pytest tests/test_newsvendor.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from analytics.optimization.newsvendor import (
    LEAD_TIME_QUANTILE,
    REVIEW_PERIOD_DAYS,
    SHELF_LIFE_CAP_QUANTILE,
    negbin_cdf,
    negbin_quantile,
    solve_order_up_to,
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
    connection.execute("set memory_limit = '4GB'")
    built = connection.execute(
        "select count(*) from information_schema.tables where table_name = 'rec_purchase_order'"
    ).fetchone()[0]
    if not built:
        connection.close()
        pytest.skip("no marts.rec_purchase_order - run `python tasks.py newsvendor`")
    yield connection
    connection.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


def synthetic(shelf_life_days: float, daily: float = 8.0) -> pd.DataFrame:
    """One store-SKU, identical but for its shelf life."""
    return pd.DataFrame(
        [
            {
                "shelf_life_days": shelf_life_days,
                "base_price": 100.0,
                "landed_cost": 60.0,
                "daily_forecast": daily,
                "on_hand_units": 0.0,
                "lead_days": 3.0,
            }
        ]
    )


# ================================================== the gate
def test_no_order_exceeds_its_shelf_life_cap(con) -> None:
    """S4.5's gate, stated as the plan states it."""
    over = con.execute(
        """
        select store_id, sku_id, order_up_to, shelf_life_cap, shelf_life_days
        from marts.rec_purchase_order
        where order_up_to > shelf_life_cap + 1e-6
        """
    ).fetchall()
    assert not over, f"order-up-to levels above their shelf-life cap: {over}"


def test_the_shelf_life_cap_actually_binds(con) -> None:
    """The gate above holds perfectly over a cap that never does anything.

    Same trap as S4.2's flat monotonicity sweep, S4.3's zero private-label floor
    and S4.4's empty transfer table: the assertion is true and empty.
    """
    capped = one(con, "select count(*) from marts.rec_purchase_order where is_shelf_life_capped")
    assert capped > 0, (
        "the shelf-life cap never binds, so the gate is untested - check the cap "
        "quantile before trusting it"
    )


def test_a_shorter_shelf_life_lowers_the_cap() -> None:
    """The cap has to respond to the thing it is named after."""
    short = solve_order_up_to(synthetic(2.0), dispersion=1.2, retention_cost=0.0)
    long = solve_order_up_to(synthetic(400.0), dispersion=1.2, retention_cost=0.0)
    assert short["shelf_life_cap"].iloc[0] < long["shelf_life_cap"].iloc[0]


# ================================================== the economics
def test_the_critical_ratio_rises_with_shelf_life(con) -> None:
    """The property that makes this a *perishable* newsvendor.

    A leftover unit of curd is thrown away; a leftover unit of detergent is
    merely early. Co carries that difference through P(spoil), so the service
    level the model asks for has to climb with shelf life. Measured on the real
    order book it runs 0.39 / 0.61 / 0.82 / 0.94 / 0.97 across the bands.
    """
    rows = con.execute(
        """
        select
            case
                when shelf_life_days <= 2 then 1
                when shelf_life_days <= 7 then 2
                when shelf_life_days <= 30 then 3
                when shelf_life_days <= 400 then 4
                else 5
            end as band,
            avg(critical_ratio) as cr
        from marts.rec_purchase_order
        group by band order by band
        """
    ).fetchall()
    ratios = [r[1] for r in rows]
    assert len(ratios) >= 3, "not enough shelf-life bands present to test the shape"
    # not strict=True: adjacent pairing zips a list against its own tail
    assert all(a <= b + 1e-9 for a, b in zip(ratios, ratios[1:], strict=False)), (
        f"the critical ratio does not rise with shelf life: {ratios}"
    )


def test_a_perishable_orders_less_than_a_durable_on_identical_demand() -> None:
    """Same demand, same price, same cost - only the shelf life differs."""
    short = solve_order_up_to(synthetic(2.0), dispersion=1.2, retention_cost=0.0)
    long = solve_order_up_to(synthetic(400.0), dispersion=1.2, retention_cost=0.0)
    assert short["order_up_to"].iloc[0] < long["order_up_to"].iloc[0]
    assert short["critical_ratio"].iloc[0] < long["critical_ratio"].iloc[0]


def test_the_critical_ratio_stays_a_probability(con) -> None:
    bad = one(
        con,
        "select count(*) from marts.rec_purchase_order "
        "where critical_ratio <= 0 or critical_ratio >= 1",
    )
    assert bad == 0, "a critical ratio outside (0, 1) is not a service level"


def test_the_spoilage_fixed_point_is_consistent() -> None:
    """P(spoil) sets the order quantity and the order quantity sets P(spoil).

    After iterating, the reported spoilage probability has to be the one implied
    by the reported order-up-to level. If it is not, the loop exited before
    settling and every perishable is biased in the same direction.
    """
    solved = solve_order_up_to(synthetic(3.0), dispersion=1.2, retention_cost=0.0)
    row = solved.iloc[0]
    implied = negbin_cdf(np.array([row["order_up_to"] - 1]), np.array([row["demand_usable"]]), 1.2)[
        0
    ]
    assert row["p_spoil"] == pytest.approx(implied, abs=1e-6), (
        "the reported spoilage probability does not match the reported quantity"
    )


def test_retention_cost_raises_the_service_level() -> None:
    """The declared assumption has to be inert at zero and live above it."""
    base = solve_order_up_to(synthetic(5.0), dispersion=1.2, retention_cost=0.0)
    with_retention = solve_order_up_to(synthetic(5.0), dispersion=1.2, retention_cost=200.0)
    assert with_retention["critical_ratio"].iloc[0] > base["critical_ratio"].iloc[0]
    assert with_retention["order_up_to"].iloc[0] >= base["order_up_to"].iloc[0]


def test_stock_on_hand_reduces_the_order(con) -> None:
    """Order-up-to is a target, not a quantity. Ordering the target regardless of
    what is already on the shelf is the classic way to double-stock."""
    wrong = one(
        con,
        """
        select count(*) from marts.rec_purchase_order
        where abs(order_units - greatest(round(order_up_to - on_hand_units), 0)) > 1
        """,
    )
    assert wrong == 0, "order quantity does not net off stock already on hand"


# ================================================== the distribution
def test_the_protection_window_uses_the_lead_time_tail_not_the_mean(con) -> None:
    """A service level computed against average lead time is not that service
    level on the days the van is late."""
    assert LEAD_TIME_QUANTILE > 0.5
    short = one(
        con,
        f"select count(*) from marts.rec_purchase_order "
        f"where protection_days < lead_days + {REVIEW_PERIOD_DAYS} - 1e-9",
    )
    assert short == 0, "the protection window is shorter than lead time plus review"


def test_negative_binomial_is_wider_than_poisson() -> None:
    """Poisson fixes Var = mu, which understates the spread and quietly
    under-orders every SKU whose demand is lumpy - which is most perishables."""
    mean = np.array([10.0])
    nb = negbin_quantile(mean, 1.2, 0.95)[0]
    poisson_like = negbin_quantile(mean, 1e6, 0.95)[0]
    assert nb > poisson_like, (
        f"the negative binomial ({nb}) is no wider than the Poisson limit ({poisson_like})"
    )


def test_the_cap_quantile_is_high_enough_to_be_a_cap_not_a_target() -> None:
    """At a low quantile the 'cap' would bind everywhere and become the policy."""
    assert SHELF_LIFE_CAP_QUANTILE >= 0.9
