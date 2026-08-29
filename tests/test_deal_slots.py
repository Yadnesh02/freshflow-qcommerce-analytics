"""What the deal-slot allocator has to be true of (task S4.3).

The gate is "solver returns feasible; PL floor respected", and both halves have
a way of passing without meaning anything. A program with no binding constraint
is feasible trivially, and a floor of `int(0.30 * 3)` is zero - which is
satisfied by every allocation ever made, including one that picks no private
label at all. That exact bug was in the first draft of `allocate_store_day` and
is the reason `test_the_private_label_floor_is_not_vacuous` exists: the floor
has to be shown *changing the answer* before it is allowed to pass.

The other thing worth pinning is the comparison the whole task rests on. P6
claims the central pick has no link to what is about to expire, and it is true
on the as-of date - 14 store-SKUs on the deal, 674 at risk, and an empty
intersection. If the allocator did not beat that it would have no reason to
exist, so `test_the_allocator_clears_more_than_the_central_pick` holds it.

Needs the recommendations built:

    python tasks.py build && python tasks.py expiry-risk
    python tasks.py deal-slots
    python -m pytest tests/test_deal_slots.py
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from analytics.optimization.deal_slots import (
    DEAL_PRICE,
    DEAL_UPLIFT_MULTIPLIER,
    MIN_DAYS_TO_EXPIRY,
    MIN_ON_HAND_UNITS,
    PRIVATE_LABEL_FLOOR,
    allocate_store_day,
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
    connection.execute("set memory_limit = '4GB'")
    built = connection.execute(
        "select count(*) from information_schema.tables where table_name = 'rec_deal_slot'"
    ).fetchone()[0]
    if not built:
        connection.close()
        pytest.skip("no marts.rec_deal_slot - run `python tasks.py deal-slots`")
    yield connection
    connection.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


def synthetic(n_pl: int = 2, n_brand: int = 6) -> pd.DataFrame:
    """A store-day where private label is deliberately the worse choice.

    Brand SKUs are given a higher slot value, so an unconstrained optimiser
    picks brand every time. That is what makes the floor observable: if the
    constraint does nothing, the allocation is identical with and without it.
    """
    rows = []
    for i in range(n_brand):
        rows.append(
            {
                "date_day": "2026-08-24",
                "store_id": "FF-TEST-01",
                "sku_id": f"BRAND-{i:02d}",
                "sku_name": f"brand {i}",
                "l1_category": "Snacks & Namkeen",
                "l2_subcategory": f"sub-{i}",
                "is_private_label": False,
                "slot_value": 1000.0 - i,
            }
        )
    for i in range(n_pl):
        rows.append(
            {
                "date_day": "2026-08-24",
                "store_id": "FF-TEST-01",
                "sku_id": f"PL-{i:02d}",
                "sku_name": f"private {i}",
                "l1_category": "Dairy & Eggs",
                "l2_subcategory": f"pl-sub-{i}",
                "is_private_label": True,
                "slot_value": 1.0 + i,
            }
        )
    return pd.DataFrame(rows)


# ================================================== the gate
def test_every_store_day_solves(con) -> None:
    """Half of S4.3's gate: the program is feasible everywhere it is posed."""
    unsolved = con.execute(
        """
        select distinct store_id, solver_status
        from marts.rec_deal_slot where solver_status <> 'Optimal'
        """
    ).fetchall()
    assert not unsolved, f"solver did not reach optimal on {unsolved}"


def test_the_private_label_floor_is_respected(con) -> None:
    """The other half, on the real allocation."""
    slots = one(con, "select max(slot_rank) from marts.rec_deal_slot")
    required = math.ceil(PRIVATE_LABEL_FLOOR * slots)
    short = con.execute(
        """
        select date_day, store_id, count(*) filter (where is_private_label) as pl
        from marts.rec_deal_slot
        group by date_day, store_id
        having count(*) filter (where is_private_label) < ?
        """,
        [required],
    ).fetchall()
    assert not short, f"store-days below the {required}-slot private-label floor: {short}"


def test_the_private_label_floor_is_not_vacuous() -> None:
    """The floor has to change the answer, or the gate above tests nothing.

    `int(0.30 * 3)` is 0, and a floor of zero is satisfied by every allocation
    including one with no private label at all. That was the first draft. Here
    private label is deliberately the *worse* pick, so an unconstrained solver
    takes none of it and a working floor is visible as a difference.
    """
    candidates = synthetic()

    unconstrained, status = allocate_store_day(candidates, slots=3, pl_floor=0.0)
    assert status == "Optimal"
    assert unconstrained["is_private_label"].sum() == 0, (
        "private label won on merit here, so this fixture cannot detect a dead floor"
    )

    constrained, status = allocate_store_day(candidates, slots=3, pl_floor=PRIVATE_LABEL_FLOOR)
    assert status == "Optimal"
    assert constrained["is_private_label"].sum() >= math.ceil(PRIVATE_LABEL_FLOOR * 3) == 1, (
        "the floor did not bind - check it uses ceil rather than int truncation"
    )


def test_the_floor_uses_ceiling_not_truncation() -> None:
    """Stated directly, because the difference is silent and total.

    At the default K of 3, truncation gives a floor of zero and ceiling gives
    one. Both look like "30%" in a config.
    """
    assert math.ceil(PRIVATE_LABEL_FLOOR * 3) == 1
    assert int(PRIVATE_LABEL_FLOOR * 3) == 0, (
        "the truncation trap has moved - the comment in allocate_store_day needs updating"
    )


# ================================================== the constraints
def test_no_store_day_exceeds_its_slot_budget(con) -> None:
    slots = one(con, "select max(slot_rank) from marts.rec_deal_slot")
    over = con.execute(
        "select date_day, store_id, count(*) from marts.rec_deal_slot "
        "group by date_day, store_id having count(*) > ?",
        [slots],
    ).fetchall()
    assert not over, f"store-days over the slot budget: {over}"


def test_at_most_one_slot_per_subcategory(con) -> None:
    """Otherwise a store spends every slot on chocolate."""
    doubled = con.execute(
        """
        select date_day, store_id, l2_subcategory, count(*) as n
        from marts.rec_deal_slot
        group by date_day, store_id, l2_subcategory
        having count(*) > 1
        """
    ).fetchall()
    assert not doubled, f"more than one slot in a subcategory: {doubled}"


def test_the_on_hand_and_shelf_life_screens_hold(con) -> None:
    """Screened in the candidate query, so this checks they actually reached it."""
    bad = con.execute(
        """
        select sku_id, on_hand_units, days_to_expiry
        from marts.rec_deal_slot
        where on_hand_units < ? or days_to_expiry < ?
        """,
        [MIN_ON_HAND_UNITS, MIN_DAYS_TO_EXPIRY],
    ).fetchall()
    assert not bad, f"slots allocated to stock that fails the screens: {bad}"


def test_nothing_is_dealt_at_or_below_the_deal_price(con) -> None:
    """A slot on a SKU already selling for Rs 11 or less is not a promotion."""
    pointless = one(
        con,
        f"select count(*) from marts.rec_deal_slot where base_price <= {DEAL_PRICE}",
    )
    assert pointless == 0, "a deal slot was given to a SKU already at or below the deal price"


# ================================================== the economics
def test_uptake_never_exceeds_stock_on_hand(con) -> None:
    """An uncapped multiplier is how an optimiser sells units nobody has."""
    impossible = con.execute(
        """
        select sku_id, expected_units, on_hand_units
        from marts.rec_deal_slot where expected_units > on_hand_units + 1e-6
        """
    ).fetchall()
    assert not impossible, f"expected uptake above stock on hand: {impossible}"


def test_clearance_never_exceeds_what_is_actually_at_risk(con) -> None:
    """Crediting a slot with clearing stock that was not dying inflates every
    comparison against the baseline, which is the whole argument of S4.3."""
    inflated = con.execute(
        """
        select sku_id, clearance_value, units_at_risk, unit_landed_cost
        from marts.rec_deal_slot
        where clearance_value > units_at_risk * unit_landed_cost + 1e-6
        """
    ).fetchall()
    assert not inflated, f"clearance credited beyond units at risk: {inflated}"


def test_the_subsidy_is_incremental_not_the_full_discount() -> None:
    """`(base - 11) x uptake` bills the discount to units that never would have
    sold. The item term is what the slot earns minus what those units would have
    earned at their normal price and volume, so it is strictly the smaller loss.
    """
    candidates = pd.DataFrame(
        [
            {
                "base_units_per_day": 2.0,
                "on_hand_units": 500.0,
                "units_at_risk": 0.0,
                "unit_landed_cost": 20.0,
                "base_price": 100.0,
            }
        ]
    )
    scored = score(candidates, reactivation_value=0.0)
    uptake = 2.0 * DEAL_UPLIFT_MULTIPLIER
    naive_subsidy = uptake * (100.0 - DEAL_PRICE)
    assert scored["subsidy"].iloc[0] < naive_subsidy, (
        "the subsidy is being charged as the full discount on every unit"
    )


def test_a_slot_on_long_life_stock_earns_no_clearance_credit() -> None:
    """The baseline's mistake, stated as a property.

    Six of the 52 SKUs the central pick chose were Household & Cleaning at an
    average 788 days of shelf life. Nothing about a slot on those clears
    anything, and the objective has to say so rather than quietly valuing them.
    """
    candidates = pd.DataFrame(
        [
            {
                "base_units_per_day": 3.0,
                "on_hand_units": 400.0,
                "units_at_risk": 0.0,
                "unit_landed_cost": 30.0,
                "base_price": 90.0,
            }
        ]
    )
    scored = score(candidates, reactivation_value=0.0)
    assert scored["clearance_value"].iloc[0] == 0.0


def test_the_allocator_clears_more_than_the_central_pick(con) -> None:
    """P6's claim, and the reason this task exists.

    On the as-of date the central pick had 14 store-SKUs on the deal and 674
    store-SKUs at risk, with an empty intersection. Any allocator worth building
    beats zero.
    """
    baseline_overlap = one(
        con,
        """
        with as_of as (select max(date_day) as d from marts.mart_expiry_risk),
        dealt as (
            select distinct price.store_id, price.sku_id
            from marts.fct_price_history as price cross join as_of
            where price.promo_id = 'PROMO-DEAL11'
              and as_of.d between price.effective_from_date and price.effective_to_date
        )
        select count(*) from dealt
        inner join marts.mart_expiry_risk as risk
            on risk.store_id = dealt.store_id and risk.sku_id = dealt.sku_id
        where risk.risk_state = 'at_risk'
        """,
    )
    allocated_clearing = one(
        con, "select count(*) from marts.rec_deal_slot where clearance_value > 0"
    )
    assert baseline_overlap == 0, (
        "the central pick now overlaps at-risk stock - P6's premise has changed "
        "and the module docstring says it has not"
    )
    assert allocated_clearing > 0, "the allocator clears nothing either"


def test_reactivation_is_off_by_default_and_moves_the_answer() -> None:
    """A declared assumption has to be both inert at its default and live above it,
    or it is not a lever S5.3 can sweep."""
    candidates = pd.DataFrame(
        [
            {
                "base_units_per_day": 2.0,
                "on_hand_units": 500.0,
                "units_at_risk": 0.0,
                "unit_landed_cost": 20.0,
                "base_price": 100.0,
            }
        ]
    )
    at_zero = score(candidates, reactivation_value=0.0)
    assert at_zero["reactivation_value"].iloc[0] == 0.0

    at_value = score(candidates, reactivation_value=2000.0)
    assert at_value["reactivation_value"].iloc[0] > 0
    assert at_value["slot_value"].iloc[0] > at_zero["slot_value"].iloc[0]
