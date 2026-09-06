"""The showcase queries run, and say what they are for (task S5.8).

S5.8's gate is two clauses - "each runs against the warehouse; each has the
business question in a comment" - and both halves are checked here, because a
folder of SQL nobody executes is the easiest thing in a portfolio to let rot.
A query that stopped compiling when a column was renamed looks exactly like one
that still works, right up until somebody opens it in an interview.

**`needs_days` is what makes the row-count assertion honest.** CI builds a
30-day slice and the full warehouse covers a year, so "every query returns
rows" is true on one and false on the other for reasons that have nothing to do
with the SQL: a cohort triangle needs several months before it has a second
cohort to show. Each file declares the span it needs, and the row assertion
applies only when the warehouse in front of it is at least that long. The
declaration lives in the file rather than in a list here, so adding a query
cannot forget to update an exemption list somewhere else - and a query that
needs a year cannot quietly be marked as needing a week without the change
appearing in the diff of the query itself.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)
SHOWCASE = ROOT / "sql_showcase"
QUERIES = sorted(SHOWCASE.glob("*.sql"))

# the plan asks for fifteen, and a count is the one thing a glob cannot drift on
EXPECTED_COUNT = 15

HEADER = re.compile(r"^--\s*(question|technique|needs_days|needs_tables):\s*(.+)$", re.MULTILINE)


def _header(path: Path) -> dict[str, str]:
    return {key: value.strip() for key, value in HEADER.findall(path.read_text(encoding="utf-8"))}


def _ids(path: Path) -> str:
    return path.stem


def _needs_tables(path: Path) -> list[str]:
    """Tables a query needs that not every build produces.

    `ci.yml` generates a 30-day slice and runs dbt, and stops there - the
    five Sprint 4 optimisers run in `warehouse.yml`, so nine marts including
    `mart_expiry_risk` and the four `rec_*` tables exist only on the full
    build. A query reading one of them is not broken on the CI slice, it is
    inapplicable, and the difference has to be declared rather than guessed
    from a catalogue error.
    """
    declared = _header(path).get("needs_tables", "")
    return [name.strip() for name in declared.split(",") if name.strip()]


def _skip_if_tables_absent(connection, query: Path) -> None:
    missing = [
        table
        for table in _needs_tables(query)
        if not connection.execute(
            "select count(*) from information_schema.tables "
            "where table_schema || '.' || table_name = ?",
            [table],
        ).fetchone()[0]
    ]
    if missing:
        pytest.skip(
            f"{query.name} needs {missing}, which this build does not carry - "
            "ci.yml stops after dbt build; warehouse.yml runs the optimisers and "
            "asserts these"
        )


@pytest.fixture
def warehouse():
    """A read-only connection per test, opened and closed.

    Deliberately not session-scoped. DuckDB's file lock is exclusive against
    writers, so a connection held open across the session blocks
    test_incremental's dbt rebuild - which surfaces as a lock error from dbt
    and reads like a dbt fault rather than a fixture one. tests/conftest.py
    carries the same warning for the same reason.
    """
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    connection = duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        yield connection
    finally:
        connection.close()


# ========================================================== the contract
def test_the_showcase_has_the_number_of_queries_the_plan_asks_for() -> None:
    assert len(QUERIES) == EXPECTED_COUNT, (
        f"the plan asks for {EXPECTED_COUNT} queries and sql_showcase/ has {len(QUERIES)}"
    )


@pytest.mark.parametrize("query", QUERIES, ids=_ids)
def test_every_query_states_its_business_question(query: Path) -> None:
    """The literal acceptance clause: the business question, in a comment.

    Checked as a header field rather than by looking for any comment at all,
    because every one of these files is heavily commented and a test that
    passes on the presence of `--` would pass on a file that explains its
    window frame and never says what it is for.
    """
    header = _header(query)
    assert "question" in header, (
        f"{query.name} has no `-- question:` header, so a reader has SQL and no idea "
        "which decision it supports"
    )
    assert len(header["question"]) > 25, (
        f"{query.name}'s question is {header['question']!r}, which is a label rather than "
        "a question somebody would actually ask"
    )


@pytest.mark.parametrize("query", QUERIES, ids=_ids)
def test_every_query_names_its_technique_and_data_span(query: Path) -> None:
    header = _header(query)
    assert "technique" in header, f"{query.name} does not say what it is demonstrating"
    assert "needs_days" in header, (
        f"{query.name} does not declare `-- needs_days:`, so the row-count assertion "
        "cannot tell a query returning nothing from a warehouse too short to answer it"
    )
    assert header["needs_days"].isdigit(), (
        f"{query.name} declares needs_days={header['needs_days']!r}, which is not a number"
    )


@pytest.mark.parametrize("query", QUERIES, ids=_ids)
def test_no_query_hardcodes_a_store_or_sku(query: Path) -> None:
    """A query pinned to one row of data is a screenshot, not a query.

    The identifiers in this warehouse are generated, so a literal `FF-AND-01`
    or `SKU-00042` is correct for exactly the build it was written against and
    returns nothing on the next one - the same silent-empty failure `needs_days`
    exists to prevent, arriving by a different route.
    """
    body = query.read_text(encoding="utf-8")
    # strip comments: the prose legitimately discusses specific defects
    statements = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("--"))
    literals = re.findall(r"'(?:FF-[A-Z]{3}-\d+|SKU-\d+|BAT-\d+)'", statements)
    assert not literals, (
        f"{query.name} pins {sorted(set(literals))} into the SQL; these ids are generated "
        "per build, so the query returns nothing on any other warehouse"
    )


@pytest.mark.parametrize("query", QUERIES, ids=_ids)
def test_every_query_is_listed_in_the_readme(query: Path) -> None:
    """An index that silently stops covering the folder is worse than none.

    The README is the first thing a reader opens, so a query missing from its
    table is a query nobody finds. Checked on the file's number rather than its
    full name, because the table is written for a human and the filenames are
    written for a sort order.
    """
    readme = (SHOWCASE / "README.md").read_text(encoding="utf-8")
    number = query.stem.split("_", 1)[0]
    assert f"| {number} |" in readme, (
        f"{query.name} is not in sql_showcase/README.md's table, so it exists in the "
        "folder and nowhere a reader would look"
    )


# ========================================================== they actually run
@pytest.mark.needs_warehouse
@pytest.mark.parametrize("query", QUERIES, ids=_ids)
def test_every_query_runs_against_the_warehouse(query: Path, warehouse) -> None:
    """The other half of the gate, and the half that catches a renamed column."""
    _skip_if_tables_absent(warehouse, query)
    sql = query.read_text(encoding="utf-8")
    try:
        result = warehouse.execute(sql).fetchdf()
    except Exception as exc:  # noqa: BLE001 - the message is the point
        pytest.fail(f"{query.name} did not run:\n  {type(exc).__name__}: {exc}")

    assert result is not None
    assert len(result.columns) > 0, f"{query.name} returned no columns"


@pytest.mark.needs_warehouse
@pytest.mark.parametrize("query", QUERIES, ids=_ids)
def test_every_query_returns_rows_when_the_window_is_long_enough(
    query: Path, warehouse, dataset_days: int
) -> None:
    """A query that runs and returns nothing has not been shown to work.

    Skipped - with the span in the message - only when the warehouse genuinely
    cannot answer it. On the full build nothing is skipped, so this cannot
    become a permanent pass.
    """
    _skip_if_tables_absent(warehouse, query)
    needs = int(_header(query)["needs_days"])
    if dataset_days < needs:
        pytest.skip(
            f"{query.name} declares needs_days={needs} and this warehouse covers "
            f"{dataset_days}; the full build asserts it"
        )

    rows = warehouse.execute(query.read_text(encoding="utf-8")).fetchdf()
    assert len(rows) > 0, (
        f"{query.name} returned no rows against a {dataset_days}-day warehouse, which "
        f"covers the {needs} days it declares it needs"
    )
