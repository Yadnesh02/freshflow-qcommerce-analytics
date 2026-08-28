"""FreshFlow Control Tower — entry page (task S3.8).

Reads only from the metrics API. Nothing in this package opens a database; the
one module that performs I/O is `serving/web/api_client.py`, and it speaks HTTP.
That is gate G3 made structural: a number can only appear here if it came
through the resolver, and the resolver reads `semantic/metrics.yml`.

    python tasks.py app
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Streamlit executes this file as a script, so the repository root is not on the
# path the way it is for `python -m`. Everything below imports from it.
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from serving.web.streamlit.components import api_footer  # noqa: E402
from serving.web.streamlit.session import get_client  # noqa: E402

st.set_page_config(
    page_title="FreshFlow Control Tower",
    page_icon=":material/inventory_2:",
    layout="wide",
)

client = get_client()

st.title("FreshFlow Control Tower")
st.caption(
    "Perishable inventory for a 14-store Mumbai dark-store network. "
    "Every number here is compiled from a metric registry - click **see query** on any "
    "tile to read the SQL that produced it and the definition that compiled it."
)

health = client.health()
catalogue = client.catalogue()

if not health.ok:
    st.error(
        f"The metrics API is not answering: {health.error} — {health.detail}",
        icon=":material/error:",
    )
    st.stop()

info = health.data[0] if health.data else {}
catalogue_info = catalogue.data[0] if catalogue.ok and catalogue.data else {}

left, middle, right = st.columns(3)
left.metric("Metrics published", info.get("metrics_published", "--"))
middle.metric("Dimensions", len(catalogue_info.get("dimensions", [])) or "--")
right.metric("North star", catalogue_info.get("north_star", "--"))

st.divider()

st.markdown(
    """
### The three pages

**1 · Executive** — is the business making money after wastage, and which stores are not.

**2 · Expiry Control Tower** — the hero page. Every batch about to expire, ranked by
rupees at risk, with the demand forecast that says whether it will sell.

**3 · Demand & Availability** — how good the forecast is, honestly, and what stockouts
cost in demand nobody ever saw.

### Why this is not a dashboard on top of a database

The chain is `metrics.yml` → resolver → API → this page, and there is no other route.
The API has no endpoint that accepts SQL, so a number cannot reach this screen unless
somebody declared it first. That is the difference between a metric layer and a
convention, and it is what **see query** demonstrates on every tile.
"""
)

if catalogue_info.get("guardrail"):
    st.info(
        f"The north star `{catalogue_info['north_star']}` is published with a guardrail: "
        f"`{catalogue_info['guardrail']}`. Margin bought by starving stores of stock shows "
        f"up as churn, not as failure, so the two are read together.",
        icon=":material/balance:",
    )

api_footer(info)
