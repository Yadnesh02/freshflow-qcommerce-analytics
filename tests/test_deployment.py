"""What gets deployed, and what must not (task S3.9).

The deployed container has 1 GB of RAM and installs from `requirements.txt`.
That file is not the project's dependency set and must never become it: dbt,
LightGBM, Dagster, scipy and the simulator's packages build the warehouse and
are never imported by a page. Shipping them would add several hundred megabytes
and minutes of cold start to answer no question.

The risk is drift in both directions and neither announces itself. A package
added to a page and forgotten here breaks the app on deploy, minutes after a
green local run. A package left here after the import that needed it went away
is dead weight that nobody profiles. So the closure is derived from the page
files rather than declared, and compared against the file.

    python -m pytest tests/test_deployment.py
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
ENTRYPOINT = ROOT / "serving" / "web" / "streamlit" / "Home.py"
PAGES = sorted((ROOT / "serving" / "web" / "streamlit" / "pages").glob("*.py"))

FIRST_PARTY = {"serving", "semantic", "analytics", "simulator", "orchestration"}

# These build the warehouse. If one ever appears in the deployed closure, some
# page has reached past the API into the modelling layer.
BUILD_ONLY = {
    "dbt",
    "lightgbm",
    "dagster",
    "scipy",
    "sklearn",
    "statsmodels",
    "pulp",
    "networkx",
    "faker",
    "sqlfluff",
    "pytest",
}

# Import name -> distribution name, where they differ.
DISTRIBUTION = {"yaml": "pyyaml"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _module_file(dotted: str) -> Path | None:
    direct = ROOT / Path(dotted.replace(".", "/") + ".py")
    if direct.exists():
        return direct
    package = ROOT / Path(dotted.replace(".", "/")) / "__init__.py"
    return package if package.exists() else None


def deployed_closure() -> tuple[set[Path], set[str]]:
    """Walk from the pages through first-party imports; collect the rest."""
    seen: set[Path] = set()
    third_party: set[str] = set()
    queue = [ENTRYPOINT, *PAGES]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        for dotted in _imports(current):
            top = dotted.split(".")[0]
            if top in FIRST_PARTY:
                module = _module_file(dotted)
                if module:
                    queue.append(module)
            elif top not in sys.stdlib_module_names:
                third_party.add(DISTRIBUTION.get(top, top))
    return seen, third_party


def declared() -> set[str]:
    names = set()
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(re.split(r"[=<>!~\[]", line)[0].strip().lower())
    return names


# ================================================== the closure
def test_requirements_covers_everything_the_pages_import() -> None:
    """A package missing here breaks the app on deploy, not before."""
    _, needed = deployed_closure()
    missing = {n.lower() for n in needed} - declared()
    assert not missing, (
        f"the deployed app imports {sorted(missing)} which requirements.txt does not "
        f"install. The container would fail on first page load."
    )


def test_requirements_carries_nothing_the_pages_do_not_import() -> None:
    """Dead weight in a 1 GB container that nobody profiles."""
    _, needed = deployed_closure()
    extra = declared() - {n.lower() for n in needed}
    assert not extra, (
        f"requirements.txt installs {sorted(extra)} which nothing in the deployed app "
        f"imports. Either a page stopped using it or it was never needed."
    )


def test_no_build_time_package_reaches_the_deployed_app() -> None:
    """The separation the whole architecture rests on.

    Pages talk to the API, the API talks to the resolver, the resolver reads a
    registry and a warehouse file. None of that needs a modelling library. If
    one appears here, a page has reached past the API into the layer that
    builds the data rather than serves it.
    """
    _, needed = deployed_closure()
    leaked = {n.lower() for n in needed} & BUILD_ONLY
    assert not leaked, (
        f"{sorted(leaked)} is in the deployed import closure. That is a build-time "
        f"dependency, so some page is importing modelling code rather than calling the API."
    )


def test_the_deployed_closure_stays_small() -> None:
    """A bound on how much can quietly accrete before somebody notices.

    Not a style rule: cold start on Community Cloud is roughly linear in what
    pip has to resolve, and the first page view of a portfolio demo is the one
    that matters most.
    """
    modules, packages = deployed_closure()
    assert len(packages) <= 12, (
        f"the deployed app now needs {len(packages)} third-party packages "
        f"({sorted(packages)}). Cold start grows with this."
    )
    assert len(modules) <= 25, f"the deployed closure is {len(modules)} first-party modules"


# ================================================== the entrypoint
def test_the_entrypoint_exists_where_the_task_runner_points() -> None:
    """Streamlit Cloud is configured with this path by hand, so it has to be
    the same one `tasks.py app` uses - otherwise local and deployed diverge and
    the difference only shows up in the cloud."""
    assert ENTRYPOINT.exists(), f"{ENTRYPOINT} is missing"
    runner = (ROOT / "tasks.py").read_text(encoding="utf-8")
    relative = ENTRYPOINT.relative_to(ROOT).as_posix()
    assert relative in runner, (
        f"tasks.py does not launch {relative}; local and deployed would run different files"
    )


def test_every_page_is_reachable_from_the_navigation() -> None:
    """Streamlit builds the sidebar from filenames, so an unnumbered page hides."""
    assert PAGES, "no pages found"
    for page in PAGES:
        assert re.match(r"^\d+_", page.name), (
            f"{page.name} has no ordering prefix and will sort unpredictably in the sidebar"
        )


def test_pinned_versions_match_what_is_installed() -> None:
    """A pin that drifts from the working environment is a deploy that differs
    from every local run."""
    import importlib.metadata as metadata

    mismatched = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, pinned = line.partition("==")
        try:
            installed = metadata.version(name.strip())
        except metadata.PackageNotFoundError:
            pytest.skip(f"{name} is not installed in this environment")
        if installed != pinned.strip():
            mismatched.append((name.strip(), pinned.strip(), installed))
    assert not mismatched, (
        "requirements.txt pins versions that are not what this environment runs: "
        + ", ".join(f"{n} pinned {p}, installed {i}" for n, p, i in mismatched)
    )


def test_the_project_dependency_set_is_not_what_ships() -> None:
    """A sanity check on the premise: the build needs far more than the app.

    If these ever converged it would mean either the app grew a modelling
    dependency or the project stopped having a build, and both are worth
    noticing.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_deps = pyproject.get("project", {}).get("dependencies", [])
    assert len(project_deps) > len(declared()) + 5, (
        f"the project declares {len(project_deps)} dependencies and the deployed app "
        f"{len(declared())}; the separation between build and serve has collapsed"
    )
