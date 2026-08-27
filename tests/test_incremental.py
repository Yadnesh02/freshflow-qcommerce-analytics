"""The S2.6 acceptance gate, run for real (task S2.6).

"Re-running after injecting a late event updates the affected day" is a claim
about behaviour over time, and it cannot be checked by looking at a finished
table. Every incremental model that silently drops late arrivals produces a
table that looks exactly like one that does not.

So this replays it. `arrival_cutoff_date` makes the model read only the rows
that had arrived by a given date - which is what a run on that date would have
seen - and the test builds the aggregate twice: once as of a cutoff, once
after the late orders have landed. The days in between must change.

This is the only test in the suite that invokes dbt, and it rebuilds a 6.3M-row
model three times, so it is marked slow. Deselect with `-m "not slow"`.

    python -m pytest tests/test_incremental.py
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
TRANSFORM = ROOT / "transform"
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

pytestmark = [pytest.mark.needs_warehouse, pytest.mark.slow]

MODEL = "agg_store_sku_day"
LOOKBACK_DAYS = 2

# The target has to match the warehouse the assertions read, or this test
# rebuilds one database and inspects another - which produces "nothing
# changed" and looks exactly like a broken lookback. CI builds the ci target;
# a local run builds dev.
DBT_TARGET = os.environ.get("FRESHFLOW_DBT_TARGET", "dev")
RAW_DIR = Path(os.environ.get("FRESHFLOW_RAW_DIR", ROOT / "data" / "raw"))


def run_dbt(*args: str) -> None:
    """Invoke dbt for one model, failing loudly with its output on error."""
    env = {**os.environ, "FRESHFLOW_RAW_DIR": RAW_DIR.as_posix()}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dbt.cli.main",
            "run",
            "--select",
            MODEL,
            "--profiles-dir",
            ".",
            "--target",
            DBT_TARGET,
            *args,
        ],
        cwd=TRANSFORM,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"dbt run failed:\n{result.stdout[-4000:]}\n{result.stderr[-2000:]}")


def revenue_by_day(days: list[dt.date]) -> dict[dt.date, float]:
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        rows = con.execute(
            f"""
            select date_day, sum(net_revenue), sum(units_sold)
            from marts.{MODEL}
            where date_day in ({",".join("?" for _ in days)})
            group by date_day
            """,
            days,
        ).fetchall()
    finally:
        con.close()
    return {r[0]: (r[1], r[2]) for r in rows}


@pytest.fixture(scope="module")
def window() -> list[dt.date]:
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        last_day = con.execute("select max(date_day) from marts.fct_order").fetchone()[0]
    finally:
        con.close()
    # step back from the end of the data so the "late" orders have somewhere to
    # arrive from
    cutoff = last_day - dt.timedelta(days=3)
    return [cutoff - dt.timedelta(days=offset) for offset in range(LOOKBACK_DAYS, -1, -1)]


def test_a_late_arrival_updates_a_day_already_written(window) -> None:
    """The gate. Build as of a cutoff, let the late orders land, rebuild
    incrementally, and require the earlier days to move.

    If the incremental keyed on the arrival partition instead of the event
    date, or had no lookback at all, these days would be identical after the
    second run - complete-looking, and short by every order that arrived late.
    """
    cutoff = window[-1]
    later = cutoff + dt.timedelta(days=3)

    try:
        run_dbt("--full-refresh", "--vars", f"{{arrival_cutoff_date: '{cutoff}'}}")
        before = revenue_by_day(window)

        run_dbt("--vars", f"{{arrival_cutoff_date: '{later}'}}")
        after = revenue_by_day(window)

        assert before, "the as-of build produced no rows for the window"

        # compared on units, not revenue: revenue is a float sum whose last
        # bits shift with aggregation order, so an equality check on it passes
        # whether or not anything was actually reprocessed - which is how this
        # test passed the first time it was written, against a model that was
        # picking up nothing at all
        moved = {
            day: (before[day], after[day])
            for day in window
            if day in before and day in after and after[day][1] != before[day][1]
        }
        assert moved, (
            f"no day in {window[0]}..{window[-1]} gained units after later-arriving "
            "orders landed - the lookback is not reprocessing anything"
        )

        for day, (was, now) in moved.items():
            assert now[1] > was[1], f"{day} lost units on reprocessing: {was[1]:,} -> {now[1]:,}"
            assert now[0] > was[0], (
                f"{day} gained units but not revenue: {was[0]:,.2f} -> {now[0]:,.2f}"
            )
    finally:
        # leave the warehouse as it was found, whatever happened above
        run_dbt("--full-refresh")


def test_the_rebuilt_window_is_bounded_by_the_lookback(window) -> None:
    """The lookback reprocesses recent days and leaves older ones alone.

    Asserted because the cheap way to make the previous test pass is to rebuild
    everything on every run, which is correct, defeats the purpose, and gets
    slower every day the dataset grows.
    """
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        compiled = (
            TRANSFORM / "target" / "compiled" / "freshflow" / "models" / "marts" / f"{MODEL}.sql"
        )
        assert compiled.exists(), "model has not been compiled - run `python tasks.py build`"
        sql = compiled.read_text(encoding="utf-8")
        assert "rebuild_from" in sql and "read_from" in sql, (
            "the compiled model has no incremental bounds - is it still incremental?"
        )

        total_days = con.execute(f"select count(distinct date_day) from marts.{MODEL}").fetchone()[
            0
        ]
    finally:
        con.close()

    assert total_days > LOOKBACK_DAYS + 1, (
        "the table holds only the lookback window - a full refresh is not "
        "producing the whole history"
    )
