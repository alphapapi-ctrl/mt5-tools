"""
view_ftp_tracker.py
===================
FTP-based multi-account MT5 tracker.
Pulls HTML reports from FTP, parses via mt5_parser, shows calendar + analysis.

Config: ftp_accounts.json  (labels, balances per account)
Cache:  cache/ftp_*.pkl    (parsed DataFrames, refreshed on demand)
FTP:    ftp_config.json    (host/user/pass)
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import date, datetime, timedelta
import calendar
import pickle
import json
import ftplib
from pathlib import Path

CONFIG_FILE   = Path("ftp_config.json")
ACCOUNTS_FILE = Path("ftp_accounts.json")
CACHE_DIR     = Path("cache")
CACHE_MAX_AGE = 5   # minutes before auto-refresh on load


# ── Config helpers ─────────────────────────────────────────────────────────────

def load_ftp_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def load_account_configs() -> list:
    if ACCOUNTS_FILE.exists():
        return json.loads(ACCOUNTS_FILE.read_text())
    return []


def save_account_configs(accounts: list):
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2))


# ── FTP + parse ────────────────────────────────────────────────────────────────

def ftp_list_accounts(cfg: dict) -> list:
    ftp = ftplib.FTP()
    ftp.connect(cfg["host"], cfg.get("port", 21), timeout=10)
    ftp.login(cfg["user"], cfg["password"])
    ftp.set_pasv(True)
    items = []
    ftp.retrlines("LIST", items.append)
    folders = [i.split()[-1] for i in items if i.startswith("d")]
    ftp.quit()
    return folders


def ftp_download_report(cfg: dict, account_folder: str) -> bytes | None:
    ftp = ftplib.FTP()
    ftp.connect(cfg["host"], cfg.get("port", 21), timeout=15)
    ftp.login(cfg["user"], cfg["password"])
    ftp.set_pasv(True)
    try:
        ftp.cwd(f"/{account_folder}")
    except ftplib.error_perm:
        ftp.quit()
        return None
    files = []
    ftp.retrlines("NLST", files.append)
    htm = next((f for f in files if f.lower().endswith(('.htm','.html'))), None)
    if not htm:
        ftp.quit()
        return None
    buf = []
    ftp.retrbinary(f"RETR {htm}", buf.append)
    ftp.quit()
    return b"".join(buf)


def _extract_report_date(raw: bytes) -> str | None:
    """Extract the report generation date from MT5 HTML report header."""
    import re
    for enc in ['utf-16', 'utf-8', 'latin-1']:
        try:
            text = raw.decode(enc)
            break
        except Exception:
            text = None
    if not text:
        return None
    # Look for Date: 2026.04.17 20:06 pattern in table cells
    # Strip tags first so whitespace/newlines between 'Date:' and value don't block match
    text_clean = re.sub(r'<[^>]+>', ' ', text)
    match = re.search(r'Date:\s*(\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2})', text_clean)
    if match:
        try:
            from datetime import datetime as _dt
            return _dt.strptime(match.group(1).strip(), '%Y.%m.%d %H:%M').isoformat()
        except Exception:
            return None
    return None


def refresh_account(cfg: dict, account_folder: str, label: str = "") -> dict:
    """Download, parse, cache one account. Returns {df, stats, error}."""
    from mt5_parser import detect_and_parse, calc_stats
    raw = ftp_download_report(cfg, account_folder)
    if raw is None:
        return {"error": f"No report found for {account_folder}"}
    df, fmt = detect_and_parse(raw, f"{account_folder}.htm")
    if df is None or df.empty:
        return {"error": f"Could not parse report for {account_folder}"}
    stats = calc_stats(df)
    # Also parse open positions if present
    from mt5_parser import parse_open_positions
    df_open = parse_open_positions(raw)
    data  = {
        "account_folder": account_folder,
        "label"         : label or account_folder,
        "df"            : df,
        "stats"         : stats,
        "fmt"           : fmt,
        "df_open"       : df_open,
        "fetched_at"    : datetime.now().isoformat(),
        "report_date"   : _extract_report_date(raw),
        "error"         : None,
    }
    CACHE_DIR.mkdir(exist_ok=True)
    (CACHE_DIR / f"ftp_{account_folder}.pkl").write_bytes(pickle.dumps(data))
    return data


def load_cache(account_folder: str) -> dict | None:
    p = CACHE_DIR / f"ftp_{account_folder}.pkl"
    if not p.exists():
        return None
    try:
        return pickle.loads(p.read_bytes())
    except Exception:
        return None


def cache_age_minutes(account_folder: str) -> float:
    p = CACHE_DIR / f"ftp_{account_folder}.pkl"
    if not p.exists():
        return float("inf")
    return (datetime.now().timestamp() - p.stat().st_mtime) / 60


def get_all_cached() -> list[dict]:
    if not CACHE_DIR.exists():
        return []
    out = []
    for p in sorted(CACHE_DIR.glob("ftp_*.pkl")):
        try:
            out.append(pickle.loads(p.read_bytes()))
        except Exception:
            pass
    return out


# ── Render ─────────────────────────────────────────────────────────────────────

def render():
    st.title("📡 Live MT5 EA's")

    ftp_cfg = load_ftp_config()
    if not ftp_cfg:
        st.warning("⚙️ **FTP not configured** — follow the setup guide below to get started.")

        st.markdown("---")
        st.markdown("## 📡 Setup Guide")
        st.markdown(
            "The Live MT5 EAs page pulls account history HTML reports from an FTP server "
            "that each MT5 terminal publishes to automatically. No MetaTrader5 Python library "
            "is required — the connection uses Python's built-in `ftplib`."
        )

        st.markdown("""
**Architecture overview:**
- **Remote Windows machine** — one or more MT5 terminals running EAs, each configured to auto-publish account history reports to a FileZilla FTP server every 5 minutes
- **FTP server (FileZilla Server)** — receives reports and stores them in per-account subfolders
- **MT5 Tools** — pulls reports from FTP, parses them, and displays them here

> **Key point:** MT5 and FileZilla are typically on the same machine. MT5 connects to FileZilla via `127.0.0.1` (loopback) so no firewall rule is needed between them. The only firewall rule needed is **port 21 open inbound** so MT5 Tools can connect from outside the LAN.
""")

        with st.expander("**Part 1 — FileZilla Server Setup**", expanded=True):
            st.markdown("""
**Installation**

Download FileZilla Server from [filezilla-project.org](https://filezilla-project.org) and install it on the remote Windows machine that runs the MT5 terminals. The free version is sufficient.

**Create FTP User**

After installation, open the FileZilla Server interface (system tray icon or Start menu):

| Step | Action |
|------|--------|
| 1 | Go to **Server → Configure** (or Edit → Users in older versions) |
| 2 | Click **Add user** and create a user named `mt5ftp` (or any name you choose) |
| 3 | Set a strong password for the user |
| 4 | Under **Directories** (or Mount Points), add a home directory — e.g. `C:\\MT5FTP\\` |
| 5 | Grant the user **Read** and **Write** permissions on that directory |
| 6 | Click **OK** to save |

*The home directory becomes the FTP root. MT5 will create subfolders here — one per account.*

**Disable TLS (Required for MT5 Compatibility)**

MT5's built-in FTP publisher uses plain FTP and does not support TLS. FileZilla Server defaults to requiring TLS, which causes a connection failure.

| Step | Action |
|------|--------|
| 1 | In FileZilla Server, go to **Server → Configure** |
| 2 | Navigate to the **FTP over TLS** settings section |
| 3 | Change from **Require explicit FTP over TLS** to **Allow plain FTP** |
| 4 | Click OK to save and restart FileZilla Server if prompted |

> ⚠️ **Security note:** Plain FTP transmits credentials unencrypted. This is acceptable on a private local network. If exposing the FTP server to the internet, consider using a VPN.

**Passive Mode Port Range**

FileZilla Server uses passive mode for data connections. If accessing from outside the local network, open the passive port range in the Windows firewall. The default range is `49152–65534`. You can restrict this (e.g. `50000–50100`) in FileZilla Server settings to reduce the number of ports to open.

**Verify FileZilla is Running**

Confirm FileZilla Server is running and listening on port 21 by checking the system tray. You can also test from FileZilla Client on the same machine using `127.0.0.1` as the host.
""")

        with st.expander("**Part 2 — MT5 Terminal Configuration**"):
            st.markdown("""
Each MT5 terminal must be configured individually to publish its account history to a dedicated subfolder on the FTP server.

**FTP Publisher Settings**

In each MT5 terminal:

| Step | Action |
|------|--------|
| 1 | Go to **Tools → Options** |
| 2 | Click the **Publisher** tab (some versions label it FTP or Report Publishing) |
| 3 | Check **Enable automatic publishing of reports via FTP** |
| 4 | Set **Server** to `127.0.0.1` (loopback — MT5 and FileZilla are on the same machine) |
| 5 | Set **Port** to `21` |
| 6 | Set **Login** to the FileZilla username (e.g. `mt5ftp`) |
| 7 | Set **Password** to the FileZilla user password |
| 8 | Set **Path** to `/ACCOUNT_NUMBER/` — e.g. `/123456/` — using the account number of that terminal |
| 9 | Check **Passive mode** |
| 10 | Set the publishing interval (recommended: **5 minutes**) |
| 11 | Click **Test** — you should see a success message |
| 12 | Click **OK** to save, then click **Publish manually** to create the first report |

> MT5 creates the subfolder automatically on first publish. The path must be unique per terminal — use the account number to keep them separate.

**Common Issues**

| Issue | Fix |
|-------|-----|
| TLS error on Test | TLS has not been disabled in FileZilla Server — see Part 1 |
| Test succeeds but no file appears | Ensure path is set correctly (e.g. `/123456/` not `/inetpub/shots`). Click Publish manually and check FileZilla Server log |
| Settings not saving | Try running MT5 as Administrator. Check each instance has its own data folder via File → Open Data Folder |
""")

        with st.expander("**Part 3 — CLI Verification**"):
            st.markdown("""
Before using this page, verify the FTP connection using the included CLI tool `ftp_sync_cli.py`.

**Initial connection test:**
```
python ftp_sync_cli.py --host 192.168.x.x --user mt5ftp --password yourpass --list
```

**Save credentials to ftp_config.json:**
```
python ftp_sync_cli.py --host 192.168.x.x --user mt5ftp --password yourpass --save
```

**Pull and parse a single account:**
```
python ftp_sync_cli.py --config --account 123456
```

**Cache all accounts:**
```
python ftp_sync_cli.py --config --account 123456 --save-cache
python ftp_sync_cli.py --config --account 789012 --save-cache
```

Each account is saved to `cache/ftp_ACCOUNT.pkl`. This page loads these automatically on startup.
""")

        with st.expander("**Part 4 — Configuration Files**"):
            st.markdown("""
| File | Location | Contents |
|------|----------|----------|
| `ftp_config.json` | MT5Tools/ | FTP host, user, password, port — **gitignored** |
| `ftp_accounts.json` | MT5Tools/ | Account labels, balances, types, prop settings — **gitignored** |
| `cache/ftp_*.pkl` | MT5Tools/cache/ | Parsed DataFrames per account — **gitignored**, auto-refreshed |

Ensure your `.gitignore` contains:
```
ftp_config.json
ftp_accounts.json
mt5_accounts.json
cache/
```
""")

        with st.expander("**Part 5 — Troubleshooting**"):
            st.markdown("""
| Issue | Solution |
|-------|----------|
| Connection refused on port 21 | Check FileZilla Server is running (system tray). Check port 21 is open in Windows firewall. Try FileZilla Client first to isolate. |
| 530 Login incorrect | Verify username and password in `ftp_config.json` match exactly. Passwords are case-sensitive. |
| No .htm/.html file found | MT5 terminal has not published yet. Go to Tools → Options → Publisher and click manual publish. Check FileZilla Server log. |
| Could not parse report | Try uploading the HTML file directly to Trade Analysis to test parsing. |
| Old data after Refresh All | MT5 publishes on a timer. Wait for the next publish cycle or trigger a manual publish in MT5. |
""")

        st.info("Once `ftp_config.json` is created via the CLI, refresh this page and the live dashboard will load.")
        return

    acc_cfgs = load_account_configs()

    # ── Account config expander ──────────────────────────────────────────────
    with st.expander("⚙️ Account Configuration", expanded=not acc_cfgs):
        st.caption("Add accounts by folder name (must match FTP folder). Set label and starting balance.")

        # ── Add account form ──────────────────────────────────────────────────
        st.markdown("**Add Account**")
        add_c1, add_c2, add_c3, add_c4, add_c5 = st.columns([2, 2, 2, 2, 1])
        new_folder  = add_c1.text_input("FTP Folder", placeholder="123456",
                                         key="cfg_new_folder")
        new_label   = add_c2.text_input("Label", placeholder="Gold EA",
                                         key="cfg_new_label")
        new_balance = add_c3.number_input("Starting Balance ($)", value=10000.0,
                                           min_value=0.0, step=1000.0, format="%.0f",
                                           key="cfg_new_balance")
        new_type    = add_c4.selectbox("Type", ["Demo","Personal","Prop"],
                                        key="cfg_new_type")
        add_c5.markdown("<br>", unsafe_allow_html=True)
        if add_c5.button("➕ Add", key="cfg_add"):
            if not new_folder.strip():
                st.error("FTP folder name is required.")
            else:
                # Verify folder exists on FTP
                try:
                    ftp_folders = ftp_list_accounts(ftp_cfg)
                    if new_folder.strip() not in ftp_folders:
                        st.error(f"Folder `{new_folder}` not found on FTP. "
                                 f"Available: {', '.join(ftp_folders)}")
                    else:
                        existing_accs = load_account_configs()
                        if any(a["account"] == new_folder.strip() for a in existing_accs):
                            st.warning(f"Account `{new_folder}` already configured.")
                        else:
                            existing_accs.append({
                                "account": new_folder.strip(),
                                "label"  : new_label.strip() or new_folder.strip(),
                                "balance": float(new_balance),
                                "type"   : new_type,
                            })
                            save_account_configs(existing_accs)
                            acc_cfgs = existing_accs
                            st.success(f"✓ Added `{new_folder}`")
                            st.rerun()
                except Exception as e:
                    st.error(f"FTP error: {e}")

        # ── Existing accounts ─────────────────────────────────────────────────
        if acc_cfgs:
            st.markdown("**Configured Accounts**")
            hdr = st.columns([1, 2, 2, 2, 2, 1])
            hdr[0].markdown("**Folder**")
            hdr[1].markdown("**Label**")
            hdr[2].markdown("**Balance ($)**")
            hdr[3].markdown("**Type**")
            hdr[4].markdown("**Last Report**")
            hdr[5].markdown("**Remove**")
            updated = []
            for idx_ac, ac in enumerate(acc_cfgs):
                if idx_ac > 0:
                    st.divider()
                c1, c2, c3, c4, c5, c6 = st.columns([1, 2, 2, 2, 2, 1])
                c1.markdown(f"`{ac['account']}`")
                label   = c2.text_input("", value=ac.get("label", ac["account"]),
                                        key=f"lbl_{ac['account']}",
                                        label_visibility="collapsed")
                balance = c3.number_input("", value=float(ac.get("balance", 10000)),
                                          min_value=0.0, step=1000.0, format="%.0f",
                                          key=f"bal_{ac['account']}",
                                          label_visibility="collapsed")
                acc_type = c4.selectbox("", ["Demo", "Personal", "Prop"],
                                        index=["Demo","Personal","Prop"].index(
                                            ac.get("type","Demo")),
                                        key=f"type_{ac['account']}",
                                        label_visibility="collapsed")
                # Prop-specific target/loss fields
                if acc_type == "Prop":
                    prop_c1, prop_c2, prop_c3 = st.columns(3)
                    profit_target = prop_c1.number_input(
                        "Profit target %",
                        value=float(ac.get("profit_target", 10.0)),
                        min_value=0.0, max_value=100.0, step=1.0, format="%.1f",
                        key=f"pt_{ac['account']}")
                    max_loss = prop_c2.number_input(
                        "Max loss %",
                        value=float(ac.get("max_loss", 10.0)),
                        min_value=0.0, max_value=100.0, step=1.0, format="%.1f",
                        key=f"ml_{ac['account']}")
                    daily_loss = prop_c3.number_input(
                        "Daily loss %",
                        value=float(ac.get("daily_loss", 5.0)),
                        min_value=0.0, max_value=100.0, step=0.5, format="%.1f",
                        key=f"dl_{ac['account']}")
                else:
                    profit_target = ac.get("profit_target", 10.0)
                    max_loss      = ac.get("max_loss", 10.0)
                    daily_loss    = ac.get("daily_loss", 5.0)
                # Last report date from cache
                cached = load_cache(ac["account"])
                if cached and cached.get("fetched_at"):
                    try:
                        dt = datetime.fromisoformat(cached["fetched_at"])
                        last_report = dt.strftime("%d %b %H:%M")
                    except Exception:
                        last_report = "—"
                else:
                    last_report = "No cache"
                c5.markdown(f'<div style="padding-top:8px;font-size:13px;color:#555">{last_report}</div>',
                           unsafe_allow_html=True)
                if c6.button("🗑", key=f"rm_{ac['account']}"):
                    remaining = [a for a in acc_cfgs if a["account"] != ac["account"]]
                    save_account_configs(remaining)
                    # Clear account selector so removed account disappears
                    if "ftp_sel_accounts" in st.session_state:
                        del st.session_state["ftp_sel_accounts"]
                    st.rerun()
                updated.append({"account": ac["account"], "label": label,
                                 "balance": balance, "type": acc_type,
                                 "profit_target": profit_target,
                                 "max_loss": max_loss,
                                 "daily_loss": daily_loss})

            if st.button("💾 Save Changes", type="primary", key="cfg_save"):
                save_account_configs(updated)
                acc_cfgs = updated
                st.success("Saved.")
                st.rerun()

    if not acc_cfgs:
        st.info("Configure account labels above, then click Refresh All.")
        return

    acc_map = {a["account"]: a for a in acc_cfgs}

    # ── Auto-load on first visit + Refresh ────────────────────────────────────
    ages = [cache_age_minutes(a["account"]) for a in acc_cfgs
            if cache_age_minutes(a["account"]) < float("inf")]
    no_cache = any(cache_age_minutes(a["account"]) == float("inf") for a in acc_cfgs)

    hdr1, hdr2, hdr3, hdr4 = st.columns([3, 1, 1, 1])
    with hdr2:
        do_refresh = st.button("🔄 Refresh All", type="primary",
                               use_container_width=True)
    with hdr3:
        poll_interval = st.number_input("Auto-refresh (min)", min_value=0,
                                         max_value=60, value=5, step=5,
                                         key="ftp_poll_interval",
                                         help="0 = disabled. Page must be open.")
    with hdr4:
        if ages:
            oldest = max(ages)
            st.caption(f"Updated {oldest:.0f}m ago")

    # Auto-refresh via polling — only trigger after poll_interval has passed
    # since the last actual refresh (tracked in session state)
    auto_refresh = False
    if poll_interval > 0 and ages and not no_cache:
        last_auto = st.session_state.get("ftp_last_auto_refresh", 0)
        now_ts    = datetime.now().timestamp()
        if (now_ts - last_auto) >= poll_interval * 60:
            auto_refresh = True

    if do_refresh or no_cache or auto_refresh:
        if auto_refresh and not do_refresh:
            st.session_state["ftp_last_auto_refresh"] = datetime.now().timestamp()
        label_text = "Loading..." if no_cache else "Refreshing..."
        prog = st.progress(0, text=label_text)
        errors = []
        for i, acfg in enumerate(acc_cfgs):
            prog.progress((i + 1) / len(acc_cfgs),
                          text=f"Fetching {acfg['label']}...")
            result = refresh_account(ftp_cfg, acfg["account"], acfg["label"])
            if result.get("error"):
                errors.append(f"**{acfg['label']}**: {result['error']}")
        prog.empty()
        if errors:
            for e in errors:
                st.error(e)
        elif do_refresh:
            st.success(f"✓ Refreshed {len(acc_cfgs)} accounts")
        st.rerun()

    # ── Load all cached data ──────────────────────────────────────────────────
    all_data = []
    for acfg in acc_cfgs:
        data = load_cache(acfg["account"])
        if data:
            data["balance"] = acfg["balance"]
            data["label"]   = acfg["label"]
            all_data.append(data)

    if not all_data:
        st.info("No cached data. Click **Refresh All**.")
        return

    # ── Account selector ──────────────────────────────────────────────────────
    st.divider()
    all_labels  = [d["label"] for d in all_data]
    sel_labels  = st.multiselect("Accounts", all_labels, default=all_labels,
                                 key="ftp_sel_accounts")
    sel_data    = [d for d in all_data if d["label"] in sel_labels]

    if not sel_data:
        st.info("Select at least one account.")
        return

    # Merge all selected DataFrames
    dfs = []
    for d in sel_data:
        df = d["df"].copy()
        df["_account"]  = d["label"]
        df["_balance"]  = d["balance"]
        dfs.append(df)
    df_all = pd.concat(dfs, ignore_index=True)
    df_all["close_time"] = pd.to_datetime(df_all["close_time"], errors="coerce")
    df_all["open_time"]  = pd.to_datetime(df_all["open_time"],  errors="coerce")
    df_all = df_all.dropna(subset=["close_time"]).sort_values("close_time").reset_index(drop=True)

    total_balance = sum(d["balance"] for d in sel_data)

    # ── Account summary table ────────────────────────────────────────────────
    st.markdown("**Account Summary**")
    _sum_cards = []
    for d in sel_data:
        acfg      = acc_map.get(d["account_folder"], {})
        acc_type  = acfg.get("type", "Demo")
        balance   = d["balance"]
        df_tmp    = d["df"].copy()
        df_tmp["net_profit"]  = pd.to_numeric(df_tmp["net_profit"], errors="coerce").fillna(0)
        df_tmp["close_time"]  = pd.to_datetime(df_tmp["close_time"], errors="coerce")
        df_tmp["open_time"]   = pd.to_datetime(df_tmp["open_time"],  errors="coerce")
        df_tmp = df_tmp.sort_values("close_time").reset_index(drop=True)

        current_pnl = df_tmp["net_profit"].sum()
        current_bal = balance + current_pnl
        pnl_pct     = round(current_pnl / balance * 100, 2) if balance else 0
        pnl_color   = "#34C27A" if current_pnl >= 0 else "#E05555"
        badge_bg    = {"Demo":"rgba(124,106,247,0.3)","Personal":"rgba(52,194,122,0.3)",
                       "Prop":"rgba(255,165,0,0.3)"}.get(acc_type,"rgba(128,128,128,0.2)")

        # Report date
        rpt_date = d.get("report_date")
        try:
            fetched_str = (datetime.fromisoformat(rpt_date).strftime("%d %b %Y %H:%M")
                           if rpt_date else
                           datetime.fromisoformat(d.get("fetched_at","")).strftime("%d %b %H:%M"))
        except Exception:
            fetched_str = "—"

        # ── Recovery factor: net_profit / abs(max_dd) ─────────────────────────
        from mt5_parser import calc_stats as _cs
        _stats    = _cs(df_tmp, deposit=balance)
        max_dd    = _stats.get("max_drawdown", 0)
        recovery  = round(current_pnl / abs(max_dd), 2) if max_dd != 0 else "—"
        rec_color = "#34C27A" if isinstance(recovery, float) and recovery >= 1 else "#E05555"

        # ── Consecutive loss streak (most recent trades) ──────────────────────
        if not df_tmp.empty:
            streak = 0
            for _, row in df_tmp[::-1].iterrows():
                if row.get("win") == False or (isinstance(row.get("win"), bool) and not row["win"]):
                    streak += 1
                else:
                    break
        else:
            streak = 0
        streak_color = "#E05555" if streak >= 3 else ("#F5A623" if streak >= 1 else "#34C27A")

        # ── Stagnation: days since last equity high ───────────────────────────
        if not df_tmp.empty:
            df_tmp["_cum"]  = df_tmp["net_profit"].cumsum()
            df_tmp["_peak"] = df_tmp["_cum"].cummax()
            at_peak = df_tmp[df_tmp["_cum"] >= df_tmp["_peak"]]
            if not at_peak.empty:
                last_high = pd.to_datetime(at_peak["close_time"].max())
                stag_days = (datetime.now() - last_high).days
            else:
                stag_days = 0
        else:
            stag_days = 0
        stag_color = "#E05555" if stag_days >= 14 else ("#F5A623" if stag_days >= 7 else "#34C27A")

        # ── Today's P&L for daily loss tracking ──────────────────────────────
        today_str  = date.today().isoformat()
        today_df   = df_tmp[df_tmp["close_time"].dt.date == date.today()]
        today_pnl  = today_df["net_profit"].sum()
        today_pct  = round(today_pnl / balance * 100, 2) if balance else 0

        # ── Prop bars ─────────────────────────────────────────────────────────
        prop_bars = ""
        ea_stopped = False
        if acc_type == "Prop":
            pt   = acfg.get("profit_target", 10.0)
            ml   = acfg.get("max_loss", 10.0)
            dl   = acfg.get("daily_loss", 5.0)
            pbw  = round(min(max(pnl_pct,0), pt) / pt * 100, 1) if pt else 0
            lbw  = round(min(max(-pnl_pct,0), ml) / ml * 100, 1) if ml else 0
            dlv  = min(max(-today_pct,0), dl)
            dbw  = round(dlv / dl * 100, 1) if dl else 0
            dl_color = "#E05555" if dbw >= 80 else ("#F5A623" if dbw >= 50 else "#34C27A")
            # EA hard stop triggered when today's loss >= daily limit
            ea_stopped = dl > 0 and (-today_pct) >= dl
            stopped_banner = (
                '<div style="background:rgba(220,80,80,0.15);border:1px solid rgba(220,80,80,0.4);'
                'border-radius:4px;padding:6px 10px;margin-top:8px;font-size:12px;font-weight:600;color:#E05555">'
                '⛔ EA stopped — daily loss limit reached</div>'
            ) if ea_stopped else ""
            prop_bars = (
                '<div style="margin-top:8px">'
                f'<div style="font-size:11px;color:#555;margin-bottom:2px">Profit {pnl_pct:+.2f}% / {pt:.0f}%</div>'
                f'<div style="background:rgba(128,128,128,0.12);border-radius:3px;height:6px;margin-bottom:6px">'
                f'<div style="background:#34C27A;width:{pbw}%;height:100%;border-radius:3px"></div></div>'
                f'<div style="font-size:11px;color:#555;margin-bottom:2px">Max loss {min(max(-pnl_pct,0),ml):.2f}% / {ml:.0f}%</div>'
                f'<div style="background:rgba(128,128,128,0.12);border-radius:3px;height:6px;margin-bottom:6px">'
                f'<div style="background:#E05555;width:{lbw}%;height:100%;border-radius:3px"></div></div>'
                f'<div style="font-size:11px;color:#555;margin-bottom:2px">Daily loss {today_pct:.2f}% / {dl:.0f}%</div>'
                f'<div style="background:rgba(128,128,128,0.12);border-radius:3px;height:6px">'
                f'<div style="background:{dl_color};width:{dbw}%;height:100%;border-radius:3px"></div></div>'
                f'{stopped_banner}'
                '</div>'
            )

        card = (
            '<div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);'
            'border-radius:8px;padding:12px 16px;flex:1;min-width:220px">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">'
            f'<span style="font-size:15px;font-weight:600">{d["label"]}</span>'
            f'<span style="font-size:12px;padding:2px 8px;border-radius:4px;background:{badge_bg};font-weight:600">{acc_type}</span>'
            '</div>'
            f'<div style="font-size:12px;color:#666;margin-bottom:6px">Updated: {fetched_str}</div>'
            f'<div style="font-size:20px;font-weight:700;color:{pnl_color}">{current_pnl:+,.2f} ({pnl_pct:+.2f}%)</div>'
            f'<div style="font-size:12px;color:#555;margin-top:2px">Balance: ${balance:,.0f}  →  Current: ${current_bal:,.2f}</div>'
            f'<div style="display:flex;gap:12px;margin-top:8px;flex-wrap:wrap">'
            f'<div style="font-size:11px;color:#555">Recovery: <b style="color:{rec_color}">{recovery}</b></div>'
            f'<div style="font-size:11px;color:#555">Loss streak: <b style="color:{streak_color}">{streak}</b></div>'
            f'<div style="font-size:11px;color:#555">Stagnation: <b style="color:{stag_color}">{stag_days}d</b></div>'
            f'<div style="font-size:11px;color:#555">Today: <b style="color:{"#34C27A" if today_pnl>=0 else "#E05555"}">{today_pnl:+.2f} ({today_pct:+.2f}%)</b></div>'
            '</div>'
            f'{prop_bars}'
            '</div>'
        )
        _sum_cards.append(card)

    st.markdown(
        '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">'
        + "".join(_sum_cards) + '</div>',
        unsafe_allow_html=True)

    # ── Open trades ──────────────────────────────────────────────────────────
    _all_open = []
    for d in sel_data:
        # Prefer parsed df_open from Open Positions section
        df_op = d.get("df_open")
        if df_op is not None and not df_op.empty:
            df_op = df_op.copy()
            df_op["_account"] = d["label"]
            _all_open.append(df_op)

    if _all_open:
        open_df   = pd.concat(_all_open, ignore_index=True)
        show_cols = [c for c in ["_account","symbol","type","volume",
                                  "open_time","open_price","sl","tp"]
                     if c in open_df.columns]
        with st.expander(f"🔴 Open Positions ({len(open_df)})", expanded=True):
            st.dataframe(open_df[show_cols], use_container_width=True, hide_index=True)
    else:
        st.caption("No open positions in current reports.")

    # ── Correlation matrix ────────────────────────────────────────────────────
    if len(sel_data) > 1:
        with st.expander("📊 Symbol Correlation across Accounts", expanded=False):
            corr_rows = []
            for d in sel_data:
                df_c = d["df"].copy()
                df_c["net_profit"] = pd.to_numeric(df_c["net_profit"], errors="coerce").fillna(0)
                df_c["close_time"] = pd.to_datetime(df_c["close_time"], errors="coerce")
                by_sym = df_c.groupby("symbol")["net_profit"].sum()
                by_sym.name = d["label"]
                corr_rows.append(by_sym)
            corr_df = pd.DataFrame(corr_rows).T.fillna(0)
            if corr_df.shape[1] > 1 and len(corr_df) > 2:
                corr_matrix = corr_df.corr().round(2)
                labels = corr_matrix.columns.tolist()
                z      = corr_matrix.values.tolist()
                fig_corr = go.Figure(go.Heatmap(
                    z=z, x=labels, y=labels,
                    colorscale=[[0,"#E05555"],[0.5,"#f0f0f0"],[1,"#34C27A"]],
                    zmin=-1, zmax=1,
                    text=[[f"{v:.2f}" for v in row] for row in z],
                    texttemplate="%{text}",
                    showscale=True,
                ))
                fig_corr.update_layout(
                    height=300, title="Account Correlation (by symbol P&L)",
                    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                    font=dict(family="sans-serif"),
                    margin=dict(l=80,r=20,t=40,b=80),
                )
                st.plotly_chart(fig_corr, use_container_width=True, key="ftp_corr")
            else:
                st.caption("Not enough shared symbols across accounts to compute correlation."
                           " Symbols need to overlap between at least 2 accounts.")

    # Force balance update when account selection changes
    _bal_key = f"ftp_bal_{'_'.join(sorted(sel_labels))}"
    if st.session_state.get("ftp_last_bal_key") != _bal_key:
        st.session_state["ftp_last_bal_key"]  = _bal_key
        st.session_state["ftp_cal_balance"]   = float(total_balance)

    # ── Calendar section ──────────────────────────────────────────────────────
    st.subheader("Calendar")
    cal_bal = st.number_input(
        "Combined Balance ($) — for % calc",
        value=st.session_state.get("ftp_cal_balance", float(total_balance)),
        min_value=100.0, step=1000.0, format="%.0f",
        key="ftp_cal_balance",
        help="Auto-set from selected account balances. Override if needed."
    )
    cal_c1, cal_c2 = st.columns([2, 2])
    cal_view = cal_c1.radio("Calendar", ["Month", "Week", "Year"],
                             horizontal=True, key="ftp_cal_view")
    cal_unit = cal_c2.radio("Calendar unit", ["$", "%"],
                             horizontal=True, key="ftp_cal_unit")

    today = date.today()
    if "ftp_cal_y" not in st.session_state:
        st.session_state["ftp_cal_y"] = today.year
        st.session_state["ftp_cal_m"] = today.month
        st.session_state["ftp_cal_w"] = today.isocalendar()[1]

    nav1, nav2, nav3 = st.columns([1, 3, 1])
    with nav1:
        if st.button("◀", key="ftp_prev"):
            if cal_view == "Month":
                m = st.session_state["ftp_cal_m"] - 1
                if m < 1: m = 12; st.session_state["ftp_cal_y"] -= 1
                st.session_state["ftp_cal_m"] = m
            elif cal_view == "Week":
                w = st.session_state["ftp_cal_w"] - 1
                if w < 1:
                    st.session_state["ftp_cal_y"] -= 1
                    w = 52
                st.session_state["ftp_cal_w"] = w
            else:
                st.session_state["ftp_cal_y"] -= 1
            st.rerun()
    with nav3:
        if st.button("▶", key="ftp_next"):
            if cal_view == "Month":
                m = st.session_state["ftp_cal_m"] + 1
                if m > 12: m = 1; st.session_state["ftp_cal_y"] += 1
                st.session_state["ftp_cal_m"] = m
            elif cal_view == "Week":
                w = st.session_state["ftp_cal_w"] + 1
                if w > 52:
                    st.session_state["ftp_cal_y"] += 1
                    w = 1
                st.session_state["ftp_cal_w"] = w
            else:
                st.session_state["ftp_cal_y"] += 1
            st.rerun()
    with nav2:
        if cal_view == "Month":
            nav_label = f"{calendar.month_name[st.session_state['ftp_cal_m']]} {st.session_state['ftp_cal_y']}"
        elif cal_view == "Week":
            nav_label = f"Week {st.session_state['ftp_cal_w']} — {st.session_state['ftp_cal_y']}"
        else:
            nav_label = str(st.session_state["ftp_cal_y"])
        st.markdown(f"<h3 style='text-align:center;margin:4px 0'>{nav_label}</h3>",
                    unsafe_allow_html=True)

    # Build daily aggregates
    df_all["_day"] = df_all["close_time"].dt.date
    day_agg = df_all.groupby("_day").agg(
        pnl_dollar = ("net_profit", "sum"),
        trades     = ("net_profit", "count"),
        wins       = ("win",        "sum"),
    ).reset_index()
    day_agg["losses"]  = day_agg["trades"] - day_agg["wins"]
    day_agg["pnl_pct"] = (day_agg["pnl_dollar"] / cal_bal * 100).round(3)
    day_map = {row["_day"]: row for _, row in day_agg.iterrows()}

    # ── Summary cards for selected period ─────────────────────────────────────
    sel_y = st.session_state["ftp_cal_y"]
    sel_m = st.session_state["ftp_cal_m"]
    sel_w = st.session_state["ftp_cal_w"]

    if cal_view == "Month":
        period_days = [d for d in day_map if d.year == sel_y and d.month == sel_m]
    elif cal_view == "Week":
        period_days = [d for d in day_map
                       if d.isocalendar()[0] == sel_y and d.isocalendar()[1] == sel_w]
    else:
        period_days = [d for d in day_map if d.year == sel_y]

    period_rows = day_agg[day_agg["_day"].isin(period_days)]
    tot_pnl   = period_rows["pnl_dollar"].sum()
    tot_pct   = period_rows["pnl_pct"].sum()
    tot_tr    = int(period_rows["trades"].sum())
    tot_w     = int(period_rows["wins"].sum())
    tot_l     = int(period_rows["losses"].sum())
    wr        = round(tot_w / tot_tr * 100, 1) if tot_tr > 0 else 0
    trd_days  = len(period_rows)

    sc1,sc2,sc3,sc4,sc5,sc6 = st.columns(6)
    sc1.metric("P&L ($)",       f"${tot_pnl:,.2f}")
    sc2.metric("P&L (%)",       f"{tot_pct:+.2f}%")
    sc3.metric("Trades",        tot_tr)
    sc4.metric("Win Rate",      f"{wr}%")
    sc5.metric("Wins / Losses", f"{tot_w} / {tot_l}")
    sc6.metric("Trading Days",  trd_days)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Calendar grid ─────────────────────────────────────────────────────────
    if cal_view == "Month":
        _render_month_grid(sel_y, sel_m, day_map, today, cal_unit, cal_bal)
    elif cal_view == "Week":
        _render_week_grid(sel_y, sel_w, day_map, today, cal_unit, cal_bal)
    else:
        _render_year_grid(sel_y, day_map, today, cal_unit, cal_bal)

    # ── Trade Analysis section ────────────────────────────────────────────────
    st.divider()

    st.divider()
    st.subheader("Trade Analysis")

    # Filters
    fc1, fc2, fc3, fc4, fc5 = st.columns(5)
    with fc1:
        valid_times = df_all["open_time"].dropna()
        d_min = valid_times.min().date()
        d_max = valid_times.max().date()
        date_from = st.date_input("From", value=d_min, min_value=d_min,
                                  max_value=d_max, key="ftp_from")
        date_to   = st.date_input("To",   value=d_max, min_value=d_min,
                                  max_value=d_max, key="ftp_to")
    with fc2:
        syms    = sorted(df_all["symbol"].dropna().unique().tolist())
        sel_sym = st.multiselect("Symbol", syms, key="ftp_sym")
    with fc3:
        # Algo from comment field
        algos    = sorted(df_all["comment"].dropna().unique().tolist())
        algos    = [a for a in algos if a.strip()]
        sel_algo = st.multiselect("Algo (comment)", algos, key="ftp_algo")
    with fc4:
        days     = ["Monday","Tuesday","Wednesday","Thursday","Friday"]
        sel_days = st.multiselect("Day of week", days, key="ftp_days")
        sel_type = st.multiselect("Type", ["buy","sell"], key="ftp_type")
    with fc5:
        sel_accs = st.multiselect("Account", all_labels, default=sel_labels,
                                  key="ftp_acc_filter")
        # Auto-calculate balance from selected accounts in filter
        _acc_bal = sum(
            d["balance"] for d in all_data if d["label"] in (sel_accs or sel_labels)
        )
        _dep_key = f"ftp_dep_{'_'.join(sorted(sel_accs or sel_labels))}"
        if st.session_state.get("ftp_last_dep_key") != _dep_key:
            st.session_state["ftp_last_dep_key"] = _dep_key
            st.session_state["ftp_deposit"]      = float(_acc_bal)
        deposit = st.number_input(
            "Balance ($)",
            value=st.session_state.get("ftp_deposit", float(_acc_bal)),
            min_value=100.0, step=1000.0, format="%.0f",
            key="ftp_deposit",
            help="Auto-set from selected accounts. Override if needed."
        )

    # Apply filters
    df = df_all.copy()
    df = df[(df["open_time"].dt.date >= date_from) &
            (df["open_time"].dt.date <= date_to)]
    if sel_sym:   df = df[df["symbol"].isin(sel_sym)]
    if sel_algo:  df = df[df["comment"].isin(sel_algo)]
    if sel_days:  df = df[df["day_of_week"].isin(sel_days)]
    if sel_type:  df = df[df["type"].isin(sel_type)]
    if sel_accs:  df = df[df["_account"].isin(sel_accs)]
    df = df.reset_index(drop=True)

    st.caption(f"Showing **{len(df)}** trades after filters  ·  "
               f"Combined balance: **${deposit:,.0f}**")

    if df.empty:
        st.info("No trades match the current filters.")
        return

    # Analysis mode
    from mt5_parser import calc_stats
    mode = st.radio("Analysis mode",
                    ["Overall", "By Account", "By Symbol", "By Algo", "By Day of Week"],
                    horizontal=True, key="ftp_mode")
    st.divider()

    if mode == "Overall":
        _render_analysis(df, calc_stats(df, deposit=deposit), deposit, key_prefix="ftp_overall")

    elif mode == "By Account":
        accs = sorted(df["_account"].dropna().unique())
        rows = []
        for a in accs:
            s = calc_stats(df[df["_account"] == a], deposit=next((d["balance"] for d in all_data if d["label"]==a), 0))
            rows.append({"Account": a, "Trades": s["total_trades"],
                         "Net P&L": s["net_profit"], "Win Rate %": s["win_rate"],
                         "Profit Factor": s["profit_factor"],
                         "Expectancy": s["expectancy"], "Max DD": s["max_drawdown"]})
        st.dataframe(pd.DataFrame(rows).sort_values("Net P&L", ascending=False),
                     use_container_width=True, hide_index=True)
        st.divider()
        sel = st.selectbox("Account detail", accs, key="ftp_acc_sel")
        if sel:
            sub = df[df["_account"] == sel]
            _render_analysis(sub, calc_stats(sub, deposit=deposit), deposit, key_prefix=f"ftp_acc_{sel}")

    elif mode == "By Symbol":
        syms_u = sorted(df["symbol"].dropna().unique())
        rows   = []
        for s in syms_u:
            st_ = calc_stats(df[df["symbol"] == s], deposit=deposit)
            rows.append({"Symbol": s, "Trades": st_["total_trades"],
                         "Net P&L": st_["net_profit"], "Win Rate %": st_["win_rate"],
                         "Profit Factor": st_["profit_factor"],
                         "Expectancy": st_["expectancy"], "Max DD": st_["max_drawdown"]})
        st.dataframe(pd.DataFrame(rows).sort_values("Net P&L", ascending=False),
                     use_container_width=True, hide_index=True)
        sel = st.selectbox("Symbol detail", syms_u, key="ftp_sym_sel")
        if sel:
            sub = df[df["symbol"] == sel]
            _render_analysis(sub, calc_stats(sub, deposit=deposit), deposit, key_prefix=f"ftp_sym_{sel}")

    elif mode == "By Algo":
        algo_u = sorted(df["comment"].dropna().unique())
        algo_u = [a for a in algo_u if a.strip()]
        rows   = []
        for a in algo_u:
            st_ = calc_stats(df[df["comment"] == a], deposit=deposit)
            rows.append({"Algo": a, "Trades": st_["total_trades"],
                         "Net P&L": st_["net_profit"], "Win Rate %": st_["win_rate"],
                         "Profit Factor": st_["profit_factor"],
                         "Expectancy": st_["expectancy"], "Max DD": st_["max_drawdown"]})
        st.dataframe(pd.DataFrame(rows).sort_values("Net P&L", ascending=False),
                     use_container_width=True, hide_index=True)
        sel = st.selectbox("Algo detail", algo_u, key="ftp_algo_sel")
        if sel:
            sub = df[df["comment"] == sel]
            _render_analysis(sub, calc_stats(sub, deposit=deposit), deposit, key_prefix=f"ftp_algo_{sel}")

    elif mode == "By Day of Week":
        _render_dow(df)
        _render_hour(df)


# ── Analysis helpers ───────────────────────────────────────────────────────────

def _render_analysis(df, stats, deposit, key_prefix="ftp"):
    """Stats cards + equity + drawdown + daily P&L + DOW + hour + monthly."""
    _render_stats(stats)
    _render_equity(df, key_prefix)
    col1, col2 = st.columns(2)
    with col1:
        _render_dow(df, key_prefix)
    with col2:
        _render_hour(df, key_prefix)
    st.divider()
    _render_monthly(df, deposit, key_prefix)


def _render_stats(stats):
    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Net Profit",    f"${stats['net_profit']:,.2f}")
    c2.metric("Win Rate",      f"{stats['win_rate']}%")
    c3.metric("Profit Factor", f"{stats['profit_factor']}")
    c4.metric("R:R Ratio",     f"{stats['rr_ratio']}")
    c5.metric("Expectancy",    f"${stats['expectancy']:,.2f}")

    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Total Trades",  stats['total_trades'])
    c2.metric("Avg Win",       f"${stats['avg_win']:,.2f}")
    c3.metric("Avg Loss",      f"${stats['avg_loss']:,.2f}")
    _dd_abs = stats['max_drawdown']
    _dd_pct = stats.get('max_drawdown_pct', 0)
    c4.metric("Max DD", f"${_dd_abs:,.2f} ({abs(_dd_pct):.2f}%)")
    c5.metric("Best Trade",    f"${stats['best_trade']:,.2f}")

    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Max Consec W",  stats['max_consec_wins'])
    c2.metric("Max Consec L",  stats['max_consec_losses'])
    c3.metric("Trading Days",  stats.get('trading_days', 0))
    c4.metric("Trades/Day",    stats.get('trades_per_day', 0))
    c5.metric("Worst Trade",   f"${stats['worst_trade']:,.2f}")

    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Long Trades",   stats['long_trades'])
    c2.metric("Long WR",       f"{stats['long_win_rate']}%")
    c3.metric("Short Trades",  stats['short_trades'])
    c4.metric("Short WR",      f"{stats['short_win_rate']}%")


def _render_equity(df, key_prefix):
    df_s = df.sort_values("close_time").copy()
    df_s["_cum"]  = df_s["net_profit"].cumsum()
    df_s["_peak"] = df_s["_cum"].cummax()
    df_s["_dd"]   = df_s["_cum"] - df_s["_peak"]

    # Drawdown unit toggle
    dd_unit = st.radio("Drawdown", ["$", "%"], horizontal=True,
                       key=f"{key_prefix}_dd_unit")
    # Running balance for % dd — use peak equity as denominator
    if dd_unit == "%":
        # % drawdown = dd / peak * 100 (avoid div by zero)
        peak_safe = df_s["_peak"].replace(0, float("nan"))
        dd_vals   = (df_s["_dd"] / peak_safe * 100).fillna(0)
        dd_prefix = ""
        dd_suffix = "%"
    else:
        dd_vals   = df_s["_dd"]
        dd_prefix = "$"
        dd_suffix = ""

    LAYOUT = dict(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                  font=dict(family="sans-serif"),
                  margin=dict(l=60,r=20,t=40,b=40),
                  xaxis=dict(gridcolor="rgba(128,128,128,0.15)"),
                  yaxis=dict(gridcolor="rgba(128,128,128,0.15)", tickprefix="$"))

    fig_eq = go.Figure(go.Scatter(
        x=df_s["close_time"], y=df_s["_cum"], mode="lines", name="Equity",
        line=dict(color="#7c6af7", width=2, shape="spline", smoothing=0.6),
        fill="tozeroy", fillcolor="rgba(124,106,247,0.08)"))
    fig_eq.update_layout(height=300, title="Equity Curve",
                         hovermode="x unified", **LAYOUT)
    st.plotly_chart(fig_eq, use_container_width=True, key=f"{key_prefix}_eq")

    st.markdown("**Drawdown**")
    fig_dd = go.Figure(go.Scatter(
        x=df_s["close_time"], y=dd_vals, mode="lines",
        fill="tozeroy",
        line=dict(color="rgba(220,80,80,0.8)", width=1.5,
                  shape="spline", smoothing=0.6),
        fillcolor="rgba(220,80,80,0.15)",
        hovertemplate=f"%{{x}}<br>DD: {dd_prefix}%{{y:.2f}}{dd_suffix}<extra></extra>"))
    fig_dd.update_layout(height=130, showlegend=False,
                         xaxis=dict(gridcolor="rgba(128,128,128,0.15)",
                                    showticklabels=False),
                         yaxis=dict(gridcolor="rgba(128,128,128,0.15)",
                                    tickprefix=dd_prefix,
                                    ticksuffix=dd_suffix),
                         plot_bgcolor="rgba(0,0,0,0)",
                         paper_bgcolor="rgba(0,0,0,0)",
                         font=dict(family="sans-serif"),
                         margin=dict(l=60,r=20,t=8,b=4))
    st.plotly_chart(fig_dd, use_container_width=True, key=f"{key_prefix}_dd")

    st.markdown("**Daily P&L**")
    daily = df_s.groupby(df_s["close_time"].dt.date)["net_profit"].sum().reset_index()
    daily.columns = ["date","pnl"]
    fig_d = go.Figure(go.Bar(
        x=[str(d) for d in daily["date"]], y=daily["pnl"].round(2).tolist(),
        marker_color=["rgba(52,194,122,0.85)" if v>=0
                      else "rgba(220,80,80,0.85)" for v in daily["pnl"]]))
    fig_d.update_layout(height=160, showlegend=False,
                        xaxis=dict(type="category",
                                   gridcolor="rgba(128,128,128,0.15)",
                                   showticklabels=False),
                        yaxis=dict(gridcolor="rgba(128,128,128,0.15)",
                                   tickprefix="$", zeroline=True,
                                   zerolinecolor="rgba(128,128,128,0.3)"),
                        plot_bgcolor="rgba(0,0,0,0)",
                        paper_bgcolor="rgba(0,0,0,0)",
                        font=dict(family="sans-serif"),
                        margin=dict(l=60,r=20,t=4,b=40))
    st.plotly_chart(fig_d, use_container_width=True, key=f"{key_prefix}_daily")


def _render_dow(df, key_prefix="ftp_dow"):
    dow_order = ["Monday","Tuesday","Wednesday","Thursday","Friday"]
    present   = [d for d in dow_order if d in df["day_of_week"].values]
    wins_dow  = df[df["win"]].groupby("day_of_week")["net_profit"].sum().reindex(present, fill_value=0)
    losses_dow= df[~df["win"]].groupby("day_of_week")["net_profit"].sum().reindex(present, fill_value=0)
    fig = go.Figure()
    fig.add_trace(go.Bar(x=present, y=wins_dow.values,   name="Profit",
                         marker_color="rgba(52,194,122,0.85)"))
    fig.add_trace(go.Bar(x=present, y=losses_dow.values, name="Loss",
                         marker_color="rgba(220,80,80,0.85)"))
    fig.update_layout(height=260, title="P&L by Day of Week", barmode="relative",
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="sans-serif"),
                      xaxis=dict(type="category",
                                 gridcolor="rgba(128,128,128,0.15)"),
                      yaxis=dict(gridcolor="rgba(128,128,128,0.15)",
                                 tickprefix="$"),
                      legend=dict(bgcolor="rgba(0,0,0,0)"),
                      margin=dict(l=60,r=20,t=40,b=40))

    st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_dow")


def _render_hour(df, key_prefix="ftp_hour"):
    all_hours = sorted(df["hour"].dropna().unique())
    str_hours = [str(int(h)) for h in all_hours]
    wins_h    = df[df["win"]].groupby("hour")["net_profit"].sum().reindex(all_hours, fill_value=0)
    losses_h  = df[~df["win"]].groupby("hour")["net_profit"].sum().reindex(all_hours, fill_value=0)
    fig = go.Figure()
    fig.add_trace(go.Bar(x=str_hours, y=wins_h.values,   name="Profit",
                         marker_color="rgba(52,194,122,0.85)"))
    fig.add_trace(go.Bar(x=str_hours, y=losses_h.values, name="Loss",
                         marker_color="rgba(220,80,80,0.85)"))
    fig.update_layout(height=260, title="P&L by Hour of Day", barmode="relative",
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="sans-serif"),
                      xaxis=dict(type="category", title="Hour (UTC)",
                                 gridcolor="rgba(128,128,128,0.15)"),
                      yaxis=dict(gridcolor="rgba(128,128,128,0.15)",
                                 tickprefix="$"),
                      legend=dict(bgcolor="rgba(0,0,0,0)"),
                      margin=dict(l=60,r=20,t=40,b=40))
    st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_hour")


def _render_monthly(df, deposit, key_prefix):
    tmp = df[["close_time","net_profit"]].dropna().copy()
    tmp["close_time"] = pd.to_datetime(tmp["close_time"], errors="coerce")
    tmp["year"]  = tmp["close_time"].dt.year
    tmp["month"] = tmp["close_time"].dt.month
    monthly = tmp.groupby(["year","month"])["net_profit"].sum().reset_index()
    if monthly.empty:
        return
    pivot = monthly.pivot(index="year", columns="month",
                          values="net_profit").fillna(0)
    pivot.columns = [pd.Timestamp(2000,int(m),1).strftime("%b") for m in pivot.columns]
    pivot["YTD"]  = pivot.sum(axis=1)
    pivot = pivot.sort_index(ascending=False)
    month_order = ["Jan","Feb","Mar","Apr","May","Jun",
                   "Jul","Aug","Sep","Oct","Nov","Dec","YTD"]
    cols = [c for c in month_order if c in pivot.columns]

    c1, c2 = st.columns([1, 5])
    toggle = c1.radio("", ["$", "%"], horizontal=True, key=f"{key_prefix}_mt_toggle", label_visibility="collapsed")

    def _cell(v):
        pv  = round(v / deposit * 100, 2) if toggle == "%" else v
        bg  = "rgba(52,194,122,0.18)" if pv>0 else ("rgba(220,80,80,0.18)" if pv<0 else "transparent")
        fg  = "#34C27A" if pv>0 else ("#E05555" if pv<0 else "#888")
        txt = (f"{pv:+.2f}%" if pv!=0 else "—") if toggle=="%" else (f"{pv:+.2f}" if pv!=0 else "—")
        return f'<td style="background:{bg};color:{fg};padding:5px 10px;text-align:right;font-size:12px;font-family:monospace;border-bottom:1px solid rgba(128,128,128,0.1)">{txt}</td>'

    rows_html = ""
    for year, row in pivot[cols].iterrows():
        cells = f'<td style="padding:5px 10px;font-size:12px;font-weight:600;border-bottom:1px solid rgba(128,128,128,0.1)">{year}</td>'
        for col in cols:
            cells += _cell(row.get(col, 0))
        rows_html += f"<tr>{cells}</tr>"

    hdr = '<tr><th style="padding:5px 10px;font-size:11px;color:#888;text-align:right;border-bottom:1px solid rgba(128,128,128,0.2)">Year</th>'
    hdr += "".join(f'<th style="padding:5px 10px;font-size:11px;color:#888;text-align:right;border-bottom:1px solid rgba(128,128,128,0.2)">{c}</th>' for c in cols)
    hdr += "</tr>"

    st.markdown(
        f'<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">'
        f'<thead>{hdr}</thead><tbody>{rows_html}</tbody></table></div>',
        unsafe_allow_html=True)


# ── Calendar grid renderers ────────────────────────────────────────────────────

def _cell_html(day_num: int, row, is_today: bool, unit: str, balance: float) -> str:
    if row is not None:
        val  = row["pnl_pct"] if unit == "%" else row["pnl_dollar"]
        pos  = val >= 0
        bg   = "rgba(52,194,122,0.15)" if pos else "rgba(220,80,80,0.15)"
        vc   = "#34C27A" if pos else "#E05555"
        sign = "+" if pos else ""
        disp = f"{sign}{val:.2f}%" if unit=="%" else f"${val:,.2f}"
        alt  = f"${row['pnl_dollar']:,.2f}" if unit=="%" else f"{row['pnl_pct']:+.2f}%"
        tr   = int(row["trades"])
        content = (
            f'<div style="font-size:14px;font-weight:700;color:{vc}">'
            f'{disp} <span style="font-size:10px;font-weight:400;opacity:0.7">({alt})</span></div>'
            f'<div style="font-size:12px;color:#aaa;margin-top:3px">{tr} trade{"s" if tr!=1 else ""}</div>'
            f'<div style="font-size:12px;color:#888">✅{int(row["wins"])} ❌{int(row["losses"])}</div>'
        )
    else:
        bg = "rgba(255,255,255,0.02)"
        content = '<div style="color:#333;font-size:11px">—</div>'

    border = "border:2px solid rgba(124,106,247,0.6);" if is_today \
             else "border:1px solid rgba(255,255,255,0.06);"
    return (
        f'<td style="padding:3px;vertical-align:top">'
        f'<div style="background:{bg};border-radius:6px;{border}'
        f'padding:6px 8px;min-height:80px;min-width:90px">'
        f'<div style="font-size:11px;color:#555;margin-bottom:3px">{day_num}</div>'
        f'{content}</div></td>'
    )


def _table_wrap(hdr: str, body: str) -> str:
    return (
        '<div style="overflow-x:auto">'
        '<table style="width:100%;border-collapse:separate;border-spacing:0">'
        f'<thead><tr>{hdr}</tr></thead><tbody>{body}</tbody></table></div>'
    )


def _dow_header() -> str:
    days_hdr = "".join(
        f'<th style="text-align:center;padding:6px 0;font-size:11px;'
        f'color:#888;font-weight:500">{d}</th>'
        for d in ["Mon","Tue","Wed","Thu","Fri"]
    )
    week_hdr = '<th style="text-align:center;padding:6px 8px;font-size:11px;color:#888;font-weight:500;border-left:1px solid rgba(128,128,128,0.15)">Weekly Total</th>'
    return days_hdr + week_hdr


def _render_month_grid(year, month, day_map, today, unit, balance):
    cal  = calendar.monthcalendar(year, month)
    body = ""
    for week in cal:
        row_html = ""
        # Mon-Fri only (indices 0-4), skip Sat(5) Sun(6)
        for dow in range(5):
            day_num = week[dow]
            if day_num == 0:
                row_html += '<td style="padding:3px"></td>'
            else:
                d = date(year, month, day_num)
                row_html += _cell_html(day_num, day_map.get(d), d==today, unit, balance)

        # Weekly summary cell
        week_days = [date(year, month, week[i]) for i in range(5) if week[i] != 0]
        if week_days:
            week_rows = [day_map[d] for d in week_days if d in day_map]
            if week_rows:
                w_pnl_d = sum(r["pnl_dollar"] for r in week_rows)
                w_pnl_p = sum(r["pnl_pct"]    for r in week_rows)
                w_tr    = sum(int(r["trades"])  for r in week_rows)
                w_wins  = sum(int(r["wins"])    for r in week_rows)
                w_loss  = sum(int(r["losses"])  for r in week_rows)
                pos     = (w_pnl_d if unit == "$" else w_pnl_p) >= 0
                bg      = "rgba(52,194,122,0.12)" if pos else "rgba(220,80,80,0.12)"
                vc      = "#34C27A" if pos else "#E05555"
                disp    = f"${w_pnl_d:,.2f}" if unit == "$" else f"{w_pnl_p:+.2f}%"
                alt     = f"{w_pnl_p:+.2f}%" if unit == "$" else f"${w_pnl_d:,.2f}"
                week_cell = (
                    f'<td style="padding:3px;vertical-align:top;border-left:1px solid rgba(128,128,128,0.15)">'
                    f'<div style="background:{bg};border-radius:6px;border:1px solid rgba(255,255,255,0.06);'
                    f'padding:6px 8px;min-height:80px;min-width:80px">'
                    f'<div style="font-size:10px;color:#777;margin-bottom:3px;font-weight:500;text-transform:uppercase;letter-spacing:0.03em">Weekly</div>'
                    f'<div style="font-size:13px;font-weight:700;color:{vc}">{disp}</div>'
                    f'<div style="font-size:10px;color:{vc};opacity:0.7">({alt})</div>'
                    f'<div style="font-size:11px;color:#aaa;margin-top:3px">{w_tr} trades</div>'
                    f'<div style="font-size:11px;color:#888">✅{w_wins} ❌{w_loss}</div>'
                    f'</div></td>'
                )
            else:
                week_cell = '<td style="padding:3px;border-left:1px solid rgba(128,128,128,0.15)"><div style="min-height:80px"></div></td>'
        else:
            week_cell = '<td style="padding:3px;border-left:1px solid rgba(128,128,128,0.15)"></td>'

        body += f"<tr>{row_html}{week_cell}</tr>"
    st.markdown(_table_wrap(_dow_header(), body), unsafe_allow_html=True)


def _render_week_grid(year, week_num, day_map, today, unit, balance):
    # Get the Monday of the given ISO week
    jan4 = date(year, 1, 4)
    week_start = jan4 + timedelta(weeks=week_num - jan4.isocalendar()[1],
                                   days=-jan4.weekday())
    days = [week_start + timedelta(days=i) for i in range(5)]  # Mon-Fri only
    cells = ""
    for d in days:
        cells += _cell_html(d.day, day_map.get(d), d==today, unit, balance)
    date_hdr = "".join(
        f'<th style="text-align:center;padding:6px 4px;font-size:11px;color:#888">'
        f'{["Mon","Tue","Wed","Thu","Fri"][i]}<br>'
        f'<span style="color:#555">{days[i].strftime("%d %b")}</span></th>'
        for i in range(5)
    )
    body = f"<tr>{cells}</tr>"
    st.markdown(_table_wrap(date_hdr, body), unsafe_allow_html=True)


def _render_year_grid(year, day_map, today, unit, balance):
    """Year view — one row per month, columns = ISO weeks or just month summary."""
    month_order = list(range(1, 13))
    hdr = '<th style="padding:5px 10px;font-size:11px;color:#888;text-align:left">Month</th>'
    hdr += '<th style="padding:5px 10px;font-size:11px;color:#888;text-align:right">P&L</th>'
    hdr += '<th style="padding:5px 10px;font-size:11px;color:#888;text-align:right">Trades</th>'
    hdr += '<th style="padding:5px 10px;font-size:11px;color:#888;text-align:right">Win Rate</th>'
    hdr += '<th style="padding:5px 10px;font-size:11px;color:#888;text-align:right">Trading Days</th>'

    body = ""
    for m in month_order:
        days_in_month = [d for d in day_map if d.year==year and d.month==m]
        if not days_in_month:
            continue
        rows   = [day_map[d] for d in days_in_month]
        pnl    = sum(r["pnl_dollar"] for r in rows)
        pct    = sum(r["pnl_pct"] for r in rows)
        trades = sum(int(r["trades"]) for r in rows)
        wins   = sum(int(r["wins"]) for r in rows)
        wr     = round(wins/trades*100,1) if trades else 0
        td     = len(days_in_month)

        val  = pct if unit=="%" else pnl
        pos  = val >= 0
        bg   = "rgba(52,194,122,0.12)" if pos else "rgba(220,80,80,0.12)"
        fg   = "#34C27A" if pos else "#E05555"
        disp = f"{val:+.2f}%" if unit=="%" else f"${val:,.2f}"

        body += (
            f'<tr style="border-bottom:1px solid rgba(128,128,128,0.08)">'
            f'<td style="padding:6px 10px;font-size:12px;font-weight:600">'
            f'{calendar.month_name[m]}</td>'
            f'<td style="padding:6px 10px;text-align:right;background:{bg};'
            f'color:{fg};font-family:monospace;font-size:12px">{disp}</td>'
            f'<td style="padding:6px 10px;text-align:right;font-size:12px">{trades}</td>'
            f'<td style="padding:6px 10px;text-align:right;font-size:12px">{wr}%</td>'
            f'<td style="padding:6px 10px;text-align:right;font-size:12px">{td}</td>'
            f'</tr>'
        )

    st.markdown(
        f'<div style="overflow-x:auto">'
        f'<table style="width:100%;border-collapse:collapse">'
        f'<thead><tr>{hdr}</tr></thead><tbody>{body}</tbody></table></div>',
        unsafe_allow_html=True)