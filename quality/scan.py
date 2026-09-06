"""Run Soda against a built warehouse, in an environment of its own (task S5.7).

    python tasks.py soda
    python tasks.py soda --warehouse serving/demo/freshflow_demo.duckdb

**Soda runs out-of-process on purpose, and this module is that boundary.** The
reasoning is in `quality/soda/configuration.yml`: soda-core caps protobuf below
the version dbt-core 1.11 requires, so installing it into this project would
mean downgrading the transformation layer to satisfy the thing that checks it.
`uv run --isolated` gives Soda its own interpreter, its own resolution and its
own duckdb, and the only thing crossing the boundary is a database file and a
JSON result. Nothing here imports soda, and `uv sync` never sees it.

**Warnings are not build failures, and the exit codes encode that.** Soda exits
0 when everything passed, 1 when something warned, 2 on a failure and 3 on an
error. This maps 1 down to success and 2 and 3 up to failure, for the same
reason S5.6 made the anchors check blocking and the figures report not: a build
that goes red for a warning teaches people to stop reading the scan, and a
warning nobody reads is worse than no warning. The known clickstream outage
warns on every healthy build - see the note in `checks/freshness.yml`.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SODA_DIR = ROOT / "quality" / "soda"
CONFIGURATION = SODA_DIR / "configuration.yml"
CHECKS_DIR = SODA_DIR / "checks"
DEFAULT_WAREHOUSE = ROOT / "data" / "warehouse" / "freshflow.duckdb"
DEFAULT_RESULTS = ROOT / "reports" / "soda_scan.json"

# Pinned, because an unpinned checker that changes its own semantics between
# runs cannot be the thing that tells you whether the data changed.
SODA_PACKAGE = "soda-core-duckdb==3.5.6"
# soda-core 3.5.6 imports distutils, which Python 3.12 removed. setuptools puts
# it back. Without this the scan dies on import with ModuleNotFoundError before
# it opens the warehouse, which reads like a broken warehouse and is not one.
SHIM_PACKAGE = "setuptools"

DATA_SOURCE = "freshflow"

# Soda's own exit codes. Anything not listed is treated as a failure.
PASSED, WARNED, FAILED, ERRORED = 0, 1, 2, 3


def checks_files() -> list[Path]:
    return sorted(CHECKS_DIR.glob("*.yml"))


def command(warehouse: Path, results_file: Path) -> list[str]:
    """The isolated invocation, built separately so a test can read it.

    Kept as data rather than as a formatted string so that
    tests/test_soda.py can assert the isolation flags are present without
    running a scan - `--isolated` and `--no-project` are the whole reason this
    module exists, and a refactor that drops one would otherwise be caught only
    by a dependency resolution failure weeks later.
    """
    return [
        "uv",
        "run",
        "--no-project",
        "--isolated",
        "--python",
        "3.12",
        "--with",
        SODA_PACKAGE,
        "--with",
        SHIM_PACKAGE,
        "soda",
        "scan",
        "-d",
        DATA_SOURCE,
        "-c",
        str(CONFIGURATION),
        "-srf",
        str(results_file),
        *[str(p) for p in checks_files()],
    ]


def scan(warehouse: Path, results_file: Path = DEFAULT_RESULTS) -> int:
    """Scan `warehouse`; return a shell exit code, not Soda's."""
    if shutil.which("uv") is None:
        print(
            "\033[31muv is not on PATH, and Soda runs through it.\033[0m\n"
            "  Soda is deliberately not a project dependency - see "
            "quality/soda/configuration.yml.\n"
            "  Install uv: https://docs.astral.sh/uv/getting-started/installation/",
            file=sys.stderr,
        )
        return 1

    if not warehouse.exists():
        print(
            f"\033[31mno warehouse at {warehouse}\033[0m\n"
            "  Build one first:  python tasks.py build",
            file=sys.stderr,
        )
        return 1

    if not checks_files():
        # An empty checks directory makes every scan pass, which is the
        # failure this whole file is supposed to detect in the data.
        print(f"\033[31mno check files in {CHECKS_DIR}\033[0m", file=sys.stderr)
        return 1

    results_file.parent.mkdir(parents=True, exist_ok=True)
    # Soda reads the path from the environment so one configuration covers the
    # dev, ci and demo builds.
    env = {**_environ(), "FRESHFLOW_WAREHOUSE": str(warehouse)}
    completed = subprocess.run(command(warehouse, results_file), cwd=ROOT, env=env)

    return interpret(completed.returncode)


def interpret(soda_exit: int) -> int:
    """Map Soda's four exit codes onto pass/fail, and say which happened."""
    if soda_exit == PASSED:
        print("\n\033[32m  every data-quality check passed\033[0m")
        return 0
    if soda_exit == WARNED:
        print(
            "\n\033[33m  warnings, and the build is not failed for them\033[0m\n"
            "    A warning is a thing to look at, not a thing to stop for. The\n"
            "    clickstream outage warns on every healthy build - see the\n"
            "    Data Quality page, which names it from the defect ledger."
        )
        return 0
    if soda_exit == FAILED:
        print("\n\033[31m  a data-quality check FAILED\033[0m", file=sys.stderr)
        return 1
    if soda_exit == ERRORED:
        print(
            "\n\033[31m  Soda could not complete the scan\033[0m\n"
            "    This is an error in the checks or the connection, not a\n"
            "    verdict about the data.",
            file=sys.stderr,
        )
        return 1
    print(f"\n\033[31m  unexpected Soda exit {soda_exit}\033[0m", file=sys.stderr)
    return 1


def _environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=DEFAULT_WAREHOUSE,
        help="warehouse to scan (default: the full-year dev build)",
    )
    parser.add_argument(
        "--results-file",
        type=Path,
        default=DEFAULT_RESULTS,
        help="where to write the machine-readable scan result",
    )
    args = parser.parse_args(argv)
    return scan(args.warehouse, args.results_file)


if __name__ == "__main__":
    raise SystemExit(main())
