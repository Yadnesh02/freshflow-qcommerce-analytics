"""The published-figures report asks real questions of a real schema (task S5.0).

There is nothing here to assert about values - the report has no expectations,
which is the whole point of it being a report rather than a gate. What can go
wrong is that a query stops matching the schema, or that two figures quietly
become the same query wearing different names, and either failure would show up
as a plausible-looking table nobody double-checks.

The queries have already earned this: the first draft referenced a
`fulfilled_units` column that does not exist, labelled the 17,293 figure as
at-risk batches when the docstring quoting it means expired ones, and counted
flagged price *intervals* as though they were the 33 stacked store-SKU-days -
the exact 3x overcount that had to be corrected once already.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from analytics.published import FIGURES, report

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)


def test_no_two_figures_run_the_same_query() -> None:
    """Two names over one query is a table that looks corroborated and is not."""
    assert len({f.name for f in FIGURES}) == len(FIGURES), "duplicate figure names"
    assert len({f.sql for f in FIGURES}) == len(FIGURES), "two figures run the same query"


def test_every_figure_says_where_it_is_quoted() -> None:
    """A figure nobody publishes does not belong in a report about published figures.

    The `quoted_in` field is what turns the output into a checklist: without it
    a reader has the new value and no idea which file still carries the old one.

    Checked by looking for a citable source rather than by length. The first
    version of this test demanded more than fifteen characters and failed on
    "S4.3 (42 slots)", which is a perfectly good citation at exactly fifteen -
    a length threshold measures prose, not whether anything was cited.
    """
    import re

    cites = re.compile(r"S\d\.\d|\.py|\.yml|README|plan|registry|metrics\.yml|page|tile|docstring")
    for figure in FIGURES:
        assert cites.search(figure.quoted_in), (
            f"{figure.name} says '{figure.quoted_in}', which names no task, file or surface - "
            "so a reader has the new value and nowhere to go and change the old one"
        )


def test_units_are_declared_from_the_known_set() -> None:
    """A typo in the unit silently formats money as a row count."""
    assert {f.unit for f in FIGURES} <= {"count", "money", "percent", "coefficient"}


@pytest.mark.needs_warehouse
def test_every_query_runs_against_the_real_schema(full_year) -> None:
    """The failure this catches is a column rename, and it is not hypothetical.

    Deliberately asserts execution and a non-null answer, never a value: the
    values are the thing being re-derived, and pinning them here would make this
    a second anchors file that has to be updated in lockstep with the first.
    """
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        con.execute("set enable_progress_bar=false")
        for figure in FIGURES:
            value = con.execute(figure.sql).fetchone()[0]
            assert value is not None, f"{figure.name} returned NULL"
    finally:
        con.close()


@pytest.mark.needs_warehouse
def test_the_report_never_fails_on_a_partly_built_warehouse(full_year) -> None:
    """It reports; it does not gate. A missing mart must not stop the rest printing.

    `warehouse.yml` runs this after the optimisers, but somebody running it on a
    freshly built warehouse has no `rec_*` tables, and the useful behaviour then
    is fifteen figures and three "unavailable" lines - not a traceback that
    hides the fifteen.
    """
    con = duckdb.connect(":memory:")
    try:
        assert report(con) == 0
    finally:
        con.close()
