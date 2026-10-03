"""Expiry Control Tower — the hero page (task S3.8).

Problem P1 is that ops sees "150 units of curd in Andheri" and not "40 of those
expire in 36 hours and only 22 will sell". This page is the second sentence.

The action queue is the point of it. The plan calls a rupee-valued queue a
first-class object rather than a table of numbers, and the distinction is real:
a table invites reading, a queue invites working down it. Every row here is one
decision, with a deadline in hours and a dependable number attached, ranked by
what it costs to ignore.

Reads only `/actions/expiry` and `/metrics/*`. Nothing here opens a database.
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
    format_value,
    frame,
    guard,
    kpi,
    page_header,
    show_query,
)
from serving.web.streamlit.session import get_client  # noqa: E402

st.set_page_config(
    page_title="Expiry Control Tower", page_icon=":material/schedule:", layout="wide"
)
client = get_client()

page_header(
    "Expiry Control Tower",
    "Work down the queue. Every row is stock that will be written off at full landed "
    "cost unless somebody marks it down today.",
)

# ------------------------------------------------------------------ controls
controls = st.columns([2, 2, 3])

# Store options come from the queue itself rather than from the `store`
# dimension, which resolves to store_name while /actions/expiry filters on
# store_id. Mapping one to the other in the browser would mean holding a second
# copy of the dimension here, which is exactly the drift this tier exists to
# avoid.
all_stores = frame(client.expiry_queue(min_value=0, limit=500))
store_ids = (
    sorted(all_stores["store_id"].dropna().unique().tolist()) if not all_stores.empty else []
)

with controls[0]:
    chosen_store = st.selectbox("Store", ["All stores", *store_ids])
with controls[1]:
    min_value = st.number_input("Minimum ₹ at risk", min_value=0, value=100, step=50)
with controls[2]:
    st.caption(
        "The queue shows batches still inside the forecast horizon. Stock already past "
        "expiry is a booked loss and appears in wastage, but no markdown recovers it, "
        "so it is not an action."
    )

# ------------------------------------------------------------------ headline
st.subheader("What is at stake")
tiles = st.columns(3)
with tiles[0]:
    kpi(
        "Value at risk",
        client.metric("expiry_value_at_risk"),
        "inr",
        "var",
        help_text="Landed cost of on-hand units the forecast says will not sell before expiry.",
    )
with tiles[1]:
    kpi(
        "Wastage value",
        client.metric("wastage_value"),
        "inr",
        "wv",
        direction="down_is_good",
        help_text="Already written off at expiry. What the queue exists to reduce.",
    )
with tiles[2]:
    kpi(
        "Wastage rate",
        client.metric("wastage_rate_value"),
        "percent_1dp",
        "wr",
        direction="down_is_good",
    )

st.divider()

# ------------------------------------------------------------------ the queue
st.subheader("Today's action queue")
queue = client.expiry_queue(
    store=None if chosen_store == "All stores" else chosen_store,
    min_value=float(min_value),
    limit=200,
)

if guard(queue, "The action queue"):
    rows = frame(queue)
    total = rows["value_at_risk_inr"].sum()
    st.markdown(
        f"**{len(rows)} decisions · {format_value(total, 'inr')} at risk.** "
        f"Ranked by what it costs to do nothing."
    )

    display = rows.assign(
        hours_left=(rows["days_to_expiry"] * 24).astype(int),
        at_risk=rows["units_at_risk"].round(1),
        value=rows["value_at_risk_inr"].round(0),
        confidence=rows["expiry_risk_score"].round(2),
    )[
        [
            "store_id",
            "sku_name",
            "l1_category",
            "hours_left",
            "qty_remaining",
            "at_risk",
            "confidence",
            "value",
        ]
    ]
    st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        column_config={
            "store_id": st.column_config.TextColumn("Store"),
            "sku_name": st.column_config.TextColumn("SKU", width="medium"),
            "l1_category": st.column_config.TextColumn("Category"),
            "hours_left": st.column_config.NumberColumn("Hours left", format="%d h"),
            "qty_remaining": st.column_config.NumberColumn("On hand"),
            "at_risk": st.column_config.NumberColumn("Units at risk"),
            "confidence": st.column_config.ProgressColumn(
                "P(unsold)", min_value=0.0, max_value=1.0, format="%.2f"
            ),
            "value": st.column_config.NumberColumn("₹ at risk", format="₹%d"),
        },
    )
    show_query(queue, "queue")

    st.caption(
        "**How to read P(unsold).** It ranks write-off risk well - across risk bands the "
        "realised write-off rate runs 0%, 0%, 0%, 1.7%, 42%. It does not rank *total* "
        "unsold stock, because a batch down to its last unit or two often never moves "
        "at all, and that is a picking problem no demand model sees."
    )

st.divider()

# ------------------------------------------------------------------ where it sits
left, right = st.columns(2)

with left:
    st.subheader("Where the risk sits")
    by_category = client.metric("expiry_value_at_risk", ["category"], limit=15)
    if guard(by_category, "Value at risk by category"):
        data = frame(by_category).sort_values("expiry_value_at_risk", ascending=True)
        figure = px.bar(
            data,
            x="expiry_value_at_risk",
            y="category",
            orientation="h",
            labels={"expiry_value_at_risk": "₹ at risk", "category": ""},
        )
        figure.update_layout(height=380, margin={"l": 0, "r": 0, "t": 10, "b": 0})
        st.plotly_chart(figure, width="stretch")
        show_query(by_category, "by_cat")

with right:
    st.subheader("Freshness at the shelf")
    dte = client.metric("dte_at_sale_p10", ["category"], limit=15)
    if guard(dte, "Days-to-expiry at sale"):
        data = frame(dte).sort_values("dte_at_sale_p10", ascending=True)
        figure = px.bar(
            data,
            x="dte_at_sale_p10",
            y="category",
            orientation="h",
            labels={"dte_at_sale_p10": "P10 days to expiry at sale", "category": ""},
        )
        figure.update_layout(height=380, margin={"l": 0, "r": 0, "t": 10, "b": 0})
        st.plotly_chart(figure, width="stretch")
        show_query(dte, "dte")
        st.caption(
            "The 10th percentile, not the mean: the mean hides the tail, and the tail is "
            "the customer who got the two-day-old curd."
        )
