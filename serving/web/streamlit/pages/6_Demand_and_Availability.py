"""Demand & Availability — how good the forecast is, honestly (task S3.8).

The page most portfolio dashboards get wrong, because the honest version has
worse numbers on it. WAPE by ABC-XYZ is shown against a zero-forecast reference
of 1.000, and on most classes here the model does not beat it - which is a fact
about intermittent demand rather than a fact about the model, and hiding it
would make every other number on the site less believable.

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

# A forecast of zero everywhere scores exactly this, because the numerator
# collapses to the sum of actuals. It is the line below which a forecast is
# earning something.
ZERO_FORECAST_WAPE = 1.0

st.set_page_config(
    page_title="Demand & Availability", page_icon=":material/trending_up:", layout="wide"
)
client = get_client()

page_header(
    "Demand & Availability",
    "Order to the forecast where it beats guessing, and to a reorder rule where it does "
    "not. The table below says which SKUs are which.",
)

# ------------------------------------------------------------------ the tiles
tiles = st.columns(4)
with tiles[0]:
    kpi(
        "Forecast WAPE",
        client.metric("forecast_wape"),
        "percent_1dp",
        "wape",
        direction="down_is_good",
        help_text="Weighted absolute percentage error. Evaluated on uncensored days only.",
    )
with tiles[1]:
    kpi(
        "Forecast bias",
        client.metric("forecast_bias"),
        "percent_1dp",
        "bias",
        help_text=(
            "Positive by construction: stockouts happen on busy days and scoring excludes "
            "them, so the model predicts the full mean and is graded on a quieter subset. "
            "Watch it for change, not for sign."
        ),
    )
with tiles[2]:
    kpi(
        "Value add vs naive",
        client.metric("forecast_value_add"),
        "percent_1dp",
        "fva",
        direction="up_is_good",
        help_text="Seasonal-naive WAPE minus model WAPE. If this is not positive, the model is theatre.",
    )
with tiles[3]:
    kpi(
        "Days of cover",
        client.metric("days_of_cover"),
        "days_1dp",
        "doc",
        help_text="Capped at shelf life: 20 days of cover on a 3-day product is not cover.",
    )

st.divider()

# ------------------------------------------------------------------ by class
st.subheader("Where forecasting works, and where it does not")

left, right = st.columns([3, 2])

with left:
    wape_by_class = client.metric("forecast_wape", ["abc_class", "xyz_class"], limit=20)
    if guard(wape_by_class, "WAPE by ABC-XYZ"):
        data = frame(wape_by_class)
        data["class"] = data["abc_class"] + data["xyz_class"]
        data = data.sort_values("forecast_wape")

        figure = px.bar(
            data,
            x="class",
            y="forecast_wape",
            labels={"forecast_wape": "WAPE", "class": "ABC-XYZ class"},
            color=data["forecast_wape"] < ZERO_FORECAST_WAPE,
            color_discrete_map={True: "#2a9d8f", False: "#adb5bd"},
        )
        figure.add_hline(
            y=ZERO_FORECAST_WAPE,
            line_dash="dash",
            annotation_text="forecasting zero everywhere scores 1.00",
            annotation_position="top left",
        )
        figure.update_layout(height=400, showlegend=False, margin={"l": 0, "r": 0, "t": 10, "b": 0})
        st.plotly_chart(figure, width="stretch")
        show_query(wape_by_class, "wape_class")

with right:
    st.markdown(
        f"""
#### Read the dashed line first

A forecast of **zero units, every day, every SKU** scores a WAPE of exactly
**{ZERO_FORECAST_WAPE:.2f}** — the error term collapses to the sum of actuals. Any bar
above that line describes a model that is beaten by predicting nothing.

Most bars are above it, and that is not a tuning failure. At store × SKU × day this
demand is intermittent: the best class averages 2.2 units a day with 36% zero days,
and the C/Z tail averages 0.17 across 95% zero days. Nothing places a fractional unit
on the right day when the actual is a coin flip.

#### So what changes tomorrow

**Green bars** — order to the forecast. It is carrying real information.

**Grey bars** — order to a reorder point with safety stock. A daily forecast on those
SKUs is a number that looks precise and is not, and acting on it would be worse than
a rule.
"""
    )

st.divider()

# ------------------------------------------------------------------ availability
st.subheader("What stockouts actually cost")
columns = st.columns(2)

with columns[0]:
    stock_by_category = client.metric("in_stock_pct", ["category"], limit=15)
    if guard(stock_by_category, "In-stock % by category"):
        data = frame(stock_by_category).sort_values("in_stock_pct")
        figure = px.bar(
            data,
            x="in_stock_pct",
            y="category",
            orientation="h",
            labels={"in_stock_pct": "time-weighted in-stock %", "category": ""},
        )
        figure.update_layout(height=380, margin={"l": 0, "r": 0, "t": 10, "b": 0})
        st.plotly_chart(figure, width="stretch")
        show_query(stock_by_category, "isp_cat")

with columns[1]:
    lost_by_category = client.metric("lost_sales_units", ["category"], limit=15)
    if guard(lost_by_category, "Lost sales by category"):
        data = frame(lost_by_category).sort_values("lost_sales_units")
        figure = px.bar(
            data,
            x="lost_sales_units",
            y="category",
            orientation="h",
            labels={"lost_sales_units": "units of demand never served", "category": ""},
        )
        figure.update_layout(height=380, margin={"l": 0, "r": 0, "t": 10, "b": 0})
        st.plotly_chart(figure, width="stretch")
        show_query(lost_by_category, "lost_cat3")

st.info(
    "Lost sales are an estimate, not an observation. A stockout hides the demand it "
    "caused, so these units come from scaling what sold by the share of the day's demand "
    "that had already arrived - the arrival curve, not the clock. A store that ran dry at "
    "07:00 had seen 6.7% of its day, not the 29% the clock suggests.",
    icon=":material/functions:",
)

api_footer(None)
