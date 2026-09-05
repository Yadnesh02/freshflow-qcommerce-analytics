"""The dashboard reads only from the API, and the pages actually render (S3.8).

S3.8's gate is one sentence - no direct DuckDB call anywhere in `serving/web/` -
and it is worth more than it sounds. It is the last link in the chain that makes
gate G3 structural: `metrics.yml` -> resolver -> API -> page. Break it once, with
one convenient `duckdb.connect` for a chart that was awkward to express as a
metric, and every guarantee about where numbers come from silently becomes a
convention instead.

So the gate is enforced by walking the AST rather than by grepping for a string,
because `import duckdb` is only the most obvious spelling of it.

The second half of the file executes each page. A Streamlit page is a script, so
"it imports" proves almost nothing - a NameError three quarters of the way down
only appears when the page runs. `AppTest` runs them the way Streamlit does.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "serving" / "web"
PAGES = sorted((WEB / "streamlit" / "pages").glob("*.py"))
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

# The one module in the package allowed to perform I/O, and it speaks HTTP.
IO_MODULE = WEB / "api_client.py"


@pytest.fixture(autouse=True, scope="module")
def _release_the_warehouse():
    """Hand the warehouse back when this module is done with it.

    Streamlit caches the API client for the life of the process, which is
    exactly right in production - one container, one app, one read handle - and
    a leak inside a test session. DuckDB allows many readers or one writer, so a
    read connection still open here makes `test_incremental.py`'s `dbt run` fail
    with a lock error two modules later, and the traceback points at dbt rather
    than at the page that never let go.
    """
    yield
    import streamlit as st

    from serving.api.main import warehouse

    st.cache_resource.clear()
    warehouse.close()


FORBIDDEN_ROOTS = {"duckdb", "pandas.io.sql", "sqlalchemy"}


def _modules() -> list[Path]:
    return sorted(p for p in WEB.rglob("*.py") if p.name != "__init__.py")


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


# ================================================== the gate
@pytest.mark.parametrize("module", _modules(), ids=lambda p: str(p.relative_to(WEB)))
def test_no_dashboard_module_imports_a_database_driver(module: Path) -> None:
    """The gate, checked on the syntax tree rather than on the text.

    A grep for 'duckdb' misses `importlib.import_module("duck" + "db")` and,
    more realistically, catches it in a comment and fails for nothing. The AST
    sees imports and only imports.
    """
    offending = _imported_roots(module) & FORBIDDEN_ROOTS
    assert not offending, (
        f"{module.relative_to(ROOT)} imports {sorted(offending)}. The dashboard reaches "
        f"the warehouse through the metrics API or not at all - otherwise a number can "
        f"appear on screen that the registry never declared."
    )


def test_only_one_module_performs_io(module: Path = IO_MODULE) -> None:
    """Every other module gets its data by asking that one.

    Not a style preference: it is what makes the gate above checkable at a
    glance and what makes swapping the in-process transport for a deployed API
    a one-file change.
    """
    assert IO_MODULE.exists()
    for other in _modules():
        if other == IO_MODULE:
            continue
        roots = _imported_roots(other)
        assert "httpx" not in roots, (
            f"{other.relative_to(ROOT)} speaks HTTP directly. All of it goes through "
            f"{IO_MODULE.relative_to(ROOT)}."
        )


def test_the_api_client_never_imports_the_warehouse_module() -> None:
    """It imports the FastAPI app, which is a different thing.

    The app opens the connection inside its own lifespan; the client only ever
    holds an ASGI transport. If this module ever imported `serving.demo_data`
    or duckdb directly, the boundary would exist on a diagram and not in the
    code.
    """
    roots = _imported_roots(IO_MODULE)
    assert "duckdb" not in roots
    tree = ast.parse(IO_MODULE.read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "serving.demo_data" not in imported


# ================================================== the pages run
@pytest.mark.slow
@pytest.mark.parametrize("page", [WEB / "streamlit" / "Home.py", *PAGES], ids=lambda p: p.stem)
def test_every_page_renders_without_error(page: Path) -> None:
    """A Streamlit page is a script, so importing it proves nothing.

    A typo in a column name three quarters of the way down a page surfaces only
    when the page runs - and on a deployed app, only when a reader clicks the
    tab. AppTest executes it the way Streamlit does.
    """
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    os.environ["FRESHFLOW_WAREHOUSE"] = str(WAREHOUSE)
    app = AppTest.from_file(str(page), default_timeout=120)
    app.run()

    assert not app.exception, f"{page.name} raised: " + "; ".join(
        str(e.value)[:200] for e in app.exception
    )


@pytest.mark.slow
def test_the_hero_page_shows_a_ranked_queue_with_money_on_it() -> None:
    """The plan's claim about this page, checked rather than asserted.

    A rupee-valued action queue is what makes it a queue rather than a table:
    every row is one decision with a deadline and a number attached. If the
    dataframe ever renders empty or unranked, the page has stopped making that
    claim true.
    """
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    os.environ["FRESHFLOW_WAREHOUSE"] = str(WAREHOUSE)
    hero = next(p for p in PAGES if "Expiry" in p.name)
    app = AppTest.from_file(str(hero), default_timeout=120)
    app.run()

    assert not app.exception
    assert app.dataframe, "the hero page rendered no action queue"

    queue = app.dataframe[0].value
    assert len(queue) > 0, "the action queue is empty"
    assert "value" in queue.columns, "the queue has no rupee column - it is a table, not a queue"
    values = queue["value"].tolist()
    assert values == sorted(values, reverse=True), "the queue is not ranked by money at risk"


def test_an_empty_transfer_queue_explains_itself_rather_than_reporting_nothing() -> None:
    """An engine can be empty for two very different reasons, and the page must say which.

    The markdown queue is empty because the measurement said so - every fitted
    elasticity is inside the unit interval - and "nothing recommended" is the
    honest reading of it. The transfer queue is empty on the *deployed* build
    for a reason that is not a result at all: the demo slice carries five of the
    fourteen stores, a transfer needs both ends inside it, and every arc the
    engine found has one endpoint the slice does not carry. Rendering the same
    bare caption for both presents a filtering artefact as a finding, on the one
    page whose whole claim is that its numbers mean what they appear to mean.
    """
    page = next(p for p in PAGES if "Action_Queue" in p.name)
    tree = ast.parse(page.read_text(encoding="utf-8"))
    notes = next(
        (
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "EMPTY_NOTES" for t in node.targets)
        ),
        None,
    )
    assert notes is not None, "the page has no EMPTY_NOTES table"
    keys = {k.value for k in notes.keys if isinstance(k, ast.Constant)}
    assert "transfer" in keys, (
        "the transfer engine has no empty-state note, so on the deployed slice it reports "
        "'nothing recommended' for arcs that were in fact recommended and then filtered"
    )


def test_no_empty_state_note_hardcodes_a_recommendation_count() -> None:
    """The note may explain the filter; it may not quote a number the build decides.

    How many transfers the engine finds is a property of the warehouse in front
    of it - three on the full estate, none inside the five-store slice - so a
    digit written into the copy is correct for exactly one build and silently
    wrong for every rebuild after it. This is the same failure as a skip that
    never expires: it keeps reading as true long after it stopped being checked.
    """
    import re

    page = next(p for p in PAGES if "Action_Queue" in p.name)
    tree = ast.parse(page.read_text(encoding="utf-8"))
    notes = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "EMPTY_NOTES" for t in node.targets)
    )
    written_numbers = re.compile(
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b", re.IGNORECASE
    )
    allowed = {"five", "fourteen", "both", "one"}  # the slice's own shape, fixed by demo_slice.py
    for value in notes.values:
        assert isinstance(value, ast.Constant), "empty-state notes must be literal strings"
        found = {m.lower() for m in written_numbers.findall(value.value)}
        assert found <= allowed, (
            f"empty-state note quotes a build-dependent count {sorted(found - allowed)}; "
            "say why the queue is empty, not how many rows another build would have"
        )
