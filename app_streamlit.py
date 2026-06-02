"""Compatibility entrypoint for the current Streamlit app.

The legacy implementation was retired. Keep this file as a thin wrapper so
existing commands like `streamlit run app_streamlit.py` still open the new app.
"""

from app_streamlit_new import *  # noqa: F401,F403
