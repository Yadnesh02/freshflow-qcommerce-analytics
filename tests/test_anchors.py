"""The anchor check has to fail when the build moves, and only then (task S5.0).

An anchor set is a strange thing to test, because the obvious test - "do the
anchors pass?" - is the one thing that must be allowed to fail: the whole point
of S5.0 is running it on a clean runner and believing the answer. So these
tests do not assert the seven figures. They assert the properties that decide
whether a pass or a fail means anything:

  - every query executes and reads the table it claims to read
  - a difference that matters is caught, and float noise is not
  - the seven are seven measurements, not one measurement counted seven times
  - a wrong-shaped dataset says so instead of reporting six red herrings

The failure paths get the most attention here, because they are the ones nobody
exercises by accident. The happy path runs every time the workflow does.
"""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import duckdb
import pytest

from analytics.anchors import (
    ANCHORS,
    MONEY_TOLERANCE_INR,
    SHAPE_ANCHOR,
    SHAPE_TOLERANCE,
    Anchor,
    Result,
    _matches,
    check,
    report,
)

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

# Measured on mart_customer_360 across two rebuilds of identical data: DuckDB
# sums floats in thread-completion order, so money columns move in their last
# bits. The tolerance has to sit far above this and far below a paise.
OBSERVED_FLOAT_DRIFT_INR = 1.5e-11


def test_the_money_tolerance_is_far_above_float_noise_and_far_below_anything_meaningful() -> None:
    """The tolerance is a business unit, not a threshold tuned until tests passed.

    Two failure modes bracket it. Too tight and the check fails on thread
    scheduling, which trains everyone to ignore it - the worst outcome for a
    gate. Too loose and real movement hides underneath. A paise clears the
    measured drift by nine orders of magnitude while still being the smallest
    unit the business has, so there is no room to argue it was fitted.
    """
    assert MONEY_TOLERANCE_INR > OBSERVED_FLOAT_DRIFT_INR * 1e6
    assert MONEY_TOLERANCE_INR <= 0.01


def test_float_noise_passes_but_a_rupee_does_not() -> None:
    """The tolerance behaves at both edges, on a real anchor rather than a toy."""
    money = next(a for a in ANCHORS if a.money)
    assert _matches(money, money.expected + OBSERVED_FLOAT_DRIFT_INR)
    assert _matches(money, money.expected - OBSERVED_FLOAT_DRIFT_INR)
    assert not _matches(money, money.expected + 1.0)
    assert not _matches(money, money.expected - 1.0)


def test_a_count_off_by_one_fails() -> None:
    """Counts have no tolerance, and one row is the smallest thing that can go wrong.

    The phantom store was one row. Had it been compared with any tolerance at
    all, on a 6.3 million row table, it would have passed.
    """
    for anchor in (a for a in ANCHORS if not a.money):
        assert _matches(anchor, anchor.expected)
        assert not _matches(anchor, anchor.expected + 1)
        assert not _matches(anchor, anchor.expected - 1)


def test_the_anchors_are_seven_measurements_not_one_counted_seven_times() -> None:
    """Distinct queries over several tables, or the set proves less than it looks.

    Four of the seven read `agg_store_sku_day`, which is fine - it is the
    warehouse's grain - but if all seven did, a single damaged table would move
    them together and a green run would mean only that one table was
    self-consistent. The set has to span the pipeline: a fact, the aggregate,
    and the Sprint 4 marts built on top of it.
    """
    assert len({a.name for a in ANCHORS}) == len(ANCHORS), "duplicate anchor names"
    assert len({a.sql for a in ANCHORS}) == len(ANCHORS), "two anchors run the same query"

    tables = {a.sql.split(" from ")[1].split()[0] for a in ANCHORS}
    assert len(tables) >= 4, f"anchors only cover {tables} - one damaged table moves them together"


def test_every_anchor_says_why_it_is_worth_pinning() -> None:
    """A figure with no stated reason is a figure nobody can decide about later.

    When a clean runner disagrees, the question is which build to believe, and
    that is answered from what the number means - not from its value.
    """
    for anchor in ANCHORS:
        assert len(anchor.why) > 40, f"{anchor.name} has no real justification"


def test_a_wrong_shape_reports_the_dataset_not_the_figures(capsys) -> None:
    """Six red herrings help nobody.

    Run the check against a 30-day slice and every anchor fails, because none
    of them describe that dataset. Reporting seven failures invites someone to
    debug net revenue when the actual problem is that they built the wrong
    thing. The shape anchor is checked first and short-circuits the rest.
    """
    results = [
        Result(a, a.expected if a.name != SHAPE_ANCHOR else 1_000.0, a.name != SHAPE_ANCHOR)
        for a in ANCHORS
    ]
    assert report(results) is False
    out = capsys.readouterr().out
    assert "not the build these figures describe" in out
    assert "meaningless" in out


def test_a_missing_table_is_a_result_rather_than_a_crash() -> None:
    """An optimiser that has not run yet must report, not raise.

    `warehouse.yml` runs the five optimisers before this check, so a missing
    `rec_markdown` means a step failed earlier. That has to surface as a named
    anchor failure in the table, not a traceback that buries which one.
    """
    con = duckdb.connect(":memory:")
    try:
        results = check(con)
    finally:
        con.close()
    assert all(not r.ok for r in results)
    assert all(r.error for r in results), "a missing table should be reported as an error string"


def test_an_anchor_cannot_pass_by_returning_null() -> None:
    """`sum()` over no rows is NULL, and NULL must never read as agreement.

    An empty mart is a build failure. Comparing NULL loosely - or coercing it -
    would let the emptiest possible warehouse pass the money anchors.
    """
    con = duckdb.connect(":memory:")
    try:
        con.execute("create schema marts")
        con.execute("create table marts.agg_store_sku_day(store_id varchar, net_revenue double)")
        results = {r.anchor.name: r for r in check(con)}
    finally:
        con.close()
    assert results["net_revenue"].ok is False
    assert results["net_revenue"].error == "query returned NULL"


@pytest.mark.needs_warehouse
def test_every_anchor_query_runs_against_the_real_warehouse(full_year) -> None:
    """The queries are real SQL over real tables, not strings nobody executed.

    Asserts execution and a non-null answer, deliberately not the value: a
    changed value is a decision for a human, and encoding the seven figures in
    two places would only guarantee they drift apart.
    """
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        con.execute("set enable_progress_bar=false")
        for result in check(con):
            assert result.error is None, f"{result.anchor.name}: {result.error}"
            assert result.actual is not None
    finally:
        con.close()


def test_a_money_anchor_is_never_declared_as_a_count() -> None:
    """The two rupee figures must carry money=True or they get compared as ints.

    `int()` truncates rather than rounds, so a money anchor mistakenly typed as
    a count ignores any error that does not carry it past the next whole rupee -
    which is twenty times the tolerance it should have had.
    """
    by_name = {a.name: a for a in ANCHORS}
    for name in ("net_revenue", "writeoff_value"):
        assert by_name[name].money is True, f"{name} must be compared as money"

    # 434,304,805.80 has 0.19 of headroom before int() sees a different number.
    # Anything inside that window is invisible to a count comparison, and 0.19
    # is 19 paise - nineteen times what the money path would allow.
    counted = replace(by_name["net_revenue"], money=False)
    assert _matches(counted, counted.expected + 0.19), (
        "sanity check on the bug this guards: as a count, sub-rupee error is invisible"
    )
    assert not _matches(by_name["net_revenue"], by_name["net_revenue"].expected + 0.19), (
        "the same error must be caught once the anchor is correctly typed as money"
    )


def test_anchor_is_frozen() -> None:
    """Anchors are constants. A test that could rewrite one proves nothing."""
    with pytest.raises(FrozenInstanceError):
        ANCHORS[0].expected = 1  # type: ignore[misc]
    assert isinstance(ANCHORS[0], Anchor)


def test_a_small_shape_move_is_a_changed_stream_not_a_changed_dataset(capsys) -> None:
    """Thirteen rows in 6.3 million is not "you built the wrong thing".

    The first version of the report had no tolerance on the shape anchor, and it
    misfired the first time it mattered: the substream refactor moved `agg_rows`
    by 0.0002% and the output announced that every other anchor was meaningless -
    at the exact moment those anchors were the values being adopted. A report
    that cries wrong-dataset over a rounding error trains people to skip its
    headline, which is the one line it needs them to read.
    """
    shape = next(a for a in ANCHORS if a.name == SHAPE_ANCHOR)
    nudged = shape.expected + 13  # the real move, on the real anchor
    results = [
        Result(a, nudged if a.name == SHAPE_ANCHOR else a.expected, a.name != SHAPE_ANCHOR)
        for a in ANCHORS
    ]
    assert report(results) is False, "a moved anchor must still fail"
    out = capsys.readouterr().out
    assert "not the build these figures describe" not in out
    assert "the shape matches" in out.lower(), "the report should say the dataset is the right one"


def test_a_different_dataset_still_says_so(capsys) -> None:
    """The tolerance must not swallow the case it was written for.

    CI's 30-day slice holds 495,162 rows against 6,324,308 - 92% below, four
    orders of magnitude outside the tolerance. If widening the boundary ever
    lets that read as "the same dataset with different draws", the report is
    back to inviting someone to debug net revenue on a warehouse they never
    meant to build.
    """
    results = [
        Result(a, 495_162.0 if a.name == SHAPE_ANCHOR else a.expected, a.name != SHAPE_ANCHOR)
        for a in ANCHORS
    ]
    assert report(results) is False
    out = capsys.readouterr().out
    assert "not the build these figures describe" in out


def test_the_shape_tolerance_sits_between_the_two_cases() -> None:
    """Stated as a relationship, so neither bound can be edited into the other.

    Below it: a changed random stream, measured at 13 rows in 6,324,308. Above
    it: a different dataset, measured at the 30-day slice's 495,162. The gap
    between those is four orders of magnitude, so the boundary is not a tuned
    number - but it is only defensible while it is provably inside that gap.
    """
    expected = next(a for a in ANCHORS if a.name == SHAPE_ANCHOR).expected
    stream_change = 13 / expected
    different_dataset = abs(495_162 - expected) / expected
    assert stream_change < SHAPE_TOLERANCE < different_dataset
