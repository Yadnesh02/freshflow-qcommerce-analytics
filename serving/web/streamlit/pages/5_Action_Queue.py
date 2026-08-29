"""Action Queue — what to do this morning, from four engines (task S4.7).

Sprint 4 produced four optimisers, and a store manager has one morning. This is
their combined output, grouped by engine and ranked within each.

**Grouped rather than ranked globally, and that is not a presentation choice.**
The four engines measure different rupees. A replenishment line's value is money
about to be committed; a transfer's is a write-off avoided; a deal slot's is net
of its own subsidy. Sorting those against one another puts every purchase order
above every transfer and reads as a priority ordering, which it is not. Each row
carries the basis its number is on.

**The markdown queue is usually empty, and that is the finding.** Every fitted
elasticity is inside the unit interval, so cutting price loses more on the units
already selling than it wins on the ones the cut brings in. The engine that was
supposed to produce the most actions produces the fewest, which is what the
measurement said to do.

Reads only from the metrics API.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from serving.web.streamlit.components import (  # noqa: E402
    api_footer,
    frame,
    guard,
    page_header,
    show_query,
)
from serving.web.streamlit.session import get_client  # noqa: E402

ENGINES = {
    "replenish": ("Order", "How much to buy, at a service level set by shelf life"),
    "markdown": ("Mark down", "Cut price only where a cut is measured to pay"),
    "deal_slot": ("Deal rail", "The Rs 11 slots, chosen per store against what is expiring"),
    "transfer": ("Transfer", "Move stock that will not clear to a store that is short"),
}

st.set_page_config(page_title="Action Queue", page_icon=":material/checklist:", layout="wide")
client = get_client()

page_header(
    "Action Queue",
    "Four decision engines, one morning. Every line carries the rupees behind it and the "
    "reason it was chosen.",
)

stores = st.session_state.get("stores")
store = st.selectbox(
    "Store",
    ["All stores", *(stores or [])],
    index=0,
    help="Filter every engine to one dark store",
)
selected_store = None if store == "All stores" else store

# One request per engine, not one for everything. A single call with a limit
# lets whichever engine happens to be busiest crowd the others out of the
# response entirely: replenishment produced 358 lines against three transfers,
# so a shared limit of 400 returned every order and no transfer at all, and the
# page showed "Transfer 0" while the table underneath it held rows. Each engine
# gets its own budget so none can starve another.
results = {
    key: client.action_queue(store=selected_store, action_type=key, limit=200) for key in ENGINES
}

first_failure = next((r for r in results.values() if not r.ok), None)
if first_failure is not None and not guard(first_failure, "the action queue"):
    st.stop()

per_engine = {key: frame(result) for key, result in results.items()}
actions = (
    pd.concat([f for f in per_engine.values() if not f.empty], ignore_index=True)
    if any(not f.empty for f in per_engine.values())
    else pd.DataFrame()
)

for warning in {w for result in results.values() for w in result.warnings}:
    st.warning(warning, icon=":material/warning:")

if actions.empty:
    st.info(
        "No engine has produced recommendations yet. Run `python tasks.py expiry-risk`, then "
        "`markdown`, `deal-slots`, `transfers` and `newsvendor`.",
        icon=":material/info:",
    )
    st.stop()

# ------------------------------------------------------------------ the tiles
counts = actions["action_type"].value_counts()
# Counts of the rows below, not registry metrics, so plain st.metric. See the
# note on the Price Elasticity page for why these do not go through kpi().
columns = st.columns(len(ENGINES))
for column, (key, (label, _)) in zip(columns, ENGINES.items(), strict=True):
    with column:
        st.metric(label, f"{int(counts.get(key, 0)):,}")
        st.caption("lines today")

if int(counts.get("markdown", 0)) == 0:
    st.info(
        "**No markdowns today, and that is the recommendation.** Every fitted elasticity is "
        "weaker than one-for-one, so a discount gives up more on the units already selling "
        "than it wins on the ones it brings in. See the Price Elasticity page.",
        icon=":material/info:",
    )

# ------------------------------------------------------------------ per engine
for key, (label, blurb) in ENGINES.items():
    lines = per_engine[key]
    st.subheader(f"{label} — {len(lines):,} lines")
    st.caption(blurb)

    if lines.empty:
        st.caption("_Nothing recommended._")
        continue

    basis = lines["value_basis"].iloc[0]
    display = lines[["store_id", "sku_id", "sku_name", "detail", "units", "value_inr", "rationale"]]
    st.dataframe(
        display.rename(
            columns={
                "store_id": "store",
                "sku_id": "sku",
                "sku_name": "product",
                "detail": "action",
                "units": "units",
                "value_inr": f"rupees — {basis}",
                "rationale": "why",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

st.caption(
    "Rupee columns are not comparable across engines: an order line is money about to be "
    "spent, a transfer line is money saved. The column header says which."
)

show_query(results["replenish"], "action-queue")
api_footer(None)
