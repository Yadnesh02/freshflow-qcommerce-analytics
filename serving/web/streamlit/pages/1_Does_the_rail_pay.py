"""Does the rail pay? — the deal's full P&L, naive against causal (task D6).

The project's headline question, and the page exists to show one gap: what the
obvious comparison says against what a randomised holdout says. Everything else
here is supporting detail for that contrast.

**The gap is the page.** +Rs 4.63 per deal order is what you get comparing
orders that took the deal to orders that did not — and deal-takers are not a
random sample of shoppers, so it is an upper bound on attach and blind to
whether the rail brought anybody in at all. The intent-to-treat figures beside
it come from D1's customer-level holdout and include the customers who never
ordered, as zeros, because dropping them would condition on the outcome the rail
is supposed to move.

**Two numbers are deliberately not reconciled on screen.** The naive figure is
per deal-taking ORDER and contains attach only; the ITT is per assigned CUSTOMER
and contains incidence as well. The second contains the first. Showing a
subtraction between them would invent a quantity neither measures.

**Nothing here is computed by this page.** Gate G3 forbids it, and the figures
are expensive besides. `analytics/deal/readout.py` computes them, a mart formats
them, the API serves them, and every row carries the module that produced it.

Reads only from the metrics API.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from serving.web.streamlit.components import guard, page_header  # noqa: E402
from serving.web.streamlit.session import get_client  # noqa: E402

st.set_page_config(page_title="Does the rail pay?", page_icon=":material/paid:", layout="wide")
client = get_client()

page_header(
    "Does the rail pay?",
    "Keep the deal rail, narrow it, or stop it — and know which of those the evidence supports.",
)

result = client.deal_readout(page="does_it_pay")
if not guard(result, "The deal readout"):
    st.info(
        "Run `python tasks.py deal-readout` and rebuild the warehouse. The figures are "
        "computed in `analytics/deal/` and served from `mart_deal_readout`; this page "
        "cannot compute them itself.",
        icon=":material/build:",
    )
    st.stop()

figures = {row["metric"]: row for row in result.data}


def show(metric: str, column, *, help_text: str | None = None) -> None:
    row = figures.get(metric)
    if row is None:
        column.metric(metric, "--")
        return
    column.metric(row["label"], row["display_value"], help=help_text or row.get("note"))


# ------------------------------------------------------- the rail on its own
st.subheader("What the rail costs on its own line")
st.caption(
    "A loss leader by construction — nobody runs a rupee store expecting margin on the item. "
    "The question is what comes back."
)
cost = st.columns(4)
for i, metric in enumerate(("deal_units", "deal_revenue", "deal_cogs", "deal_margin")):
    show(metric, cost[i])

# ------------------------------------------------------------- the contrast
st.divider()
st.subheader("What it earns back: the obvious answer, and the measured one")

left, right = st.columns(2)
with left:
    st.markdown("**Naive** — orders that took the deal against orders that did not")
    naive = figures.get("naive_attach")
    if naive:
        st.metric("Attach, per deal order", naive["display_value"])
    st.caption(
        "Self-selected, and per order rather than per customer. It cannot see whether the "
        "rail brought anyone in, only what else was already in the basket."
    )

with right:
    st.markdown("**Causal** — exposed arm against D1's randomised holdout, per customer")
    itt = figures.get("itt_margin")
    if itt:
        delta = None
        if itt.get("ci_low") is not None:
            delta = f"95% CI [{itt['ci_low']:,.2f}, {itt['ci_high']:,.2f}]"
        st.metric(
            "Total margin effect, per customer",
            itt["display_value"],
            delta=delta,
            delta_color="off",
        )
    st.caption(
        "Intent to treat: customers who never ordered are included, as zeros. Dropping them "
        "would condition on the very outcome the rail is supposed to move."
    )

st.caption(
    "**These are not the same quantity.** The naive figure is per deal-taking order and "
    "contains attach only; the causal one is per assigned customer and contains incidence "
    "as well. The second contains the first, so the difference between them is what a "
    "per-order view cannot see — not an error term."
)

# ------------------------------------------------------------- the verdict
st.divider()
st.subheader("The verdict, with its interval")

verdict = st.columns(3)
show("return_per_rupee", verdict[0])
show("cannibalisation_share", verdict[1])
show("churn_effect", verdict[2])

ret = figures.get("return_per_rupee")
if ret and ret.get("ci_low") is not None:
    low, high = sorted((ret["ci_low"], ret["ci_high"]))
    crosses = low < 1.0 < high
    st.markdown(
        f"Return per rupee of subsidy is **{ret['display_value']}**, 95% CI "
        f"**[{low:,.2f}x, {high:,.2f}x]**."
    )
    if crosses:
        st.warning(
            "That interval crosses 1x. On this window the rail cannot be shown to pay for "
            "itself at all — which is the honest headline, and a better one than a confident "
            "number off a short window would have been.",
            icon=":material/warning:",
        )

churn = figures.get("churn_effect")
mde = figures.get("churn_mde")
if churn and mde:
    st.markdown(
        f"Churn moves **{churn['display_value']}** against a minimum detectable effect of "
        f"**{mde['display_value']}**. "
        + (
            "The effect is not distinguishable from zero, so retention is a plausible "
            "consequence of the rail rather than a measured one."
            if churn.get("is_significant") is False
            else "The effect clears the floor this design can see."
        )
    )

# ---------------------------------------------------------------- provenance
st.divider()
with st.expander("Every figure on this page, with the module that produced it"):
    st.dataframe(
        [
            {
                "figure": r["label"],
                "value": r["display_value"],
                "95% CI": (
                    "--"
                    if r.get("ci_low") is None
                    else f"[{r['ci_low']:,.2f}, {r['ci_high']:,.2f}]"
                ),
                "source": r["source"],
                "note": r.get("note") or "",
            }
            for r in result.data
        ],
        use_container_width=True,
        hide_index=True,
    )

st.caption(
    "Computed in `analytics/deal/`, served from `mart_deal_readout`. These figures do not come "
    "from `semantic/metrics.yml`, which is why this page carries its sources in the table above "
    "rather than claiming the registry behind them."
)
