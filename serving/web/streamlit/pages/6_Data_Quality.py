"""Data Quality — is what we are serving still fit to serve (task S5.7).

Three things a reader needs before trusting any other page, in the order they
would ask for them: has any feed stopped arriving, what did the last scan say,
and what was wrong with this data in the first place.

**The defect ledger is on this page rather than buried in the README because
the warehouse was dirtied on purpose.** Eight defects were injected into the raw
feeds and staging repairs them; a data-quality page that showed only green
checks would be hiding the most interesting thing about the build. Each row
names the symptom and the repair, so "the checks pass" reads as "these specific
failures were handled" rather than as "nothing was ever wrong".

**A warning here is expected and the page says why.** The clickstream lost two
partitions to a documented outage that is deliberately not backfilled, so the
coverage check warns on every healthy build. That warning is matched against
the ledger rather than explained by a number typed into this page - a count
written into the copy is correct for one build and quietly wrong after the
next.

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
    guard,
    page_header,
    show_query,
)
from serving.web.streamlit.session import get_client  # noqa: E402

OUTCOME_ICONS = {"pass": "✅", "warn": "⚠️", "fail": "❌"}

st.set_page_config(page_title="Data Quality", page_icon=":material/rule:", layout="wide")
client = get_client()

page_header(
    "Data Quality",
    "Trust the other five pages, or find out which feed to chase before you act on them.",
)

# ------------------------------------------------------------------ freshness
st.subheader("Source freshness")
st.caption(
    "A feed that stops arriving is the dangerous failure: nothing errors, the rows are simply "
    "absent, and every average over the window quietly divides by a smaller denominator."
)

fresh = client.freshness()
if guard(fresh, "source freshness"):
    # /health/freshness answers with a bare object, not the {data, meta}
    # envelope the metric endpoints use, so the client wraps the whole response
    # as a single row. Reading it with frame() renders one row of envelope -
    # as_of, sources, stale_sources, meta - and a "Feeds tracked: 1" tile on the
    # one page whose subject is whether the numbers can be trusted.
    sources = pd.DataFrame((fresh.data[0] if fresh.data else {}).get("sources") or [])
    stale = sources[sources["is_stale"]] if "is_stale" in sources.columns else pd.DataFrame()

    left, right = st.columns(2)
    with left:
        st.metric("Feeds tracked", f"{len(sources):,}")
    with right:
        st.metric("Stale feeds", f"{len(stale):,}")

    if not stale.empty:
        st.error(
            "**"
            + ", ".join(stale["source_name"])
            + "** stopped arriving before the rest of the estate did. Every figure computed "
            "over the affected window is averaging across days this feed did not contribute to.",
            icon=":material/error:",
        )

    st.dataframe(
        sources.rename(
            columns={
                "source_name": "feed",
                "last_seen_date": "last seen",
                "days_behind": "days behind",
                "missing_partitions": "missing partitions",
                "is_stale": "stale",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )
    show_query(fresh, "freshness")

st.divider()

# ------------------------------------------------------- the scan and the dirt
quality = client.quality()
if not quality.ok:
    st.warning(
        f"The quality endpoint is unavailable — {quality.error}: {quality.detail}",
        icon=":material/error:",
    )
    api_footer(None)
    st.stop()

payload = quality.data[0] if quality.data else {}
checks = payload.get("checks") or []
defects = payload.get("defects") or []
missing = payload.get("unavailable") or []

st.subheader("The last Soda scan")
scanned_at = payload.get("scanned_at")
st.caption(
    f"Soda runs outside this application entirely and writes a result file — scanned {scanned_at}."
    if scanned_at
    else "Soda runs outside this application entirely and writes a result file."
)

for note in missing:
    st.info(note, icon=":material/info:")

if checks:
    if payload.get("has_failures"):
        st.error("A data-quality check failed on this build.", icon=":material/error:")
    elif payload.get("has_warnings"):
        st.warning(
            "Warnings only — the build is not failed for them. A scan that goes red for a "
            "known condition teaches everyone to stop reading it.",
            icon=":material/warning:",
        )
    else:
        st.success("Every check passed.", icon=":material/check_circle:")

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "": OUTCOME_ICONS.get(c["outcome"], "•"),
                    "check": c["name"],
                    "outcome": c["outcome"],
                    "table": c["table"],
                    "rows matched": c["failed_rows"],
                }
                for c in checks
            ]
        ),
        use_container_width=True,
        hide_index=True,
        column_config={
            "rows matched": st.column_config.NumberColumn(
                help=(
                    "How many rows the check's query returned. Blank where the check is a "
                    "threshold rather than a row query — a row count is not a failure count."
                )
            )
        },
    )

# The warning is matched to its cause here rather than described in prose, so
# that an outage which is later backfilled stops being explained away.
warned = [c for c in checks if c["outcome"] == "warn"]
outage = next((d for d in defects if d["key"] == "clickstream_outage"), None)
if warned and outage is not None:
    st.info(
        f"**The outstanding warning is a defect this build injected on purpose.** "
        f"{outage['title']} — {outage['symptom']} It is not backfilled, because a gap that is "
        f"quietly filled in is indistinguishable afterwards from one that never happened.",
        icon=":material/info:",
    )

st.divider()

# ------------------------------------------------------------------ the ledger
st.subheader("What was wrong with this data")
st.caption(
    "The raw feeds were dirtied deliberately, so that the repairs in staging are demonstrable "
    "rather than claimed. Every row here is a defect the pipeline is built to survive."
)

if defects:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "defect": d["title"],
                    "feeds": ", ".join(d["feeds"]),
                    "rows": d["rows"],
                    "symptom": d["symptom"],
                    "how it is repaired": d["fix"],
                }
                for d in defects
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

api_footer(None)
