"""What runs tomorrow? — the decision, not the analysis (task D6).

Pages 1 and 2 are evidence. This one is the only page in the three that somebody
acts on: which SKUs take tomorrow's slots, in which stores, and why each was
chosen over the alternatives.

**Every number behind these choices is now measured rather than declared.** D5
re-pointed the allocator's objective at what D2-D4 found — the basket margin is
the conservative end of a causal interval instead of a self-selected constant,
reactivation stays at zero because the evidence points the other way, and
cannibalisation is reported beside the choice rather than charged into it,
because the causal basket figure already nets it.

**The constraints are shown because they are the interesting part.** An
allocator that always picks the highest-value candidate is a sort. This one
cannot: at most one slot per subcategory, a floor on stock so a slot does not
advertise an empty shelf, a shelf-life floor, and a private-label quota. Which
constraint bound is usually why a surprising SKU is on the list.

Reads only from the metrics API.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from serving.web.streamlit.components import frame, guard, page_header, show_query  # noqa: E402
from serving.web.streamlit.session import get_client  # noqa: E402

st.set_page_config(
    page_title="What runs tomorrow?", page_icon=":material/event_upcoming:", layout="wide"
)
client = get_client()

page_header(
    "What runs tomorrow?",
    "Put these SKUs in tomorrow's slots, or override them knowing what the override costs.",
)

readout = client.deal_readout(page="what_runs_tomorrow")
figures = {row["metric"]: row for row in readout.data} if readout.ok else {}

if figures:
    top = st.columns(2)
    for i, metric in enumerate(("slots_recommended", "slot_value_total")):
        row = figures.get(metric)
        if row:
            top[i].metric(row["label"], row["display_value"], help=row.get("note"))
else:
    st.info(
        "No slot recommendations in this build. `rec_deal_slot` is written by "
        "`python tasks.py deal-slots`, which needs the expiry-risk mart and therefore a full "
        "build — `warehouse.yml` produces one.",
        icon=":material/build:",
    )

# ------------------------------------------------------------ the queue
st.divider()
st.subheader("Tomorrow's slots")

queue = client.action_queue(action_type="deal_slot", limit=100)
if guard(queue, "The deal-slot queue"):
    table = frame(queue)
    st.dataframe(table, use_container_width=True, height=440)
    show_query(queue, key="deal-slot-queue")
else:
    st.caption(
        "The action queue serves every engine's output; this page filters it to the deal rail. "
        "An empty queue here means the allocator has not run against this build, not that it "
        "found nothing worth doing."
    )

# ------------------------------------------------- what the choice rests on
st.divider()
st.subheader("What these choices rest on")

st.markdown(
    """
| Term | Where the number comes from |
|---|---|
| **Clearance value** | Landed cost of at-risk stock the slot actually clears — a write-off avoided, capped by what is genuinely dying. Zero for long-life goods, which is why more than half the baseline's slots were worth nothing on this term. |
| **Basket value** | The conservative end of D2's causal interval, not the self-selected constant it replaced. The central estimate is documented and swept rather than used, because at that level it would dwarf every other term and the allocator would simply rank by uptake. |
| **Reactivation** | **Zero**, and D3 made that a finding rather than caution: measured against the holdout the exposed arm churns more, not less, so a positive value has the wrong sign. |
| **Subsidy** | What the slot earns on the item minus what those units would have earned anyway — not `(base − 11) × uptake`, which would bill the discount to units that were never going to sell. |
| **Cannibalisation** | Reported beside the choice, **not charged into it**. The causal basket figure is an arm-level difference and already nets it; adding it would bill the same loss twice. |
"""
)

st.caption(
    "The five constraints — slots per store, one per subcategory, a minimum on-hand, a "
    "shelf-life floor, and a 30% private-label quota — are in "
    "`analytics/optimization/deal_slots.py`, and the quota is a gate rather than a preference."
)
