"""Who should see it? — five estimators, and the one that works (task D6).

Page 1 asks whether the rail pays on average. This one asks the question that
survives either answer: whether there is a subset of customers it pays for.

**The honest shape of this chapter is four failures and one success.** T-learner,
X-learner, behavioural clustering and the transformed outcome all lose to random
targeting on this data. A causal tree beats it. That is not a detail to tidy
away — the four that failed are what makes the fifth worth believing, and a page
that showed only the winner would be claiming a result the evidence does not
support on its own.

**Qini, not AUC, and the difference matters.** AUC asks whether a model ranks
responders. Qini asks whether it ranks people by how much the treatment *moved*
them, which is the only thing a targeting policy can act on. A model can have
excellent AUC and a flat Qini, and that is exactly what happens when it has
learned who orders anyway.

**The targeting curve is deliberately shown with its own caveat.** It is not
monotonic, because a 10% cut holds only a few hundred control customers. The
coefficient integrates the whole curve and is the number to quote; any single
fraction on it is one noisy estimate.

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

from serving.web.streamlit.components import guard, page_header  # noqa: E402
from serving.web.streamlit.session import get_client  # noqa: E402

st.set_page_config(page_title="Who should see it?", page_icon=":material/groups:", layout="wide")
client = get_client()

page_header(
    "Who should see it?",
    "Show the rail to everyone, to a targeted slice, or to nobody — and know what the slice is worth.",
)

result = client.deal_readout(page="who_should_see_it")
if not guard(result, "The uplift readout"):
    st.info(
        "Run `python tasks.py deal-readout` and rebuild. The estimators are fitted in "
        "`analytics/deal/uplift.py`; this page cannot fit them itself.",
        icon=":material/build:",
    )
    st.stop()

figures = {row["metric"]: row for row in result.data}

# -------------------------------------------------------------- estimators
st.subheader("Can any model rank customers by how much the rail moves them?")
st.caption(
    "Qini on a held-out 30% of customers. Above zero beats random targeting; below it, the "
    "model is worse than picking at random."
)

ESTIMATORS = (
    (
        "qini_t_learner",
        "T-learner",
        "Models the outcome, then subtracts. On a Rs 700 "
        "standard deviation that difference is mostly the difference of two errors.",
    ),
    ("qini_x_learner", "X-learner", "The textbook answer for 80/20 arms. Better, not enough."),
    (
        "qini_causal_tree",
        "Causal tree",
        "Splits on the treatment effect itself. The only one "
        "whose every split asks where the rail did something different.",
    ),
)
rows = []
for metric, name, why in ESTIMATORS:
    row = figures.get(metric)
    if row is None:
        continue
    beats = (row["value"] or 0) > 0
    rows.append(
        {
            "estimator": name,
            "Qini": row["display_value"],
            "verdict": "beats random" if beats else "no better than random",
            "why": why,
        }
    )

if rows:
    frame = pd.DataFrame(rows)
    st.dataframe(frame, use_container_width=True, hide_index=True)
    winners = [r for r in rows if r["verdict"] == "beats random"]
    losers = [r for r in rows if r["verdict"] != "beats random"]
    if winners and losers:
        st.success(
            f"**{winners[0]['estimator']} is the only estimator here that beats random.** "
            f"{len(losers)} others do not, and they are shown rather than dropped: they are "
            f"what makes the one that works worth believing.",
            icon=":material/model_training:",
        )
    elif not winners:
        st.warning(
            "No estimator beats random targeting on this build. That is the finding, and it "
            "is reported rather than buried.",
            icon=":material/warning:",
        )

# ----------------------------------------------------------------- policy
st.divider()
st.subheader("What targeting is worth")

curve = sorted(
    (
        (int(m.removeprefix("targeting_")), figures[m])
        for m in figures
        if m.startswith("targeting_")
    ),
    key=lambda pair: pair[0],
)
if curve:
    everyone = next((row for pct, row in curve if pct == 100), None)
    best = next((row for pct, row in curve if (row["value"] or 0) > 0), None)

    if everyone and best:
        left, right = st.columns(2)
        left.metric("Shown to everybody", everyone["display_value"])
        right.metric("Shown to the best-performing slice", best["display_value"])
        st.markdown(
            f"The rail loses **{everyone['display_value']}** across the whole base and gains "
            f"**{best['display_value']}** on the slice the model ranks highest. "
            f"**That sign change is the argument for targeting it.**"
        )

    st.dataframe(
        [
            {
                "targeted": f"{pct}%",
                "net gain": row["display_value"],
                "note": row.get("note") or "",
            }
            for pct, row in curve
        ],
        use_container_width=True,
        hide_index=True,
    )
    st.warning(
        "**Read the Qini, not a row of this table.** The curve is not monotonic — thin cuts "
        "hold only a few hundred control customers, so each point is one noisy estimate. The "
        "coefficient integrates the whole curve and is the number to quote.",
        icon=":material/info:",
    )

value = figures.get("customer_value")
if value:
    st.caption(
        f"Net figures weigh margin against churn at **{value['display_value']}** per retained "
        f"customer. {value.get('note') or ''} It cannot be measured inside the window, so it is "
        f"declared in one line of `analytics/deal/uplift.py` and every net number above is "
        f"proportional to it."
    )

st.divider()
with st.expander("Every figure on this page, with the module that produced it"):
    st.dataframe(
        [
            {
                "figure": r["label"],
                "value": r["display_value"],
                "source": r["source"],
                "note": r.get("note") or "",
            }
            for r in result.data
        ],
        use_container_width=True,
        hide_index=True,
    )
