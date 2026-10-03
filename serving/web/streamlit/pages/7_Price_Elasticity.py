"""Price Elasticity — what a discount actually buys (task S4.7).

The page has to carry an awkward result without softening it. Across 23
category x freshness cells, nine found no price response at all, and the ones
that did are all inelastic: every fitted coefficient sits inside the unit
interval, which means cutting price loses more on the units already selling
than it wins on the ones the cut brings in. The markdown optimiser therefore
recommends holding price almost everywhere.

So the chart shows the unidentified cells rather than dropping them. A curve
drawn only through the cells that worked would run smoothly across every
category and imply the last day of shelf life was measured. It was not, in any
category - and that absence is the most decision-relevant thing here, because
the last day is exactly where the old flat ladder cut deepest.

Reads only from the metrics API.
"""

from __future__ import annotations

import sys
from pathlib import Path

import plotly.graph_objects as go
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

# Below this a discount cannot pay for itself: revenue falls with price whenever
# the response is weaker than one-for-one.
ELASTIC_THRESHOLD = -1.0

st.set_page_config(page_title="Price Elasticity", page_icon=":material/percent:", layout="wide")
client = get_client()

page_header(
    "Price Elasticity",
    "How much more sells if the price drops - measured within freshness band, because "
    "stock is discounted *because* it is ageing and the two effects pull opposite ways.",
)

result = client.elasticity()
if not guard(result, "price elasticity"):
    st.stop()

cells = frame(result)

# ------------------------------------------------------------------ the tiles
identified = cells[cells["is_identified"]]
strongest = identified["elasticity_raw"].min() if len(identified) else None

# These three describe the table on this page rather than the business, so they
# are plain st.metric rather than kpi(). `kpi` takes an ApiResult and prints the
# SQL behind it, which is how G3 keeps every business number traceable to the
# registry - passing a locally-computed count through it would borrow that
# provenance for a number the registry never defined. The query behind this page
# is shown in full at the bottom.
left, middle, right = st.columns(3)
with left:
    st.metric("Cells measured", f"{len(identified)} of {len(cells)}")
    st.caption("the rest found no price response at all")
with middle:
    st.metric("Strongest response", f"{strongest:.2f}" if strongest is not None else "--")
    st.caption("still inside the unit interval")
with right:
    st.metric("Elastic cells", f"{int((identified['elasticity_raw'] < ELASTIC_THRESHOLD).sum())}")
    st.caption("a discount pays only past -1.00")

st.info(
    "**No band reaches -1.00, and that decides the markdown policy.** While stock is short "
    "of demand the objective is revenue, which falls with price whenever the response is "
    "weaker than one-for-one. So the optimiser holds price nearly everywhere - and the flat "
    "50% ladder it replaces runs at minus Rs 22.7 lakh of margin a year.",
    icon=":material/info:",
)

# ------------------------------------------------------------------ the curves
st.subheader("Response by days to expiry")

figure = go.Figure()
for category, group in cells.groupby("l1_category"):
    group = group.sort_values("min_days")
    measured = group[group["is_identified"]]
    if len(measured):
        figure.add_trace(
            go.Scatter(
                x=measured["dte_band"],
                y=measured["elasticity_raw"],
                mode="lines+markers",
                name=category,
                error_y={"type": "data", "array": 1.96 * measured["standard_error"]},
            )
        )
    # the cells that found nothing, drawn at zero and hollow. Leaving them out
    # would let each line run smoothly through a gap it cannot actually cross.
    missing = group[~group["is_identified"]]
    if len(missing):
        figure.add_trace(
            go.Scatter(
                x=missing["dte_band"],
                y=[0] * len(missing),
                mode="markers",
                name=f"{category} (not measured)",
                marker={"symbol": "circle-open", "size": 11},
                showlegend=False,
                hovertemplate="%{x}: no price response established<extra></extra>",
            )
        )

figure.add_hline(
    y=ELASTIC_THRESHOLD, line_dash="dot", annotation_text="a discount pays below this line"
)
figure.add_hline(y=0, line_width=1)
figure.update_layout(
    xaxis_title="days to expiry",
    yaxis_title="elasticity (more negative = more responsive)",
    height=460,
    legend={"orientation": "h", "y": -0.25},
)
st.plotly_chart(figure, use_container_width=True)

st.caption(
    "Hollow markers are cells where the confidence interval covers zero. They are shown at "
    "zero because that is what was established, not because the response is zero. Error bars "
    "are 95% intervals on the fitted coefficient."
)

# ------------------------------------------------------------------ the table
st.subheader("Every cell, including the ones that failed")

display = cells.assign(
    used=cells["elasticity_usable"].map(lambda v: "—" if v is None else f"{v:.2f}"),
    interval=cells.apply(
        lambda r: f"{r['elasticity_raw']:+.2f} ± {1.96 * r['standard_error']:.2f}", axis=1
    ),
)[
    [
        "l1_category",
        "dte_band",
        "observations",
        "discounted_observations",
        "interval",
        "is_identified",
        "used",
        "elasticity_basis",
    ]
].rename(
    columns={
        "l1_category": "category",
        "dte_band": "band",
        "observations": "obs",
        "discounted_observations": "discounted",
        "interval": "fitted (95% CI)",
        "is_identified": "measured",
        "used": "used by the optimiser",
        "elasticity_basis": "basis",
    }
)
st.dataframe(display, use_container_width=True, hide_index=True)

st.caption(
    "Two of the unmeasured cells fit at +8.60 and +1.61. Those are not findings - they are a "
    "Poisson fit dividing by a price series with almost no variation in it, on a couple of "
    "hundred observations. Separating them from the cells that did measure something is the "
    "entire job of the identification rule."
)

show_query(result, "elasticity")
api_footer(None)
