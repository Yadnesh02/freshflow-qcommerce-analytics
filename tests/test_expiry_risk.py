"""What the expiry risk model must be true of (task S3.4).

The gate is "every open batch scored; at-risk rupees sum sensibly", and the
second half of that is doing most of the work. A risk table is trivially easy to
make look right - score everything at zero and every aggregate is tidy, every
sum ties, and the number is useless. So these tests check the two things that
make the column mean something: that the FEFO queue is actually subtracted, and
that the scores rank against what subsequently happened.

Needs the whole chain:

    python tasks.py build && python tasks.py backtest
    python tasks.py forecast && python tasks.py expiry-risk
    python -m pytest tests/test_expiry_risk.py
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from analytics.forecasting.train import MAX_HORIZON

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

pytestmark = pytest.mark.needs_warehouse

STATES = {"expired", "at_risk", "beyond_horizon"}


@pytest.fixture(scope="module")
def con():
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    connection = duckdb.connect(str(WAREHOUSE), read_only=True)
    connection.execute("set enable_progress_bar = false")
    connection.execute("set memory_limit = '4GB'")
    connection.execute("set threads = 2")
    scored = connection.execute(
        """
        select count(*) from information_schema.tables
        where table_name = 'mart_expiry_risk'
        """
    ).fetchone()[0]
    if not scored:
        connection.close()
        pytest.skip("no mart_expiry_risk - run `python tasks.py expiry-risk`")
    yield connection
    connection.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


# ================================================== every open batch is scored
def test_every_batch_holding_stock_at_the_as_of_date_is_present(con) -> None:
    """The gate's first half, checked against the ledger rather than the mart.

    Batch position is replayed from movements, so the comparison has to be too -
    fct_inventory_batch.qty_remaining is as at the end of all data, not as at
    the scoring date, and comparing against it would pass for the wrong reason.
    """
    missing = one(
        con,
        """
        with as_of as (select max(date_day) as d from marts.mart_expiry_risk),
        position as (
            select moves.batch_id, sum(moves.qty_delta) as qty
            from marts.fct_inventory_movement as moves, as_of
            where moves.date_day <= as_of.d
            group by moves.batch_id
        )
        select count(*)
        from position
        left join marts.mart_expiry_risk as risk using (batch_id)
        where position.qty > 0 and risk.batch_id is null
        """,
    )
    assert missing == 0, f"{missing:,} batches held stock at the as-of date and were not scored"


def test_every_row_carries_a_known_state(con) -> None:
    states = {
        r[0]
        for r in con.execute("select distinct risk_state from marts.mart_expiry_risk").fetchall()
    }
    assert states <= STATES, f"unexpected risk_state values: {states - STATES}"
    assert states == STATES, f"a state is never produced: {STATES - states}"


def test_states_match_the_dates_they_claim(con) -> None:
    """The flag has to follow from days_to_expiry, not float free of it."""
    wrong = one(
        con,
        f"""
        select count(*) from marts.mart_expiry_risk
        where (days_to_expiry < 0 and risk_state <> 'expired')
           or (days_to_expiry between 0 and {MAX_HORIZON} and risk_state <> 'at_risk')
           or (days_to_expiry > {MAX_HORIZON} and risk_state <> 'beyond_horizon')
        """,
    )
    assert wrong == 0, f"{wrong:,} rows carry a state their days_to_expiry contradicts"


# ================================================== the numbers are bounded
def test_units_at_risk_never_exceeds_units_on_hand(con) -> None:
    """You cannot lose more than you hold, and the money column multiplies this."""
    over = one(
        con,
        "select count(*) from marts.mart_expiry_risk where units_at_risk > qty_remaining + 1e-9",
    )
    assert over == 0, f"{over:,} batches are at risk of losing more units than they hold"


def test_risk_scores_are_probabilities(con) -> None:
    outside = one(
        con,
        """
        select count(*) from marts.mart_expiry_risk
        where expiry_risk_score < 0 or expiry_risk_score > 1
        """,
    )
    assert outside == 0, f"{outside:,} scores fall outside [0, 1]"


def test_expired_batches_are_certain_and_beyond_horizon_ones_are_not_guessed(con) -> None:
    """The two states that are definitional rather than modelled."""
    bad_expired = one(
        con,
        """
        select count(*) from marts.mart_expiry_risk
        where risk_state = 'expired'
          and (expiry_risk_score <> 1.0 or abs(units_at_risk - qty_remaining) > 1e-9)
        """,
    )
    assert bad_expired == 0, (
        f"{bad_expired:,} already-expired batches are scored as anything other than certain"
    )

    bad_beyond = one(
        con,
        """
        select count(*) from marts.mart_expiry_risk
        where risk_state = 'beyond_horizon' and (expiry_risk_score <> 0 or units_at_risk <> 0)
        """,
    )
    assert bad_beyond == 0, (
        f"{bad_beyond:,} batches beyond the forecast horizon carry a risk number the "
        "horizon cannot support"
    )


# ================================================== FEFO actually applied
def test_the_fefo_queue_is_subtracted(con) -> None:
    """The modelling decision the whole table rests on.

    A batch behind others in the queue sees only the demand they leave. If
    residual demand ever exceeded the raw forecast, the queue would not be
    being subtracted at all and every batch would be scored as though it were
    first in line - which is the blindness this model exists to remove.
    """
    inflated = one(
        con,
        """
        select count(*) from marts.mart_expiry_risk
        where residual_demand_mean > horizon_demand_mean + 1e-9
        """,
    )
    assert inflated == 0, f"{inflated:,} batches expect more demand than the SKU forecast holds"

    queued = one(
        con,
        "select count(*) from marts.mart_expiry_risk where units_ahead_in_queue > 0",
    )
    assert queued > 0, "no batch has anything ahead of it - FEFO is not being computed"


def test_the_queue_ahead_grows_with_expiry_date(con) -> None:
    """Position in the queue is what FEFO means, and it must be ordered.

    Not asserted on residual demand, which is *not* monotone in expiry date and
    should not be: a batch expiring later has more of the forecast horizon
    available to it as well as more stock ahead of it, and those pull in
    opposite directions. The queue itself is the thing with an order to it - a
    later-expiring batch has at least as much in front of it as an earlier one,
    by the definition of the queue.
    """
    violations = one(
        con,
        """
        select count(*)
        from marts.mart_expiry_risk as front
        join marts.mart_expiry_risk as back
            on front.store_id = back.store_id
            and front.sku_id = back.sku_id
            and front.expiry_date < back.expiry_date
        where back.units_ahead_in_queue < front.units_ahead_in_queue - 1e-9
        """,
    )
    assert violations == 0, (
        f"{violations:,} later-expiring batches have less stock ahead of them than an "
        "earlier-expiring one, which is not a FEFO queue"
    )


# ================================================== it ranks what happened
def test_higher_scores_are_written_off_more_often(con) -> None:
    """The gate's second half. An action queue is read top-down, so ordering is
    the property that has to hold.

    Asserted on booked write-offs, which is what the score actually ranks:
    0.0%, 0.0%, 0.0%, 1.7%, 42.2% across risk bands. It is deliberately *not*
    asserted on every unsold unit, because the model does not rank those and
    test_the_dregs_are_a_known_blind_spot says why.
    """
    rows = con.execute(
        """
        with disposition as (
            select
                risk.batch_id,
                risk.expiry_risk_score,
                risk.qty_remaining,
                coalesce(
                    sum(-moves.qty_delta) filter (where moves.event_type = 'expiry_writeoff'), 0
                ) as written_off
            from marts.mart_expiry_risk as risk
            left join marts.fct_inventory_movement as moves
                on moves.batch_id = risk.batch_id and moves.date_day > risk.date_day
            where risk.risk_state = 'at_risk'
            group by risk.batch_id, risk.expiry_risk_score, risk.qty_remaining
        )
        select
            expiry_risk_score >= 0.5 as flagged,
            sum(written_off) / nullif(sum(qty_remaining), 0) as writeoff_rate
        from disposition
        group by expiry_risk_score >= 0.5
        """
    ).fetchall()
    by_flag = {bool(r[0]): r[1] for r in rows}
    assert set(by_flag) == {True, False}, "the scores do not separate into both halves"
    assert by_flag[True] > by_flag[False], (
        f"batches scored at or above 0.5 were written off at {by_flag[True]:.1%} against "
        f"{by_flag[False]:.1%} below - the score does not rank the outcome"
    )


def test_the_dregs_are_a_known_blind_spot(con) -> None:
    """A limitation recorded as a test, so it cannot quietly become a surprise.

    Batches the model scores as safe still fail to clear, and not by a little:
    334 batches averaging 1.59 units each, expiring five days out, sold 192 of
    531 units and had *zero* written off inside the window. The model is not
    wrong about the SKU - the forecast says it sells several units a day, and it
    does - it is wrong about whether these particular units participate. They are
    the dregs of a batch that has already mostly sold, and they behave like the
    6.9% residue seen across every expired batch in the warehouse: stock the
    ledger carries and the shelf never moves.

    No demand model fixes that, because it is not a demand phenomenon. What a
    demand model can do is not pretend otherwise, which is why the ranking test
    above is scoped to write-offs and this one states the gap in numbers.

    The test asserts the blind spot still looks like a blind spot. If small
    low-risk batches ever start clearing at the rate the forecast implies, the
    residue has gone away and both this test and the caveat in the module
    docstring should go with it.
    """
    small_low_risk_loss, large_high_risk_loss = con.execute(
        """
        with disposition as (
            select
                risk.batch_id,
                risk.expiry_risk_score,
                risk.qty_remaining,
                coalesce(sum(-moves.qty_delta), 0) as consumed
            from marts.mart_expiry_risk as risk
            left join marts.fct_inventory_movement as moves
                on moves.batch_id = risk.batch_id and moves.date_day > risk.date_day
            where risk.risk_state = 'at_risk'
            group by risk.batch_id, risk.expiry_risk_score, risk.qty_remaining
        )
        select
            sum(qty_remaining - consumed) filter (where expiry_risk_score < 0.5)
                / nullif(sum(qty_remaining) filter (where expiry_risk_score < 0.5), 0),
            sum(qty_remaining - consumed) filter (where expiry_risk_score >= 0.5)
                / nullif(sum(qty_remaining) filter (where expiry_risk_score >= 0.5), 0)
        from disposition
        """
    ).fetchone()
    assert small_low_risk_loss > large_high_risk_loss, (
        f"low-risk batches now fail to clear at {small_low_risk_loss:.1%} against "
        f"{large_high_risk_loss:.1%} for high-risk ones - the dregs effect has reversed, "
        "so the docstring caveat and this test are both out of date"
    )


def test_the_model_does_not_understate_the_loss(con) -> None:
    """Bias direction is a design choice and this pins it.

    Missing stock that spoils costs the batch; flagging stock that turns out
    fine costs a look at a queue. The model is meant to err toward the second,
    and a change that quietly made it optimistic would not show up in ranking.
    """
    predicted, realised = con.execute(
        """
        with disposition as (
            select
                risk.units_at_risk,
                risk.qty_remaining,
                risk.expiry_date,
                coalesce(sum(-moves.qty_delta), 0) as consumed,
                coalesce(
                    sum(-moves.qty_delta) filter (where moves.event_type = 'expiry_writeoff'), 0
                ) as written_off
            from marts.mart_expiry_risk as risk
            left join marts.fct_inventory_movement as moves
                on moves.batch_id = risk.batch_id and moves.date_day > risk.date_day
            where risk.risk_state = 'at_risk'
            group by all
        )
        select
            sum(units_at_risk),
            sum(
                written_off + case
                    when expiry_date <= (select max(date_day) from marts.agg_store_sku_day)
                    then qty_remaining - consumed else 0 end
            )
        from disposition
        """
    ).fetchone()
    assert realised > 0, "nothing spoiled in the validation window - nothing to check against"
    assert predicted >= realised, (
        f"predicted {predicted:,.0f} units at risk against {realised:,.0f} realised - "
        "the model has become optimistic, which is the wrong direction for this column"
    )
