"""Assert the warehouse rebuilt to the same numbers it built to before (task S5.0).

Seven figures, measured once on a full-year build and checked on every rebuild
after it. Run it against a warehouse produced by
`tasks.py simulate --days 365 --seed 42` followed by `tasks.py build` and the
five optimisers; any other dataset will disagree on all of them, which is why
the shape check below runs first and says so rather than printing seven
failures.

**These figures were measured on a clean GitHub runner, not on the development
laptop.** The first set was not: it came from the laptop, and this file existed
to ask whether that mattered. It did not - `warehouse.yml` reproduced all seven
on 2026-09-05 at `4348626`, which is what confirmed the published numbers were
undamaged. They were then re-derived here at `379997d`, because `04737af` gave
every simulator component its own random substream and that changes every draw.
The two structural anchors did not move at all (14 stores, 23 elasticity cells);
the others shifted by between 0.2% and 2.1%, which is what a different stream
through the same process looks like.

**Why this exists, and what it does not do.** On 2026-09-05 the development
laptop was confirmed to have faulty non-ECC RAM - `mdsched` events 1102 and
1202. Sprint 5 therefore moved the authoritative build to GitHub Actions, and
the first question that move raises is whether the figures already written into
the README, the plan and the model docstrings were the *right* ones. These
anchors answer that, in one direction: if a clean runner reproduces all seven,
the numbers already published were not damaged. If it does not, the laptop's
build was the wrong one and the documents get corrected - never this file.

**These anchors are not an integrity check, and treating them as one is exactly
the mistake that let the last corruption through.** The phantom `FF-LPA-00`
store lived in `agg_store_sku_day` for a whole session while every total tied:
the row count was unchanged, net revenue was unchanged, and it was caught only
because a dbt foreign-key test asked whether every `store_id` existed in
`dim_store`. So a green run here means "the build reproduced", not "the data is
clean". The 359 dbt tests are the integrity check; run them too, and read a
failure there as more serious than a failure here.

**Counts are compared exactly and money to the paise, for different reasons.**
A row count is either the same or the data changed - there is no tolerance to
give. Money cannot be compared exactly, and not because of any fault: DuckDB
sums floats in whatever order its threads finish, so the last bits of every
summed column move between runs. Measured drift on `mart_customer_360` was
1.5e-11 absolute and 5.4e-16 relative, which is twenty orders of magnitude
below a paise. Comparing at one paise is therefore loose enough never to fail
on thread scheduling and still tight enough that no corruption anyone would
care about survives it.

There is deliberately no `--update` flag. A disagreement is a decision about
which build to believe, and it belongs to a human who has looked at the dbt
test results first.

    python tasks.py anchors
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

# One paise. See the module docstring: the float drift this has to tolerate is
# ~1.5e-11, so this is not a tuned threshold, it is a business-meaningful unit
# chosen because it is enormously larger than the noise and still small enough
# that nothing worth catching hides under it.
MONEY_TOLERANCE_INR = 0.01

# The dataset these figures describe. Any other and every anchor is meaningless.
BUILD = "tasks.py simulate --days 365 --seed 42, then build, then the five optimisers"

# How far the shape anchor may move before this is a *different dataset* rather
# than the same one built from different draws.
#
# The first version had no tolerance, and it misfired the first time it mattered:
# the substream refactor moved `agg_rows` by thirteen rows in 6.3 million, and
# the report announced "this is not the build these figures describe - every
# other anchor below is meaningless" over a 0.0002% difference. The other five
# were not meaningless at all; they were the values being adopted.
#
# The two cases are orders of magnitude apart and easy to separate. A genuinely
# different dataset is the 30-day CI slice at 495,162 rows - 92% below. A
# changed random stream moves the row count by a rounding error, because the
# same configuration still runs the same stores over the same days. 1% sits in
# the enormous gap between them, so this is a boundary rather than a tuned
# threshold.
SHAPE_TOLERANCE = 0.01


@dataclass(frozen=True)
class Anchor:
    """One measured figure, its query, and why it is worth pinning."""

    name: str
    sql: str
    expected: float
    money: bool
    why: str


ANCHORS: tuple[Anchor, ...] = (
    Anchor(
        name="agg_rows",
        sql="select count(*) from marts.agg_store_sku_day",
        expected=6_324_308,
        money=False,
        why="the grain of the warehouse - wrong here means a different dataset, not a defect",
    ),
    Anchor(
        name="net_revenue",
        sql="select sum(net_revenue) from marts.agg_store_sku_day",
        expected=432_431_889.30,
        money=True,
        why="gate G2 ties this to raw order totals exactly; it is the figure the README quotes",
    ),
    Anchor(
        name="order_items",
        sql="select count(*) from marts.fct_order_item",
        expected=4_249_658,
        money=False,
        why="the deduplicated order book, net of returns - the denominator under most metrics",
    ),
    Anchor(
        name="writeoff_value",
        sql="select sum(writeoff_value) from marts.agg_store_sku_day",
        expected=8_055_476.64,
        money=True,
        why="Rs 80.55 L, the wastage the whole project exists to reduce",
    ),
    Anchor(
        name="distinct_stores",
        sql="select count(distinct store_id) from marts.agg_store_sku_day",
        expected=14,
        money=False,
        why=(
            "the one anchor that would have caught FF-LPA-00, and only because a bit flip "
            "invented a fifteenth store - had it landed in a store that already existed, "
            "this would have passed alongside every other total"
        ),
    ),
    Anchor(
        name="elasticity_cells",
        sql="select count(*) from marts.mart_price_elasticity",
        expected=23,
        money=False,
        why="S4.1's fitted grid; 9 of these are unidentified and S4.2 guards on that, not on null",
    ),
    Anchor(
        name="rec_markdown_rows",
        sql="select count(*) from marts.rec_markdown",
        expected=724,
        money=False,
        why="candidate batches scored by S4.2 - note the optimiser then marks down none of them",
    ),
)

SHAPE_ANCHOR = "agg_rows"


@dataclass(frozen=True)
class Result:
    anchor: Anchor
    actual: float | None
    ok: bool
    error: str | None = None


def _matches(anchor: Anchor, actual: float) -> bool:
    if anchor.money:
        return abs(actual - anchor.expected) <= MONEY_TOLERANCE_INR
    return int(actual) == int(anchor.expected)


def check(con: duckdb.DuckDBPyConnection) -> list[Result]:
    """Evaluate every anchor against an open connection."""
    results: list[Result] = []
    for anchor in ANCHORS:
        try:
            value = con.execute(anchor.sql).fetchone()[0]
        except duckdb.Error as exc:  # a missing table is a result, not a crash
            results.append(Result(anchor, None, False, str(exc).splitlines()[0]))
            continue
        if value is None:
            results.append(Result(anchor, None, False, "query returned NULL"))
            continue
        results.append(Result(anchor, float(value), _matches(anchor, float(value))))
    return results


def _format(value: float | None, money: bool) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}" if money else f"{int(value):,}"


def report(results: list[Result], warehouse: Path = WAREHOUSE) -> bool:
    """Print the table. Returns True when every anchor holds."""
    width = max(len(r.anchor.name) for r in results)
    print(f"\n  anchors for: {BUILD}")
    print(f"  warehouse:   {warehouse}\n")
    for r in results:
        mark = "\033[32mok\033[0m  " if r.ok else "\033[31mFAIL\033[0m"
        expected = _format(r.anchor.expected, r.anchor.money)
        actual = r.error or _format(r.actual, r.anchor.money)
        line = f"  {mark} {r.anchor.name:<{width}}  expected {expected:>16}"
        print(line if r.ok else f"{line}  got {actual}")

    failed = [r for r in results if not r.ok]
    if not failed:
        print(f"\n  \033[32mall {len(results)} anchors hold\033[0m")
        return True

    # A wrong shape means a different dataset, and then the other six failures
    # are noise. Say that instead of letting someone debug net revenue - but
    # only when the shape is wrong by enough to mean it. See SHAPE_TOLERANCE.
    shape = next(r for r in results if r.anchor.name == SHAPE_ANCHOR)
    wrong_dataset = not shape.ok and (
        shape.actual is None
        or abs(shape.actual - shape.anchor.expected) / shape.anchor.expected > SHAPE_TOLERANCE
    )
    if wrong_dataset:
        print(
            f"\n  \033[31m{SHAPE_ANCHOR} disagrees, so this is not the build these figures "
            f"describe.\033[0m\n"
            f"  Expected: {BUILD}\n"
            f"  Every other anchor below is meaningless until that matches."
        )
        return False

    print(f"\n  \033[31m{len(failed)} of {len(results)} anchors failed\033[0m")
    for r in failed:
        print(f"    {r.anchor.name}: {r.anchor.why}")
    print(
        "\n  The shape matches, so this IS the right dataset and the figures genuinely differ.\n"
        "  Read the dbt test results before deciding which build to believe - a foreign-key\n"
        "  or uniqueness failure there points at the data; a clean dbt run with anchors moved\n"
        "  points at the build that produced the expectations. Correct the documents, not\n"
        "  this file, if the clean-runner figure is the trustworthy one."
    )
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--warehouse", type=Path, default=WAREHOUSE, help="warehouse to check (default: dev)"
    )
    args = parser.parse_args(argv)

    if not args.warehouse.exists():
        print(f"\n  no warehouse at {args.warehouse} - run `python tasks.py build` first")
        return 1

    con = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        # otherwise the progress bar floods stdout and buries the table
        con.execute("set enable_progress_bar=false")
        return 0 if report(check(con), args.warehouse) else 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
