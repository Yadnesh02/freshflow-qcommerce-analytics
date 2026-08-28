"""One API client per Streamlit session (task S3.8).

Streamlit re-executes the whole script on every interaction, so a client built
at module scope would be rebuilt on every click - and with the in-process ASGI
transport that means re-importing the app and re-opening the warehouse handle
each time. `cache_resource` keeps one for the session, which is the same
lifetime the container has.
"""

from __future__ import annotations

import streamlit as st

from serving.web.api_client import MetricsClient


@st.cache_resource(show_spinner=False)
def get_client() -> MetricsClient:
    return MetricsClient()
