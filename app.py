"""
MT5 Tools Dashboard
===================
Streamlit app for MT5 trade analysis and comparison.

Launch: streamlit run app.py
"""

import streamlit as st
from streamlit_option_menu import option_menu
import importlib, sys, os

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title  = "MT5 Tools",
    page_icon   = "📈",
    layout      = "wide",
    initial_sidebar_state = "expanded"
)

# ── Theme ─────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  /* Base */
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Syne:wght@400;600;800&display=swap');

  html, body, [class*="css"] {
    font-family: 'Syne', sans-serif;
    background-color: #0a0a0f;
    color: #e0e0e8;
  }
  code, .mono { font-family: 'JetBrains Mono', monospace; }

  /* Sidebar */
  [data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0d0d1a 0%, #0a0a12 100%);
    border-right: 1px solid rgba(255,255,255,0.06);
  }
  [data-testid="stSidebar"] .stMarkdown h3 {
    color: #7c6af7;
    font-size: 11px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    font-weight: 800;
  }

  /* Cards */
  .stat-card {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 8px;
    padding: 14px 18px;
    margin-bottom: 8px;
  }
  .stat-label {
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #666;
    margin-bottom: 4px;
  }
  .stat-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 22px;
    font-weight: 700;
    color: #e0e0e8;
  }
  .stat-pos { color: #2dc653; }
  .stat-neg { color: #e63946; }

  /* Info card */
  .info-card {
    background: rgba(124,106,247,0.08);
    border: 1px solid rgba(124,106,247,0.2);
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 13px;
    color: #aaa;
    margin: 8px 0;
  }

  /* Comparison cells */
  .diff-pos { color: #2dc653; font-weight: 600; }
  .diff-neg { color: #e63946; font-weight: 600; }
  .diff-neu { color: #888; }

  /* Divider */
  hr { border-color: rgba(255,255,255,0.06); }

  /* Metric overrides */
  [data-testid="metric-container"] {
    background: rgba(255,255,255,0.02);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 8px;
    padding: 12px;
  }
  [data-testid="stMetricValue"] {
    font-family: 'JetBrains Mono', monospace;
    font-size: 18px !important;
  }

  /* Buttons */
  .stButton > button {
    background: rgba(124,106,247,0.15);
    border: 1px solid rgba(124,106,247,0.3);
    color: #c5beff;
    font-family: 'Syne', sans-serif;
    font-weight: 600;
    letter-spacing: 0.05em;
    border-radius: 6px;
    transition: all 0.15s;
  }
  .stButton > button:hover {
    background: rgba(124,106,247,0.3);
    border-color: rgba(124,106,247,0.6);
  }

  /* File uploader */
  [data-testid="stFileUploader"] {
    background: rgba(255,255,255,0.02);
    border: 1px dashed rgba(255,255,255,0.12);
    border-radius: 8px;
  }

  /* Tab overrides */
  .stTabs [data-baseweb="tab"] {
    font-family: 'Syne', sans-serif;
    font-weight: 600;
    font-size: 13px;
  }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar nav ───────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 📈 MT5 Tools")
    st.markdown("---")
    page = option_menu(
        menu_title  = None,
        options     = ["Trade Analysis", "Trade Compare", "EA Comparator", "Batch Backtest", "Settings"],
        icons       = ["bar-chart-line", "arrow-left-right", "sliders", "cpu", "gear"],
        default_index = 0,
        styles = {
            "container"       : {"background-color": "transparent", "padding": "0"},
            "icon"            : {"color": "#7c6af7", "font-size": "14px"},
            "nav-link"        : {
                "font-family" : "Syne, sans-serif",
                "font-size"   : "13px",
                "font-weight" : "600",
                "color"       : "#aaa",
                "border-radius": "6px",
                "margin"      : "2px 0",
            },
            "nav-link-selected": {
                "background-color": "rgba(124,106,247,0.15)",
                "color"           : "#c5beff",
                "border"          : "1px solid rgba(124,106,247,0.25)",
            },
        }
    )

# ── Route pages ───────────────────────────────────────────────────────────────
if page == "Trade Analysis":
    import view_trade_analysis as p
    importlib.reload(p)
    p.render()

elif page == "Trade Compare":
    import view_trade_compare as p
    importlib.reload(p)
    p.render()

elif page == "EA Comparator":
    import view_set_comparator as p
    importlib.reload(p)
    p.render()

elif page == "Batch Backtest":
    import view_batch_backtest as p
    importlib.reload(p)
    p.render()

elif page == "Settings":
    import view_settings as p
    importlib.reload(p)
    p.render()