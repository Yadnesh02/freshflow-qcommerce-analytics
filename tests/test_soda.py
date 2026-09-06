"""The freshness check is a gate, which means it has been seen to fail (S5.7).

S5.7's acceptance criterion is one line - "injected staleness triggers an
alert" - and it is the whole point of the task rather than a formality. This
project has now shipped six checks that could pass without their mechanism ever
firing: a sweep at an inelastic coefficient, a private-label floor at
`int(0.30 * 3)`, a CRN gate an inert arm satisfies, a SUTVA filter with no
cross-boundary arc to drop, a parallel-trends check inside a single world, and a
single-setting sweep printing "NO" as though a finding had failed. A freshness
check on a warehouse where nothing is stale is exactly that shape.

So the two tests that matter here are a pair. One builds a coverage table where
every feed arrives through the last day and requires the scan to come back
clean; the other truncates a single feed and requires the same scan to go red.
A change that made the check vacuous - a typo in the column name, a `where`
clause that can never match - passes the first and fails the second.

These run Soda in its own interpreter through `uv`, so they are slow and are
marked as such.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality import scan as soda  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None, reason="Soda runs through `uv run --isolated`"
)

FIRST_DAY = dt.date(2026, 1, 1)
LAST_DAY = dt.date(2026, 1, 10)
FEEDS = ("pos_orders", "clickstream", "wms_inventory_movement")


def _coverage_table(path: Path, stale_feed: str | None = None) -> None:
    """A miniature dq_source_coverage, optionally with one feed cut short.

    Built by hand rather than by running dbt, so the test states the condition
    it is testing in the rows themselves. A fixture that had to build the real
    warehouse would take forty minutes and would make the failing case depend
    on the simulator emitting a defect on demand.
    """
    rows = []
    for feed in FEEDS:
        # the stale feed stops three days before the window closes
        last_seen = LAST_DAY - dt.timedelta(days=3) if feed == stale_feed else LAST_DAY
        day = FIRST_DAY
        while day <= last_seen:
            rows.append(
                (feed, day, True, FIRST_DAY, last_seen, 100, True, False, last_seen < LAST_DAY)
            )
            day += dt.timedelta(days=1)

    con = duckdb.connect(str(path))
    con.execute("create schema if not exists marts")
    con.execute(
        """
        create table marts.dq_source_coverage (
            source_name varchar,
            date_day date,
            expects_daily boolean,
            first_seen_date date,
            last_seen_date date,
            row_count bigint,
            is_present boolean,
            is_missing_partition boolean,
            is_stale boolean
        )
        """
    )
    con.executemany("insert into marts.dq_source_coverage values (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.close()


# ============================================== the pair that makes it a gate
@pytest.mark.slow
def test_the_freshness_check_passes_when_every_feed_is_current(tmp_path: Path) -> None:
    """The control. Without it, the failing test below proves only that Soda runs."""
    warehouse = tmp_path / "clean.duckdb"
    _coverage_table(warehouse)

    code = soda.scan(warehouse, results_file=tmp_path / "clean.json")

    assert code == 0, (
        "the freshness check went red on a warehouse where every feed arrives through the "
        "last day, so it cannot distinguish a stale feed from a healthy one"
    )


@pytest.mark.slow
def test_injected_staleness_fails_the_freshness_check(tmp_path: Path) -> None:
    """S5.7's acceptance criterion, as an executable statement.

    One feed stops arriving three days before the others. Nothing else about
    the warehouse changes - no row is deleted from any fact, no total moves -
    which is exactly why this failure mode is dangerous and why the check has
    to be the thing that catches it.
    """
    warehouse = tmp_path / "stale.duckdb"
    _coverage_table(warehouse, stale_feed="clickstream")

    code = soda.scan(warehouse, results_file=tmp_path / "stale.json")

    assert code == 1, (
        "a feed that stopped arriving three days before the window closed did not fail the "
        "freshness check - the check passes on stale data and is therefore not a gate"
    )


@pytest.mark.slow
def test_the_failing_scan_names_the_feed_that_went_quiet(tmp_path: Path) -> None:
    """An alert that does not say which feed is not an alert, it is a mood.

    The scan result is what the Data Quality page renders, so the failing rows
    have to carry the source name rather than only a count.
    """
    import json

    warehouse = tmp_path / "stale.duckdb"
    results = tmp_path / "stale.json"
    _coverage_table(warehouse, stale_feed="clickstream")
    soda.scan(warehouse, results_file=results)

    payload = json.loads(results.read_text(encoding="utf-8"))
    failed = [c for c in payload["checks"] if c["outcome"] == "fail"]

    assert failed, "the scan recorded no failed check"
    assert any("stopped arriving" in c["name"] for c in failed)
    assert payload["hasFailures"] is True


# ====================================================== the isolation boundary
def test_soda_is_not_a_project_dependency() -> None:
    """Installing it here would downgrade the thing it exists to check.

    soda-core caps protobuf below the version dbt-core 1.11 requires, so a
    resolver satisfying both drags dbt-core back to 1.8.8 - which is the
    version that builds the warehouse Soda is scanning. The isolation is the
    feature; this test is what stops a future `uv add soda-core-duckdb` from
    quietly undoing it.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "soda" not in pyproject.lower(), (
        "soda has been added to pyproject.toml. It runs through `uv run --isolated` "
        "precisely so that it cannot constrain dbt-core - see quality/soda/configuration.yml"
    )


def test_the_scan_command_actually_isolates() -> None:
    """The two flags that make the boundary real, asserted rather than assumed."""
    command = soda.command(Path("anywhere.duckdb"), Path("results.json"))

    assert "--isolated" in command and "--no-project" in command, (
        "the scan no longer runs in its own environment, so Soda's resolution can reach "
        "the project's"
    )
    assert soda.SODA_PACKAGE.count("==") == 1, "the checker must be pinned"


# ============================================================== the exit codes
@pytest.mark.parametrize(
    ("soda_exit", "expected", "why"),
    [
        (0, 0, "everything passed"),
        (1, 0, "a warning is a thing to look at, not a thing to stop for"),
        (2, 1, "a failure stops the build"),
        (3, 1, "an error means the scan did not produce a verdict"),
        (99, 1, "an exit code we do not recognise is not a pass"),
    ],
)
def test_warnings_do_not_fail_the_build(soda_exit: int, expected: int, why: str) -> None:
    """The same distinction S5.6 drew between the anchors gate and the figures report.

    The clickstream outage warns on every healthy build. If that failed the
    build, the first thing anybody would do is stop running the scan.
    """
    assert soda.interpret(soda_exit) == expected, why
