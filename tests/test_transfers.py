"""What the transfer engine has to be true of (task S4.4).

The gate is "no transfer recommended that can't survive transit", and it has the
same failure mode as every other gate in this sprint: it passes trivially when
nothing is recommended. So the shelf-life rule is checked from both sides -
nothing infeasible gets through, and something feasible does.

The finding worth protecting is why so little moves. 97% of candidate arcs are
dropped, and only 249 of 1,557 fail because transit exceeds the remaining shelf
life. 1,343 fail because the origin holds fewer than six units at risk. Stranded
stock here is a handful of units per store-SKU, not crates, so the binding
constraint is quantity rather than geography - and if that ever inverts, the
module's headline claim is wrong and `test_quantity_binds_harder_than_distance`
should say so.

Needs the recommendations built:

    python tasks.py build && python tasks.py expiry-risk
    python tasks.py transfers
    python -m pytest tests/test_transfers.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from analytics.optimization.transfers import (
    DEFAULT_HANDLING_HOURS,
    DEFAULT_SPEED_KMH,
    DEFICIT_LOOKBACK_DAYS,
    MIN_TRANSFER_UNITS,
    apply_shelf_life_gate,
    attribute_trip_cost,
    load_candidates,
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
        "select count(*) from information_schema.tables where table_name = 'rec_transfer_order'"
    ).fetchone()[0]
    if not built:
        connection.close()
        pytest.skip("no marts.rec_transfer_order - run `python tasks.py transfers`")
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def candidates(con) -> pd.DataFrame:
    return load_candidates(con, DEFICIT_LOOKBACK_DAYS)


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


# ================================================== the gate
def test_no_recommended_transfer_can_miss_its_expiry(con) -> None:
    """S4.4's gate. Enforced by construction in the arc bound, asserted anyway."""
    late = con.execute(
        """
        select from_store, to_store, sku_id, days_to_expiry,
               transit_hours, sellable_days_after_transit
        from marts.rec_transfer_order
        where sellable_days_after_transit <= 0
        """
    ).fetchall()
    assert not late, f"transfers that arrive after expiry: {late}"


def test_the_gate_is_not_passing_because_nothing_moves(con) -> None:
    """A gate about recommended transfers is vacuous with zero of them.

    This is the same trap as S4.2's monotonicity sweep and S4.3's private-label
    floor: the assertion above holds perfectly over an empty table.
    """
    moved = one(con, "select count(*) from marts.rec_transfer_order")
    assert moved > 0, (
        "no transfer was recommended at all, so the shelf-life gate is untested by "
        "the data - check the trip cost and the minimum transfer size before "
        "trusting a green gate here"
    )


def test_transit_infeasible_arcs_are_dropped_not_merely_penalised(candidates) -> None:
    """An arc that cannot survive transit must be unrepresentable, not unattractive.

    Left in the program with a large negative value it would still be chosen if
    everything else were worse. Bounded at zero units it cannot be chosen at all.
    """
    gated = apply_shelf_life_gate(candidates, DEFAULT_SPEED_KMH, DEFAULT_HANDLING_HOURS)
    assert (gated["sellable_days"] > 0).all(), "an arc survived with no sellable days left"
    assert (gated["arc_cap"] >= MIN_TRANSFER_UNITS).all()


def test_a_slower_van_strands_more_stock(candidates) -> None:
    """The gate has to actually respond to the assumption it is built on.

    If halving the assumed speed does not shrink the feasible set, the transit
    term is not reaching the bound and the gate is decorative.
    """
    fast = apply_shelf_life_gate(candidates, DEFAULT_SPEED_KMH, DEFAULT_HANDLING_HOURS)
    slow = apply_shelf_life_gate(candidates, DEFAULT_SPEED_KMH / 4, DEFAULT_HANDLING_HOURS * 4)
    assert len(slow) < len(fast), (
        f"a four-times-slower van left the feasible set unchanged at {len(fast)} arcs"
    )


# ================================================== the finding
def test_quantity_binds_harder_than_distance(candidates) -> None:
    """The module's headline claim, held open.

    97% of arcs are dropped, but transit failure accounts for only a sixth of
    that. The dominant reason is that both sides are tiny - the dregs population
    mart_expiry_risk already documents. If distance ever becomes the dominant
    constraint, the docstring is wrong.
    """
    transit_days = (DEFAULT_HANDLING_HOURS + candidates["km"] / DEFAULT_SPEED_KMH) / 24.0
    fails_transit = int((candidates["days_to_expiry"] - transit_days <= 0).sum())
    thin_origin = int((candidates["units_at_risk"] < MIN_TRANSFER_UNITS).sum())

    assert thin_origin > fails_transit, (
        f"distance now binds harder than quantity ({fails_transit} transit failures "
        f"against {thin_origin} thin origins) - the module docstring says the reverse"
    )


def test_the_network_has_both_surplus_and_deficit(candidates) -> None:
    """P3's premise. Without it there is nothing for this engine to do."""
    assert not candidates.empty
    assert candidates["from_store"].nunique() > 1
    assert candidates["to_store"].nunique() > 1


# ================================================== the economics
def test_no_transfer_moves_more_than_exists_or_more_than_is_needed(con) -> None:
    over = con.execute(
        """
        select from_store, to_store, sku_id, units, arc_cap
        from marts.rec_transfer_order where units > arc_cap
        """
    ).fetchall()
    assert not over, f"transfers above their arc cap: {over}"


def test_no_transfer_is_below_the_minimum_size(con) -> None:
    """Moving three units across Mumbai is a rounding error with a van attached."""
    tiny = one(
        con,
        f"select count(*) from marts.rec_transfer_order where units < {MIN_TRANSFER_UNITS}",
    )
    assert tiny == 0, "a transfer below the minimum size was recommended"


def test_the_trip_cost_is_shared_across_everything_on_the_van() -> None:
    """One van, one fixed charge, split by what rides on it.

    Charging the full trip to each SKU would make a two-SKU transfer look twice
    as expensive as it is, and net benefit is what decides whether it happens.
    """
    moved = pd.DataFrame(
        [
            {
                "from_store": "A",
                "to_store": "B",
                "sku_id": "S1",
                "units": 30,
                "avoided_writeoff": 500.0,
                "recovered_margin": 200.0,
                "trip_cost": 400.0,
            },
            {
                "from_store": "A",
                "to_store": "B",
                "sku_id": "S2",
                "units": 10,
                "avoided_writeoff": 150.0,
                "recovered_margin": 60.0,
                "trip_cost": 400.0,
            },
        ]
    )
    shared = attribute_trip_cost(moved)
    assert shared["trip_cost_share"].sum() == pytest.approx(400.0), (
        "the trip was charged more than once across the SKUs riding on it"
    )
    assert shared.loc[0, "trip_cost_share"] == pytest.approx(300.0)
    assert shared.loc[1, "trip_cost_share"] == pytest.approx(100.0)


def test_every_recommended_transfer_pays_for_its_share_of_the_van(con) -> None:
    """P3's condition: avoided write-off plus recovered margin beats the trip."""
    losers = con.execute(
        """
        select from_store, to_store, sku_id, net_benefit
        from marts.rec_transfer_order where net_benefit <= 0
        """
    ).fetchall()
    assert not losers, f"transfers recommended that do not pay for themselves: {losers}"


def test_a_store_never_transfers_to_itself(con) -> None:
    self_moves = one(
        con, "select count(*) from marts.rec_transfer_order where from_store = to_store"
    )
    assert self_moves == 0


def test_distances_are_real_and_plausible_for_mumbai(con) -> None:
    """The one input here that is measured rather than assumed.

    Haversine over the store dimension's lat/lon: 2.33 km at the closest pair,
    26.89 at the furthest. A bug in the formula shows up as absurd distances
    long before it shows up as a bad recommendation.
    """
    bounds = con.execute("select min(km), max(km) from marts.rec_transfer_order").fetchone()
    assert bounds[0] > 0.5, f"a transfer distance of {bounds[0]} km is not two dark stores"
    assert bounds[1] < 60, f"a transfer distance of {bounds[1]} km is outside Mumbai"
