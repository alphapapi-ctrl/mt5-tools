"""
view_settings.py
================
Settings page — theme, custom colours, font size.
"""

import streamlit as st
import os, re

CONFIG_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.toml")

GITHUB_REPO = "alphapapi-ctrl/mt5-tools"
GITHUB_BRANCH = "master"


# ─────────────────────────────────────────────────────────────────────────────
# Version / update check
# ─────────────────────────────────────────────────────────────────────────────
def _get_local_sha() -> str:
    """Return the current local git HEAD SHA, or '' if not a git repo."""
    try:
        git_head = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".git", "HEAD")
        if not os.path.isfile(git_head):
            return ""
        ref = open(git_head).read().strip()
        if ref.startswith("ref:"):
            # Resolve the ref to a SHA
            ref_path = ref.split(" ", 1)[1]  # e.g. refs/heads/master
            sha_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), ".git", ref_path
            )
            if os.path.isfile(sha_file):
                return open(sha_file).read().strip()
        else:
            return ref  # detached HEAD — ref is the SHA directly
    except Exception:
        pass
    return ""


@st.cache_data(ttl=3600)  # cache for 1 hour so we don't hammer the API
def _get_remote_info() -> dict:
    """
    Fetch the latest commit SHA and message from GitHub API.
    Returns dict with keys: sha, message, author, date, error
    """
    import urllib.request, json
    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MT5Tools"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        return {
            "sha":     data["sha"],
            "short":   data["sha"][:7],
            "message": data["commit"]["message"].split("\n")[0],
            "author":  data["commit"]["author"]["name"],
            "date":    data["commit"]["author"]["date"][:10],
            "error":   None,
        }
    except Exception as e:
        return {"sha": "", "short": "", "message": "", "author": "", "date": "", "error": str(e)}


@st.cache_data(ttl=3600)
def _get_recent_commits(n=10) -> list:
    """Fetch the last n commits for the changelog."""
    import urllib.request, json
    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits?sha={GITHUB_BRANCH}&per_page={n}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MT5Tools"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        return [
            {
                "sha":     c["sha"][:7],
                "message": c["commit"]["message"],
                "date":    c["commit"]["author"]["date"][:10],
                "author":  c["commit"]["author"]["name"],
            }
            for c in data
        ]
    except Exception:
        return []


def check_for_update_banner():
    """
    Call from app.py on every page render.
    Shows a top-of-page banner if a newer version is available on GitHub.
    Silently does nothing if offline or not a git repo.
    """
    local_sha = _get_local_sha()
    if not local_sha:
        return  # not a git repo — skip silently

    remote = _get_remote_info()
    if remote["error"] or not remote["sha"]:
        return  # offline or API error — skip silently

    if not remote["sha"].startswith(local_sha) and local_sha != remote["sha"]:
        st.info(
            f"🔄 **Update available** — "
            f"latest commit `{remote['short']}` on {remote['date']}: "
            f"*{remote['message']}*  ·  "
            f"Run `git pull` then restart to update.",
            icon="🔄",
        )

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
                    "secondaryBackgroundColor","textColor","font","cardBackgroundColor"]:
            m = re.search(key + r'\s*=\s*"([^"]*)"', text)
            if m:
                found[key] = m.group(1)
        return {**THEMES["Dark"], **found}
    except Exception:
        return dict(THEMES["Dark"])


def _get_lan_ip() -> str:
    """Return the first 192.168.x.x address found on this machine, or '' if none."""
    import socket
    try:
        # Get all addresses for this hostname
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None)
        for addr in addrs:
            ip = addr[4][0]
            if ip.startswith("192.168."):
                return ip
    except Exception:
        pass
    # Fallback: connect to a dummy address to get the outbound LAN interface
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip.startswith("192.168."):
            return ip
    except Exception:
        pass
    return ""


def _read_server_config() -> dict:
    """Read [server] section from config.toml."""
    if not os.path.isfile(CONFIG_FILE):
        return {}
    try:
        text = open(CONFIG_FILE).read()
        found = {}
        m = re.search(r'address\s*=\s*"([^"]*)"', text)
        if m: found["address"] = m.group(1)
        m = re.search(r'port\s*=\s*(\d+)', text)
        if m: found["port"] = int(m.group(1))
        return found
    except Exception:
        return {}


def _write_config(theme: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # Preserve existing [server] section if present
    server_block = ""
    if os.path.isfile(CONFIG_FILE):
        existing = open(CONFIG_FILE).read()
        m = re.search(r'\[server\].*?(?=\[|\Z)', existing, re.DOTALL)
        if m:
            server_block = "\n" + m.group(0).strip() + "\n"
    lines = ["[theme]\n"]
    for k, v in theme.items():
        lines.append(f'{k} = "{v}"\n')
    if server_block:
        lines.append(server_block)
    with open(CONFIG_FILE, "w") as f:
        f.writelines(lines)


def _write_server_config(address: str, port: int):
    """Write or update the [server] section in config.toml."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # Read existing content, strip any existing [server] block
    existing = open(CONFIG_FILE).read() if os.path.isfile(CONFIG_FILE) else ""
    existing = re.sub(r'\[server\].*?(?=\[|\Z)', '', existing, flags=re.DOTALL).strip()
    server_block = f'\n\n[server]\naddress = "{address}"\nport = {port}\n'
    with open(CONFIG_FILE, "w") as f:
        f.write(existing + server_block)


def _is_custom(cfg: dict) -> bool:
    """Return True if the saved config differs from both standard themes."""
    for t in THEMES.values():
        if all(cfg.get(k) == v for k, v in t.items()):
            return False
    return True


def inject_theme_css():
    """Call from app.py on every page to apply font size and card theme globally."""
    _size_map = {"Normal": 0, "Large (+2px)": 2, "Extra Large (+4px)": 4}
    _delta    = _size_map.get(st.session_state.get("st_font_size", "Normal"), 0)

    # Read card background from config
    cfg      = _read_config()
    base     = cfg.get("base", "dark")
    card_bg  = cfg.get("cardBackgroundColor", "")
    text_col = cfg.get("textColor", "#fafafa")

    # Default card colours per base theme
    if not card_bg:
        card_bg = "#f0f2f6" if base == "light" else "#131720"
    card_border = "rgba(0,0,0,0.10)" if base == "light" else "#1E2535"
    label_col   = "#555e70"          if base == "light" else "#6C7A9A"
    kk_col      = "#4a5568"          if base == "light" else "#7A8898"
    row_border  = "rgba(0,0,0,0.07)" if base == "light" else "#141820"
    sh_border   = "rgba(0,0,0,0.10)" if base == "light" else "#1E2535"
    th_bg       = card_bg
    td_col      = text_col

    css = f"""<style>
    .stat-card{{background:{card_bg} !important;border:1px solid {card_border} !important;
               border-radius:8px;padding:14px 16px;text-align:center;height:100%}}
    .stat-label{{font-size:11px;color:{label_col} !important;text-transform:uppercase;
                letter-spacing:.08em;margin-bottom:4px}}
    .stat-value{{font-size:22px;font-weight:700;color:{text_col} !important}}
    .stat-value.pos{{color:#34C27A !important}}.stat-value.neg{{color:#E05555 !important}}
    .stat-value.neutral{{color:#7BA4DC !important}}
    .stat-sub{{font-size:11px;color:{label_col} !important;margin-top:2px}}
    .sh{{font-size:11px;font-weight:600;color:{label_col} !important;text-transform:uppercase;
        letter-spacing:.1em;margin:18px 0 6px;
        border-bottom:1px solid {sh_border} !important;padding-bottom:4px}}
    .kv-row{{display:flex;justify-content:space-between;padding:5px 0;
            border-bottom:1px solid {row_border}}}
    .kk{{font-size:13px;color:{kk_col} !important}}
    .kv-v{{font-size:13px;font-weight:600;color:{text_col} !important}}
    .kv-v.pos{{color:#34C27A !important}}.kv-v.neg{{color:#E05555 !important}}
    .chip{{display:inline-block;padding:3px 10px;border-radius:12px;font-size:11px;
          font-weight:600;margin:2px;border:1px solid {card_border}}}
    .pb-title{{font-size:22px;font-weight:700;color:{text_col};letter-spacing:.04em}}
    .pb-sub{{font-size:13px;color:{label_col};margin-bottom:14px}}
    .mt th{{background:{th_bg} !important;color:{label_col} !important;
           padding:5px 8px;text-align:center;border-bottom:1px solid {card_border}}}
    .mt td{{padding:4px 8px;text-align:center;color:{td_col};
           border-bottom:1px solid {row_border}}}
    </style>"""
    st.markdown(css, unsafe_allow_html=True)

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
            card_bg = st.color_picker("Card / stat block background",
                                       value=cfg.get("cardBackgroundColor","#131720"),
                                       key="st_card_bg", help="Background for stat cards and tables")
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
                    "cardBackgroundColor":       card_bg,
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

    # ── Network ───────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Network Access")
    st.caption("Controls which network interface Streamlit binds to. "
               "Restart the app after changing.")

    server_cfg = _read_server_config()
    lan_ip     = _get_lan_ip()

    nc1, nc2 = st.columns(2)
    access_mode = nc1.radio(
        "Access",
        ["Localhost only", "Local network (LAN)"],
        horizontal=True,
        key="st_net_mode",
        index=1 if server_cfg.get("address","") not in ("","127.0.0.1","localhost") else 0,
        help="Localhost = this PC only.  LAN = all devices on your 192.168.x.x network.",
    )
    port = nc2.number_input(
        "Port", min_value=1024, max_value=65535,
        value=server_cfg.get("port", 8501),
        step=1, key="st_net_port",
    )

    if access_mode == "Local network (LAN)":
        if lan_ip:
            st.info(f"Detected LAN address: **{lan_ip}**  →  "
                    f"other devices can reach the app at `http://{lan_ip}:{int(port)}`")
        else:
            st.warning("No 192.168.x.x address found — are you connected to your router?")

        # Hostname instructions
        import socket as _socket, platform as _platform
        hostname = _socket.gethostname().lower()
        os_name  = _platform.system()
        lan_ip_str = lan_ip or "192.168.x.x"

        st.markdown("**Use a custom hostname instead of an IP address**")
        st.caption(
            "Add an entry to the hosts file on each device that needs to access the app. "
            "No extra software required — works on Windows, macOS, iOS, and Android."
        )

        custom_name = st.text_input(
            "Hostname to use", value="mt5tools",
            key="st_custom_hostname",
            help="e.g. mt5tools  →  http://mt5tools:8501"
        )
        st.markdown(
            f"Once set, devices on your LAN can access the app at: "
            f"`http://{custom_name}:{int(port)}`"
        )

        if os_name == "Windows":
            st.markdown(f"""
<details>
<summary><b>Windows — edit the hosts file on each client device</b></summary>

1. Open **Notepad as Administrator** (right-click Notepad → Run as administrator)
2. Open the file: `C:\\Windows\\System32\\drivers\\etc\\hosts`
3. Add this line at the bottom:

```
{lan_ip_str}    {custom_name}
```

4. Save the file
5. Open a browser and go to `http://{custom_name}:{int(port)}`

**Note:** You need to do this on every Windows device that will access the app.
No restart required — the change takes effect immediately.
</details>
""", unsafe_allow_html=True)

        elif os_name == "Darwin":
            st.markdown(f"""
<details>
<summary><b>macOS — edit /etc/hosts on each client device</b></summary>

1. Open **Terminal**
2. Run:

```
sudo nano /etc/hosts
```

3. Add this line at the bottom:

```
{lan_ip_str}    {custom_name}
```

4. Press `Ctrl+X`, then `Y`, then `Enter` to save
5. Flush the DNS cache:

```
sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder
```

6. Open a browser and go to `http://{custom_name}:{int(port)}`
</details>
""", unsafe_allow_html=True)

        st.markdown(f"""
<details>
<summary><b>iOS / Android — not supported via hosts file</b></summary>

Mobile devices do not allow editing the hosts file without rooting/jailbreaking.
Use the IP address directly instead:

`http://{lan_ip_str}:{int(port)}`

Alternatively, set a **static DHCP reservation** on your router so the IP never
changes — then bookmark the IP address URL.
</details>
""", unsafe_allow_html=True)

    if st.button("Apply Network Settings", key="st_net_apply"):
        if access_mode == "Localhost only":
            _write_server_config("127.0.0.1", int(port))
        else:
            addr = lan_ip if lan_ip else "192.168.1.1"
            _write_server_config(addr, int(port))
        st.success("Saved to config.toml — restart the app for changes to take effect.")

    # ── Version & Changelog ───────────────────────────────────────────────────
    st.divider()
    st.subheader("Version & Changelog")

    local_sha  = _get_local_sha()
    remote     = _get_remote_info()

    vc1, vc2 = st.columns(2)
    vc1.markdown(
        f"**Local version:** `{local_sha[:7] if local_sha else 'unknown'}`"
        if local_sha else "**Local version:** not a git repo"
    )
    if remote["error"]:
        vc2.markdown(f"**Remote:** unable to reach GitHub *(offline?)*")
    else:
        vc2.markdown(f"**Latest on GitHub:** `{remote['short']}` — {remote['date']}")

    if local_sha and not remote["error"]:
        if remote["sha"].startswith(local_sha) or local_sha == remote["sha"]:
            st.success("✅ You are on the latest version.")
        else:
            st.warning(
                f"🔄 **Update available.**  Latest: `{remote['short']}` — *{remote['message']}*\n\n"
                f"Run the following to update:\n```\ngit pull\n```\nThen restart the app."
            )

    if st.button("🔁 Check again", key="st_ver_refresh"):
        st.cache_data.clear()
        st.rerun()

    st.markdown("##### Recent Changes")
    commits = _get_recent_commits(10)
    if commits:
        for c in commits:
            is_local = local_sha and c["sha"] == local_sha[:7]
            lines    = c["message"].strip().split("\n")
            title    = lines[0]
            body     = [l for l in lines[1:] if l.strip()]
            marker   = " ← **you are here**" if is_local else ""
            with st.expander(f"`{c['sha']}` · {c['date']} · {title}{marker}"):
                if body:
                    for l in body:
                        if l.strip().startswith("-"):
                            st.markdown(l.strip())
                        elif l.strip():
                            st.markdown(l.strip())
                else:
                    st.caption("No extended description.")
    else:
        st.caption("Changelog unavailable — check your internet connection.")

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