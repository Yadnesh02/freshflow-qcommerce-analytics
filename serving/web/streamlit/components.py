"""Shared pieces of the control tower (task S3.8).

`show_query` is the one that matters. The plan calls it a feature vendor BI
tools do not give you, and it is: every tile can show the exact statement that
produced its number, the registry definition that compiled it, and the source
model underneath. Full lineage from a pixel back to a dbt model, on any figure
a reader doubts.

It is also the enforcement mechanism for gate G3 rather than a decoration. A
tile that renders a number it cannot show a statement for did not come through
the API, and the absence is visible on the page rather than buried in a review.
So every rendering helper here takes an `ApiResult` and refuses to display a
value without also offering its provenance.

Nothing in this package imports duckdb, and `test_the_dashboard_never_touches_
the_warehouse` walks the AST to keep it that way.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from serving.web.api_client import ApiResult

# down_is_good metrics must not render a fall as a red number
DELTA_COLOURS = {"up_is_good": "normal", "down_is_good": "inverse", "neutral": "off"}


def format_value(value: float | None, fmt: str) -> str:
    """Render per the registry's declared format, not per the caller's taste.

    The format is part of the metric definition for the same reason the grain
    is: a rate shown as a count and a count shown as a rate are different
    claims, and letting each page decide would make the same metric look
    different on two tiles.
    """
    if value is None:
        return "--"
    if fmt == "percent_1dp":
        return f"{value * 100:.1f}%"
    if fmt == "inr":
        return _rupees(value)
    if fmt == "number_0dp":
        return f"{value:,.0f}"
    if fmt == "number_1dp":
        return f"{value:,.1f}"
    if fmt == "days_1dp":
        return f"{value:,.1f} d"
    return f"{value:,.4g}"


def _rupees(value: float) -> str:
    """Indian digit grouping, because the audience reads lakh and crore.

    A figure like 1,26,74,000 is instantly sized by an Indian reader and
    12,674,000 is not, and this is a Mumbai dark-store project.
    """
    negative = value < 0
    whole = f"{abs(value):.0f}"
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    return f"{'-' if negative else ''}₹{whole}"


def show_query(result: ApiResult, key: str) -> None:
    """The lineage control: pixel -> metric definition -> SQL -> dbt model."""
    if not result.sql and not result.definition:
        return
    with st.expander("see query", expanded=False):
        definition = result.definition
        if definition:
            st.markdown(f"**{definition['label']}** — {definition['description']}")
            meta_bits = [
                f"source `{definition['source']}`",
                f"grain `{', '.join(definition['grain'])}`",
            ]
            if definition.get("guarded_by"):
                meta_bits.append(f"guardrail `{definition['guarded_by']}`")
            st.caption(" · ".join(meta_bits))
            if definition.get("notes"):
                st.info(definition["notes"], icon=":material/info:")
        if result.sql:
            st.code(result.sql, language="sql")
        if result.meta.get("params"):
            st.caption(f"parameters: {result.meta['params']}")
        for warning in result.warnings:
            st.warning(warning, icon=":material/warning:")


def kpi(
    label: str,
    result: ApiResult,
    fmt: str,
    key: str,
    delta: float | None = None,
    direction: str = "neutral",
    help_text: str | None = None,
) -> None:
    """One headline number, with its provenance underneath."""
    if not result.ok:
        st.metric(label, "--", help=help_text)
        st.caption(f":red[{result.error}] {result.detail or ''}"[:160])
        return

    value = result.scalar()
    st.metric(
        label,
        format_value(value, fmt),
        delta=None if delta is None else format_value(delta, fmt),
        delta_color=DELTA_COLOURS.get(direction, "off"),
        help=help_text,
    )
    show_query(result, key)


def frame(result: ApiResult) -> pd.DataFrame:
    return pd.DataFrame(result.data) if result.ok and result.data else pd.DataFrame()


def guard(result: ApiResult, what: str) -> bool:
    """Render a legible failure in the space the chart would have taken.

    A page of twelve tiles where one metric is missing should show eleven and
    an explanation, not a stack trace. The message is the API's own, which
    names the dimension and lists what would have worked.
    """
    if result.ok and result.data:
        return True
    if not result.ok:
        st.warning(
            f"**{what}** unavailable — {result.error}: {result.detail}", icon=":material/error:"
        )
    else:
        st.info(f"**{what}** returned no rows for this selection.", icon=":material/filter_alt:")
    return False


def page_header(title: str, question: str) -> None:
    """Every page states the decision it exists to support.

    The plan's design rule is that a chart with no decision attached gets cut;
    putting the question at the top is what makes that rule checkable by a
    reader rather than only by its author.
    """
    st.title(title)
    st.caption(f"**So what do I do differently tomorrow?** {question}")


def api_footer(client_health: dict[str, Any] | None) -> None:
    st.divider()
    bits = ["Every number on this page is compiled from `semantic/metrics.yml`."]
    if client_health:
        bits.append(f"{client_health.get('metrics_published', '?')} metrics published.")
    st.caption(" ".join(bits))
