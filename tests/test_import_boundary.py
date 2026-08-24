"""The rule that makes this project's findings defensible.

The simulator holds the ground-truth data-generating process. If any analytics,
semantic or serving code imported from it, every result would be circular: the
model would be reading the answer instead of inferring it from emitted events.

This test walks the AST of every module outside `simulator/` and fails on any
import that crosses the boundary. Keep it green. When an interviewer asks
"you generated the data, so how do you know it works?", this file is the first
half of the answer and the policy backtest is the second.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_ROOT = "simulator"

# everything here must be able to run against real event data alone
GUARDED_PACKAGES = ["analytics", "semantic", "serving", "orchestration"]

# the orchestrator legitimately *triggers* the simulator as a pipeline step,
# but only via the task runner in a subprocess - never by importing its internals
ALLOWED_EXCEPTIONS: set[Path] = set()


def _guarded_modules() -> list[Path]:
    files: list[Path] = []
    for pkg in GUARDED_PACKAGES:
        pkg_dir = ROOT / pkg
        if pkg_dir.exists():
            files.extend(
                p for p in pkg_dir.rglob("*.py")
                if p.relative_to(ROOT) not in ALLOWED_EXCEPTIONS
            )
    return sorted(files)


def _imported_roots(path: Path) -> set[str]:
    """Top-level package name of every import in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, which cannot escape its own package
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_guarded_packages_exist() -> None:
    """Guard against the boundary test silently passing on an empty tree."""
    assert any((ROOT / pkg).exists() for pkg in GUARDED_PACKAGES), (
        "none of the guarded packages exist - this test would pass vacuously"
    )


@pytest.mark.parametrize("module", _guarded_modules(), ids=lambda p: str(p.relative_to(ROOT)))
def test_analytics_never_imports_simulator(module: Path) -> None:
    offenders = _imported_roots(module) & {FORBIDDEN_ROOT}
    assert not offenders, (
        f"{module.relative_to(ROOT)} imports '{FORBIDDEN_ROOT}'.\n"
        "The analytics layer must infer everything from emitted event data. "
        "If you need a value the simulator knows, emit it as data or estimate it."
    )
