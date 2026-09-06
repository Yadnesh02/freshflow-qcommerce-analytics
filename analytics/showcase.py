"""Run every query in sql_showcase/ and report what each returned (task S5.8).

    python tasks.py sql-showcase
    python tasks.py sql-showcase --warehouse serving/demo/freshflow_demo.duckdb

A runner rather than a test, for the case the test cannot serve: opening the
folder and seeing what the queries actually say about this build. The gate lives
in `tests/test_sql_showcase.py`, which asserts the same fifteen files run and
return rows - this one prints their shape and the first line of each question,
so a documentation pass or an interview rehearsal has one command.

**It does not fail on an empty result.** A query that declares `needs_days: 120`
returns nothing against a 30-day slice, and that is the slice being too short
rather than the query being broken - the test knows the difference because it
reads the declaration. Here the row count is printed and the reader can see it.
A non-zero exit is reserved for a query that did not run at all.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
SHOWCASE = ROOT / "sql_showcase"
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

QUESTION = re.compile(r"^--\s*question:\s*(.+)$", re.MULTILINE)


def question_of(path: Path) -> str:
    match = QUESTION.search(path.read_text(encoding="utf-8"))
    return match.group(1).strip() if match else "(no question declared)"


def run(warehouse: Path) -> int:
    queries = sorted(SHOWCASE.glob("*.sql"))
    if not queries:
        print(f"\033[31mno .sql files in {SHOWCASE}\033[0m", file=sys.stderr)
        return 1
    if not warehouse.exists():
        print(
            f"\033[31mno warehouse at {warehouse}\033[0m\n  Build one:  python tasks.py build",
            file=sys.stderr,
        )
        return 1

    print(f"\n  {len(queries)} showcase queries against {warehouse}\n")
    connection = duckdb.connect(str(warehouse), read_only=True)
    failures = 0
    try:
        for query in queries:
            try:
                frame = connection.execute(query.read_text(encoding="utf-8")).fetchdf()
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                failures += 1
                print(f"  \033[31mFAIL\033[0m {query.stem}")
                print(f"         {type(exc).__name__}: {str(exc).splitlines()[0]}")
                continue

            shape = f"{len(frame):>5,} rows x {len(frame.columns):>2} cols"
            colour = "\033[32m" if len(frame) else "\033[33m"
            print(f"  {colour}ok\033[0m   {query.stem:<38} {shape}")
            print(f"         {question_of(query)}")
    finally:
        connection.close()

    if failures:
        print(f"\n\033[31m  {failures} of {len(queries)} did not run\033[0m", file=sys.stderr)
        return 1
    print(f"\n\033[32m  all {len(queries)} ran\033[0m")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--warehouse", type=Path, default=WAREHOUSE)
    return run(parser.parse_args(argv).warehouse)


if __name__ == "__main__":
    raise SystemExit(main())
