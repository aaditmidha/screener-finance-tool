"""Streamlit Community Cloud entry point.

Streamlit Cloud runs this root-level file. It bridges ``st.secrets`` (the
cloud's secret store) into environment variables the rest of the package
expects (e.g. ``GROQ_API_KEY``), then hands off to the real app in
:mod:`screener.ui.app`.

Run locally with:
    streamlit run streamlit_app.py
"""

import logging
import os

logger = logging.getLogger(__name__)

# Secrets that, when present in .streamlit/secrets.toml or the Streamlit Cloud
# secrets UI, are exposed as environment variables for the package to read.
_BRIDGED_SECRETS = ("GROQ_API_KEY",)


def bridge_secrets() -> None:
    """Copy known keys from st.secrets into os.environ (env always wins).

    Safe when no secrets file exists at all — Streamlit raises on first
    access in that case, which is caught and ignored.
    """
    import streamlit as st

    for key in _BRIDGED_SECRETS:
        if os.environ.get(key):
            continue  # explicit environment always takes precedence
        try:
            value = st.secrets.get(key)
        except Exception:  # no secrets.toml configured anywhere
            return
        if value:
            os.environ[key] = str(value)
            logger.debug("Bridged secret %s from st.secrets into environment", key)


bridge_secrets()

from screener.ui.app import main  # noqa: E402 — import after secrets bridge

main()
