"""Shared fixtures for the tests that read a built warehouse.

**Why `full_year` exists.** CI builds a 30-day slice so the pipeline runs in
under a minute, while the tests were written against the 365-day dataset. Most
assertions hold on both - they are anchored to the defect manifest, or to
invariants that do not care how long the window is. A few genuinely do not:
a SKU's price cannot change mid-year inside 30 days, the IPL season is not in
the window, and a history that spans one month cannot demonstrate that
collapsing it into intervals compresses anything.

The wrong fix is to loosen those assertions until they pass on both, because
that costs the full-year run the only version of the test worth having. The
right one is to say what the test needs and skip when it is absent - a skip
reads as "not applicable here", a weakened assertion reads as "passing", and
only one of those is true.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)
MANIFEST = Path(os.environ.get("FRESHFLOW_MANIFEST", ROOT / "data" / "_manifest" / "dirt.json"))

# below this the dataset cannot contain a mid-year price change, a festival
# calendar worth checking, or enough history to compress
FULL_YEAR_DAYS = 300


@pytest.fixture(scope="session")
def dataset_days() -> int:
    """How many days of catalogue the built warehouse actually covers.

    Opens and closes its own connection rather than holding a session-scoped
    one. DuckDB's file lock is exclusive against writers, so a read-only
    connection left open for the whole session blocks test_incremental from
    rebuilding its model - which surfaces as a lock error from dbt and reads
    like a dbt problem rather than a fixture problem.
    """
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    connection = duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        return connection.execute(
            "select count(distinct snapshot_date) from staging.stg_catalog__products"
        ).fetchone()[0]
    finally:
        connection.close()


@pytest.fixture
def full_year(dataset_days):
    """Skip a test that only means something over a long window."""
    if dataset_days < FULL_YEAR_DAYS:
        pytest.skip(
            f"needs a full-year dataset; this warehouse covers {dataset_days} days. "
            "CI builds a 30-day slice on purpose - see tests/conftest.py."
        )


@pytest.fixture(scope="session")
def defect_manifest() -> dict:
    if not MANIFEST.exists():
        pytest.skip("no defect manifest - run `python tasks.py simulate`")
    return {d["key"]: d for d in json.loads(MANIFEST.read_text(encoding="utf-8"))}
