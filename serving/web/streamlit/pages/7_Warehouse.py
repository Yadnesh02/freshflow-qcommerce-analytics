"""Warehouse — the base rows underneath every other page.

The other six pages answer questions the registry has already framed. This one
exists for the question that comes next: *why is that number what it is?* The
honest answer to that is usually a handful of rows, and until now reading them
meant leaving the app for a Python prompt.

**This page is base data, and it says so.** Every other page carries the claim
that its numbers were compiled from `semantic/metrics.yml`; that claim would be
false here, so the footer does not make it. What does carry over is the
provenance: each grid shows the exact statement that produced it, for the same
reason a tile does. Gate G3 says no number reaches the screen that the registry
never declared - it does not say an analyst may never look at a stored row, and
the distinction is kept by the fact that nothing on this page can compute an
aggregate. The only aggregation on offer is none.

**It reads what the build actually holds, not what dbt would build.** The
published slice carries marts only, so on the deployed app the staging views
are simply absent from the picker rather than present and broken.

**There is deliberately no query box.** `test_there_is_no_endpoint_that_takes_
raw_sql` forbids one, and it is right to: an API that accepts a statement makes
every guarantee about where numbers come from a convention rather than a
property. So the only things this page can ask for are a relation and a window
into it, and both are whitelisted against `information_schema` before they
reach a FROM clause. Ad-hoc SQL belongs in a client pointed at the file, which
is outside this app and outside the gate.

Reads only from the metrics API.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from serving.web.streamlit.components import (  # noqa: E402
    frame,
    guard,
    page_header,
    show_query,
)
from serving.web.streamlit.session import get_client  # noqa: E402

PAGE_SIZES = [25, 50, 100, 250, 500]

# Why each layer is shaped the way it is. Keyed on schema, because the schema is
# the materialisation decision: staging is views, marts is tables.
LAYER_NOTES = {
    "staging": (
        "Views. They store their SELECT text and no rows at all — the parquet in `data/raw/` "
        "is rescanned on every read, which is why they are absent from the published slice."
    ),
    "marts": (
        "Tables. Materialised because the API and these pages read them repeatedly, so the "
        "build pays the storage once and every query gets the scan speed."
    ),
    "seeds": "Small CSVs under version control, loaded as tables.",
}

st.set_page_config(page_title="Warehouse", page_icon=":material/database:", layout="wide")
client = get_client()

page_header(
    "Warehouse",
    "Check the rows behind a figure before you act on it — or find the ones the metric hides.",
)

catalogue = client.warehouse_catalogue()
if not guard(catalogue, "The warehouse catalogue"):
    st.stop()

health = client.health()
open_file = health.data[0].get("warehouse", "") if health.ok and health.data else ""

relations = catalogue.data
st.caption(
    f"Reading **{Path(open_file).name or 'the configured warehouse'}** — "
    f"{len(relations)} relations across {len({r['schema_name'] for r in relations})} schemas. "
    "These are stored rows, not registry metrics: the numbers here were never declared in "
    "`metrics.yml`, and the grid shows its own statement instead."
)

# ------------------------------------------------------------------ selection
schemas = sorted({r["schema_name"] for r in relations})
left, right = st.columns([1, 3])
schema_name = left.selectbox(
    "Schema", schemas, index=schemas.index("marts") if "marts" in schemas else 0
)

in_schema = [r for r in relations if r["schema_name"] == schema_name]
labels: dict[str, dict] = {}
for relation in in_schema:
    size_hint = "view" if relation["rows"] is None else f"{relation['rows']:,} rows"
    labels[f"{relation['name']}  ·  {size_hint}"] = relation
picked = labels[right.selectbox("Relation", list(labels))]

if schema_name in LAYER_NOTES:
    st.caption(LAYER_NOTES[schema_name])

# ------------------------------------------------------------------- relation
st.subheader(f"{schema_name}.{picked['name']}")
if picked["note"]:
    st.info(picked["note"])
else:
    st.caption(
        "No description stored in the file. The Python-written marts — forecasts, expiry risk, "
        "the optimiser's recommendations — never pass through dbt, so `persist_docs` never "
        "reaches them."
    )

columns = picked["columns"]
tabs = st.tabs(["Rows", f"Columns ({len(columns)})"])

# ----------------------------------------------------------------------- rows
with tabs[0]:
    c1, c2, c3, c4 = st.columns([1, 1, 2, 1])
    size = c1.selectbox("Page size", PAGE_SIZES, index=1)
    page = c2.number_input("Page", min_value=1, value=1, step=1)
    order_by = c3.selectbox("Order by", ["(none)"] + [c["name"] for c in columns])
    order_dir = c4.selectbox("Direction", ["asc", "desc"], disabled=order_by == "(none)")

    rows = client.warehouse_rows(
        schema_name,
        picked["name"],
        limit=size,
        offset=(int(page) - 1) * size,
        order_by=None if order_by == "(none)" else order_by,
        order_dir=order_dir,
    )
    if guard(rows, f"{schema_name}.{picked['name']}"):
        st.dataframe(frame(rows), use_container_width=True, height=520)
        show_query(rows, key=f"rows-{schema_name}-{picked['name']}")

# -------------------------------------------------------------------- columns
with tabs[1]:
    st.caption(
        "Types as the database reports them. Notes are the dbt column descriptions, read back "
        "out of the file's own COMMENTs rather than out of the manifest."
    )
    st.dataframe(
        [
            {"#": i, "column": c["name"], "type": c["type"], "note": c["note"] or ""}
            for i, c in enumerate(columns, start=1)
        ],
        use_container_width=True,
        hide_index=True,
        height=min(560, 45 + 35 * len(columns)),
    )

# --------------------------------------------------------------------- footer
st.divider()
st.caption(
    "Base rows, served through the same API as every other page — but not through the metric "
    "registry, which is why this page makes no claim that its numbers were declared in "
    "`semantic/metrics.yml`. Every grid above shows the statement that produced it."
)
