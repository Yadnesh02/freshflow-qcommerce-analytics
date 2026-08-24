"""Keeps the metric registry and the warehouse model in sync (task S0.4).

The ERD is the designed model; metrics.yml is what the BI layer promises to
serve from it. Nothing stops those two documents drifting apart except a test,
and drift is cheapest to catch now - before either exists as code.

Parses the mermaid `erDiagram` blocks in docs/img/erd.md and asserts that every
table a metric reads from is actually modelled, and that every column a metric
references exists on that table.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from semantic.registry import load_registry

ROOT = Path(__file__).resolve().parent.parent
ERD_PATH = ROOT / "docs" / "img" / "erd.md"

reg = load_registry()

# Bare SQL keywords. Function names need no listing - a token followed by "("
# is a call, not a column, so the tokenizer drops it structurally.
SQL_KEYWORDS = {
    "case",
    "when",
    "then",
    "else",
    "end",
    "distinct",
    "as",
    "and",
    "or",
    "not",
    "null",
    "is",
    "in",
    "between",
    "like",
    "over",
    "partition",
    "by",
    "asc",
    "desc",
}


def _referenced_columns(expr: str) -> set[str]:
    """Column identifiers in a metric expression.

    Two things are deliberately excluded, because both used to produce false
    positives: quoted string literals ('pass', 'freshness' are values, not
    columns) and any identifier followed by an open paren (a function call).
    """
    without_literals = re.sub(r"'[^']*'", " ", expr)
    tokens = re.findall(r"\b([a-z_][a-z0-9_]*)\b\s*(\()?", without_literals, flags=re.I)
    return {tok.lower() for tok, is_call in tokens if not is_call} - SQL_KEYWORDS


def _parse_erd(path: Path) -> dict[str, set[str]]:
    """Return {table_name: {column, ...}} from every erDiagram block in the file."""
    text = path.read_text(encoding="utf-8")
    tables: dict[str, set[str]] = {}
    # an entity block is:  name {  <type> <column> ...  }
    for match in re.finditer(r"^\s{4}(\w+)\s*\{\n(.*?)^\s{4}\}", text, re.S | re.M):
        name, body = match.group(1), match.group(2)
        cols: set[str] = set()
        for line in body.splitlines():
            # "        string  store_id PK "note""  ->  store_id
            parts = line.strip().split()
            if len(parts) >= 2 and not parts[0].startswith('"'):
                cols.add(parts[1])
        tables[name] = cols
    return tables


ERD = _parse_erd(ERD_PATH)
METRICS = sorted(reg.metrics.values(), key=lambda m: m.name)


def test_erd_file_parses() -> None:
    assert ERD, f"no entities parsed from {ERD_PATH} - has the mermaid syntax changed?"
    assert len(ERD) >= 25, f"only {len(ERD)} entities parsed, expected the full model"


def test_erd_covers_the_documented_catalogue() -> None:
    """A few tables the plan depends on, spot-checked so a rename cannot pass silently."""
    for required in (
        "fct_order_item",
        "fct_inventory_batch",
        "fct_inventory_movement",
        "agg_store_sku_day",
        "mart_expiry_risk",
        "mart_customer_360",
    ):
        assert required in ERD, f"{required} is missing from the ERD"


def test_order_item_carries_the_fefo_batch_key() -> None:
    """The keystone. Without batch_id on the sale, expiry attribution is impossible."""
    assert "batch_id" in ERD["fct_order_item"], (
        "fct_order_item must carry the FEFO-allocated batch_id - it is the join that "
        "makes every expiry, freshness and markdown metric in this project computable"
    )


@pytest.mark.parametrize("metric", METRICS, ids=[m.name for m in METRICS])
def test_metric_source_is_a_modelled_table(metric) -> None:
    assert metric.source in ERD, (
        f"metric '{metric.name}' reads from '{metric.source}', which is not in the ERD. "
        f"Either model the table or point the metric at an existing one."
    )


@pytest.mark.parametrize("metric", METRICS, ids=[m.name for m in METRICS])
def test_metric_columns_exist_on_its_source(metric) -> None:
    """Catch a metric referencing a column its source table does not have."""
    if metric.source not in ERD:
        pytest.skip("source not modelled - covered by test_metric_source_is_a_modelled_table")

    exprs = [e for e in (metric.expression, metric.numerator, metric.denominator) if e]
    referenced = {col for e in exprs for col in _referenced_columns(e)}

    available = {c.lower() for c in ERD[metric.source]}
    missing = sorted(referenced - available)
    assert not missing, (
        f"metric '{metric.name}' references {missing} which do not exist on "
        f"'{metric.source}'. Add the column to the ERD or fix the expression."
    )


@pytest.mark.parametrize("metric", METRICS, ids=[m.name for m in METRICS])
def test_metric_source_can_reach_its_grain(metric) -> None:
    """A metric can only group by a dimension whose join key sits on its source."""
    available = {c.lower() for c in ERD.get(metric.source, set())}
    for dim_name in metric.grain:
        dim = reg.dimension(dim_name)
        # a dimension whose join table IS the source needs no key - it is already there
        if dim.join and dim.join["table"] == metric.source:
            continue
        assert dim.key.lower() in available, (
            f"metric '{metric.name}' grains by '{dim_name}', which needs key "
            f"'{dim.key}' on '{metric.source}'. That column is not modelled there."
        )
