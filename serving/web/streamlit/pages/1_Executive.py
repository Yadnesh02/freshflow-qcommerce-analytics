"""Executive — is the business making money after wastage (task S3.8).

Four tiles and a store ranking. The north star is gross margin after wastage
and markdown, and it is shown next to its guardrail on purpose: margin can be
bought by starving stores of stock, which reads as an improvement here and as
churn on the customers page. The registry declares that pairing, the API
carries it, and this page renders it rather than trusting anyone to remember.

Reads only from the metrics API.
"""

from __future__ import annotations

import sys
from pathlib import Path

import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from serving.web.streamlit.components import (  # noqa: E402
    api_footer,
    frame,
    guard,
    kpi,
    page_header,
    show_query,
)
from serving.web.streamlit.session import get_client  # noqa: E402

st.set_page_config(page_title="Executive", page_icon=":material/monitoring:", layout="wide")
client = get_client()

page_header(
    "Executive",
    "Find the stores losing margin to wastage, and check the guardrail before "
    "congratulating anyone: margin bought by running stores empty shows up here as a win.",
)

# ------------------------------------------------------------------ the tiles
tiles = st.columns(4)
with tiles[0]:
    kpi(
        "GM after wastage & markdown",
        client.metric("gm_awm"),
        "percent_1dp",
        "gmawm",
        direction="up_is_good",
        help_text="The north star. Net revenue less COGS, write-offs and platform-funded subsidy.",
    )
with tiles[1]:
    kpi(
        "90-day retention",
        client.metric("retention_90d"),
        "percent_1dp",
        "ret",
        direction="up_is_good",
        help_text="The guardrail on the north star. Read them together, always.",
    )
with tiles[2]:
    kpi(
        "Wastage value",
        client.metric("wastage_value"),
        "inr",
        "wast",
        direction="down_is_good",
    )
with tiles[3]:
    kpi(
        "In-stock %",
        client.metric("in_stock_pct"),
        "percent_1dp",
        "isp",
        direction="up_is_good",
        help_text="Time-weighted, not a midnight snapshot: a snapshot cannot see the 8pm stockout.",
    )

st.divider()

# ------------------------------------------------------------------ the ranking
st.subheader("Stores, ranked by the north star")
by_store = client.metric("gm_awm", ["store"], limit=20)
wastage_by_store = client.metric("wastage_rate_value", ["store"], limit=20)
stock_by_store = client.metric("in_stock_pct", ["store"], limit=20)

if guard(by_store, "GM-AWM by store"):
    table = frame(by_store)
    for other in (wastage_by_store, stock_by_store):
        if other.ok and other.data:
            table = table.merge(frame(other), on="store", how="left")

    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        column_config={
            "store": st.column_config.TextColumn("Store", width="medium"),
            "gm_awm": st.column_config.NumberColumn("GM-AWM", format="percent"),
            "wastage_rate_value": st.column_config.NumberColumn("Wastage", format="percent"),
            "in_stock_pct": st.column_config.NumberColumn("In stock", format="percent"),
        },
    )
    show_query(by_store, "store_rank")

    if len(table) > 1 and "gm_awm" in table:
        best, worst = table.iloc[0], table.iloc[-1]
        spread = best["gm_awm"] - worst["gm_awm"]
        st.caption(
            f"**{spread * 100:.1f} points** separate {best['store']} from {worst['store']}. "
            f"On a network this size that gap is an operating difference, not a market one - "
            f"the assortment and the prices are the same in both."
        )

st.divider()

# ------------------------------------------------------------------ where margin leaks
left, right = st.columns(2)

with left:
    st.subheader("Where margin leaks")
    by_category = client.metric("wastage_value", ["category"], limit=15)
    if guard(by_category, "Wastage by category"):
        data = frame(by_category).sort_values("wastage_value", ascending=True)
        figure = px.bar(
            data,
            x="wastage_value",
            y="category",
            orientation="h",
            labels={"wastage_value": "₹ written off", "category": ""},
        )
        figure.update_layout(height=380, margin={"l": 0, "r": 0, "t": 10, "b": 0})
        st.plotly_chart(figure, width="stretch")
        show_query(by_category, "waste_cat")

with right:
    st.subheader("Demand that was never served")
    lost = client.metric("lost_sales_units")
    fill = client.metric("fill_rate")
    lost_by_category = client.metric("lost_sales_units", ["category"], limit=15)

    metrics = st.columns(2)
    with metrics[0]:
        kpi("Lost sales (units)", lost, "number_0dp", "lost", direction="down_is_good")
    with metrics[1]:
        kpi("Fill rate", fill, "percent_1dp", "fill", direction="up_is_good")

    if guard(lost_by_category, "Lost sales by category"):
        data = frame(lost_by_category).sort_values("lost_sales_units", ascending=True)
        figure = px.bar(
            data,
            x="lost_sales_units",
            y="category",
            orientation="h",
            labels={"lost_sales_units": "units of demand not served", "category": ""},
        )
        figure.update_layout(height=280, margin={"l": 0, "r": 0, "t": 10, "b": 0})
        st.plotly_chart(figure, width="stretch")
        show_query(lost_by_category, "lost_cat")

st.info(
    "Wastage and lost sales are the two ends of the same decision. Ordering less cuts the "
    "left chart and grows the right one; the north star is what balances them, which is why "
    "it is the number on the wall and neither of these is.",
    icon=":material/balance:",
)

api_footer(None)
