"""
pages/settings.py
=================
Settings page for MT5 Tools dashboard.
"""

import streamlit as st


def render():
    st.title("⚙️ Settings")

    st.markdown("""
    <div class="info-card">
        MT5 Tools — settings and information.
    </div>
    """, unsafe_allow_html=True)

    st.subheader("About")
    st.markdown("""
    **MT5 Tools** is a standalone trade analysis and comparison dashboard.

    **Supported file formats:**
    - MT5 Account History HTML export (`.htm` / `.html`)
    - MT5 Strategy Tester Backtest Report (`.htm` / `.html`)
    - Quant Analyzer CSV export (`listOfTrades_*.csv`)

    **Pages:**
    - **Trade Analysis** — statistics, equity curves, day/hour breakdown for a single report
    - **Trade Compare** — match and compare two reports to measure slippage and variance
    """)

    st.subheader("Requirements")
    st.code("pip install streamlit streamlit-option-menu pandas plotly", language="bash")

    st.subheader("Launch")
    st.code("streamlit run app.py", language="bash")
