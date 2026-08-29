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


def _interface() -> tuple[str, ...]:
    """The client's public method names, used as the cache key.

    `cache_resource` outlives a code update inside a warm container, so a deploy
    that adds a client method can leave the *old* instance cached against the
    *new* pages. That is not hypothetical: shipping the elasticity and action
    queue pages produced `AttributeError: 'MetricsClient' object has no
    attribute 'elasticity'` on the live app, with both new pages visible in the
    sidebar and a client from before they existed answering their calls.

    Keying the cache on the interface means adding or renaming a method changes
    the key and builds a fresh client, so this cannot recur without somebody
    also removing this function. Cheaper than remembering to bump a version
    constant, and it fails closed rather than open.
    """
    return tuple(sorted(name for name in dir(MetricsClient) if not name.startswith("_")))


@st.cache_resource(show_spinner=False)
def _build_client(interface: tuple[str, ...]) -> MetricsClient:  # noqa: ARG001 - cache key
    return MetricsClient()


def get_client() -> MetricsClient:
    return _build_client(_interface())
