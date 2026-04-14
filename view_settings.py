"""
view_settings.py
================
Settings page — theme, custom colours, font size.
"""

import streamlit as st
import os, re

CONFIG_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.toml")

THEMES = {
    "Dark": {
        "base": "dark",
        "primaryColor":             "#7c6af7",
        "backgroundColor":          "#0e1117",
        "secondaryBackgroundColor": "#1a1f2e",
        "textColor":                "#fafafa",
        "font":                     "sans serif",
    },
    "Light": {
        "base": "light",
        "primaryColor":             "#2E75B6",
        "backgroundColor":          "#ffffff",
        "secondaryBackgroundColor": "#f0f2f6",
        "textColor":                "#1a1a1a",
        "font":                     "sans serif",
    },
}


def _read_config() -> dict:
    """Read .streamlit/config.toml, return dict of theme keys."""
    if not os.path.isfile(CONFIG_FILE):
        return dict(THEMES["Dark"])
    try:
        text  = open(CONFIG_FILE).read()
        found = {}
        for key in ["base","primaryColor","backgroundColor",
                    "secondaryBackgroundColor","textColor","font"]:
            m = re.search(key + r'\s*=\s*"([^"]*)"', text)
            if m:
                found[key] = m.group(1)
        return {**THEMES["Dark"], **found}
    except Exception:
        return dict(THEMES["Dark"])


def _write_config(theme: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    lines = ["[theme]\n"]
    for k, v in theme.items():
        lines.append(f'{k} = "{v}"\n')
    with open(CONFIG_FILE, "w") as f:
        f.writelines(lines)


def _is_custom(cfg: dict) -> bool:
    """Return True if the saved config differs from both standard themes."""
    for t in THEMES.values():
        if all(cfg.get(k) == v for k, v in t.items()):
            return False
    return True


def inject_theme_css():
    """Call from app.py on every page to apply font size globally."""
    _size_map = {"Normal": 0, "Large (+2px)": 2, "Extra Large (+4px)": 4}
    _delta    = _size_map.get(st.session_state.get("st_font_size", "Normal"), 0)
    if _delta > 0:
        st.markdown(f"""<style>
        html, body, [class*="css"] {{ font-size: calc(1rem + {_delta}px) !important; }}
        .stMarkdown p, .stMarkdown li, .stCaption, label,
        .stMetric label, .stMetric div[data-testid="metric-container"] {{
            font-size: calc(1rem + {_delta}px) !important;
        }}
        h1 {{ font-size: calc(2rem   + {_delta}px) !important; }}
        h2 {{ font-size: calc(1.5rem + {_delta}px) !important; }}
        h3 {{ font-size: calc(1.25rem + {_delta}px) !important; }}
        </style>""", unsafe_allow_html=True)


def render():
    st.title("Settings")

    # ── Theme selector ────────────────────────────────────────────────────────
    st.subheader("Theme")

    cfg = _read_config()

    # Determine current theme state
    if _is_custom(cfg):
        current_mode = "Custom"
    elif cfg.get("base") == "light":
        current_mode = "Light"
    else:
        current_mode = "Dark"

    # Always show all three options — Custom opens the colour pickers
    theme_options = ["Dark", "Light", "Custom"]
    selected = st.radio("Theme", theme_options, horizontal=True, key="st_theme_pick",
                        index=theme_options.index(current_mode) if current_mode in theme_options else 0)

    # ── Apply button for Dark / Light ────────────────────────────────────────
    if selected in ("Dark", "Light"):
        if st.button(f"Apply {selected} Theme", type="primary", key="st_apply_base"):
            _write_config(THEMES[selected])
            st.success(f"{selected} theme applied — reloading...")
            st.rerun()

    # ── Custom colour section — only show when Custom is selected ─────────────
    if selected == "Custom":
        st.caption("Customise colours below then click **Apply Custom Theme**.")
        st.markdown("---")

        c1, c2, c3 = st.columns(3)
        with c1:
            primary = st.color_picker("Accent colour",
                                       value=cfg.get("primaryColor","#7c6af7"),
                                       key="st_primary", help="Buttons, links, highlights")
        with c2:
            bg         = st.color_picker("Page background",
                                          value=cfg.get("backgroundColor","#0e1117"),
                                          key="st_bg")
            sidebar_bg = st.color_picker("Sidebar / widget background",
                                          value=cfg.get("secondaryBackgroundColor","#1a1f2e"),
                                          key="st_sidebar_bg")
        with c3:
            text_color = st.color_picker("Text colour",
                                          value=cfg.get("textColor","#fafafa"),
                                          key="st_text")
            font       = st.selectbox("Font", ["sans serif", "serif", "monospace"],
                                       index=["sans serif","serif","monospace"].index(
                                           cfg.get("font","sans serif")),
                                       key="st_font")
            base_for_custom = st.radio("Base", ["dark","light"], horizontal=True,
                                        key="st_custom_base",
                                        index=0 if cfg.get("base","dark")=="dark" else 1,
                                        help="Underlying dark or light base for your custom colours")

        # Preview swatches
        st.markdown("**Preview**")
        sw1, sw2, sw3, sw4 = st.columns(4)
        for col, color, label in [
            (sw1, bg,         "Page BG"),
            (sw2, sidebar_bg, "Sidebar BG"),
            (sw3, primary,    "Accent"),
            (sw4, text_color, "Text"),
        ]:
            col.markdown(
                f'<div style="background:{color};padding:14px 8px;border-radius:6px;'
                f'text-align:center;font-size:12px;border:1px solid rgba(128,128,128,0.25)">'
                f'{label}<br><code style="font-size:10px">{color}</code></div>',
                unsafe_allow_html=True)

        st.markdown("")
        btn1, btn2 = st.columns([2, 1])
        if btn1.button("Apply Custom Theme", type="primary", key="st_apply"):
            try:
                _write_config({
                    "base":                     base_for_custom,
                    "primaryColor":             primary,
                    "backgroundColor":          bg,
                    "secondaryBackgroundColor": sidebar_bg,
                    "textColor":                text_color,
                    "font":                     font,
                })
                st.success("Custom theme saved — reloading...")
                st.rerun()
            except Exception as e:
                st.error(f"Could not write config: {e}")

        if btn2.button("Discard / Reset to Dark", key="st_reset"):
            _write_config(THEMES["Dark"])
            st.success("Reset to Dark — reloading...")
            st.rerun()

    elif selected in ("Dark", "Light"):
        st.caption("Select **Custom** to adjust individual colours.")

    with st.expander("Config file"):
        st.code(CONFIG_FILE)
        if os.path.isfile(CONFIG_FILE):
            st.code(open(CONFIG_FILE).read(), language="toml")
        else:
            st.caption("No config.toml yet — created on first Apply.")

    # ── Text size ─────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Text Size")
    st.caption("Applies immediately across all pages — no reload needed.")
    st.radio("Text size", ["Normal", "Large (+2px)", "Extra Large (+4px)"],
             horizontal=True, key="st_font_size")
    inject_theme_css()

    # ── About ─────────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("About")
    st.markdown("""
**MT5 Tools** — standalone trade analysis and portfolio construction dashboard for MetaTrader 5.

**Supported formats:** MT5 Account History HTM · MT5 Backtest HTM · Quant Analyzer CSV · IC Markets XLSX

**Pages:** Trade Analysis · Trade Compare · Portfolio Builder · Portfolio Master · EA Comparator · Batch Backtest *(Windows only)*
""")
    st.subheader("Requirements")
    st.code("pip install -r requirements.txt", language="bash")
    st.subheader("Launch")
    st.code("streamlit run app.py", language="bash")