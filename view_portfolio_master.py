"""
view_portfolio_master.py — Portfolio Master
Automated portfolio construction from uploaded backtest files.
Ranks strategy combinations by Return/DD, Net Profit, or Stagnation %.
Filters by correlation, date range, min/max strategies per portfolio.
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from scipy import stats as scipy_stats
import io, importlib, sys, os, itertools
from datetime import timedelta

# ─────────────────────────────────────────────────────────────────────────────
# Parser (shared with portfolio builder)
# ─────────────────────────────────────────────────────────────────────────────
def _get_parser():
    if "mt5_parser" in sys.modules:
        return importlib.reload(sys.modules["mt5_parser"])
    import mt5_parser
    return mt5_parser


def _parse_file(file_obj):
    try:
        parser = _get_parser()
        raw    = file_obj.read()
        result = parser.detect_and_parse(raw)
        return result[0] if isinstance(result, tuple) else result
    except Exception as e:
        st.error(f"Failed to parse **{file_obj.name}**: {e}")
        return None


def _normalise(df: pd.DataFrame, label: str) -> pd.DataFrame:
    col_map = {}
    def _f(targets, dest):
        for c in targets:
            if c in df.columns and dest not in col_map.values():
                col_map[c] = dest; return
    _f(["open_time","Open time","Open time ($)","Time"],  "open_time")
    _f(["close_time","Close time"],                       "close_time")
    _f(["symbol","Symbol"],                               "symbol")
    _f(["type","Type","Direction"],                       "type")
    _f(["net_profit","P/L in money","Profit","profit"],   "net_profit")
    _f(["volume","Volume","Size","size"],                  "volume")
    _f(["commission","Commission"],                        "commission")
    _f(["swap","Swap"],                                    "swap")
    df = df.rename(columns=col_map)
    if "net_profit" not in df.columns:
        for c in ["profit","Profit","P/L"]:
            if c in df.columns:
                comm  = pd.to_numeric(df.get("commission",0), errors="coerce").fillna(0)
                swap_ = pd.to_numeric(df.get("swap",0),       errors="coerce").fillna(0)
                df["net_profit"] = pd.to_numeric(df[c], errors="coerce").fillna(0)+comm+swap_
                break
    for tc in ["open_time","close_time"]:
        if tc in df.columns:
            df[tc] = pd.to_datetime(df[tc], dayfirst=True, errors="coerce")
    if "net_profit" in df.columns:
        df["net_profit"] = pd.to_numeric(df["net_profit"], errors="coerce").fillna(0)
    df["_strategy"] = label
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Per-strategy statistics (full output columns)
# ─────────────────────────────────────────────────────────────────────────────
def _full_stats(df: pd.DataFrame, deposit: float, idx: int, custom_name: str) -> dict:
    s = {}
    if df.empty or "net_profit" not in df.columns:
        return s

    label    = df["_strategy"].iloc[0] if "_strategy" in df.columns else f"#{idx}"
    symbol   = df["symbol"].iloc[0] if "symbol" in df.columns else ""
    profits  = df["net_profit"].fillna(0)

    s["#"]              = idx
    s["Strategy Name"]  = custom_name if custom_name else label
    s["Symbol"]         = str(symbol).split(".")[0] if symbol else ""
    s["# Trades"]       = len(df)
    s["Net Profit ($)"] = round(float(profits.sum()), 2)
    s["Avg Win ($)"]    = round(float(profits[profits > 0].mean()), 2) if (profits > 0).any() else 0.0
    s["Avg Loss ($)"]   = round(float(profits[profits < 0].mean()), 2) if (profits < 0).any() else 0.0
    s["% Wins"]         = round(float((profits > 0).sum() / len(profits) * 100), 2)

    gp = float(profits[profits > 0].sum())
    gl = float(profits[profits < 0].sum())
    s["Profit Factor"]  = round(gp / abs(gl), 2) if gl else 999.0

    # Commission
    if "commission" in df.columns:
        s["Commissions ($)"] = round(float(pd.to_numeric(df["commission"], errors="coerce").fillna(0).sum()), 2)
    else:
        s["Commissions ($)"] = 0.0

    # Equity & drawdown
    eq = deposit + profits.cumsum()
    rm = eq.cummax()
    dd = eq - rm
    s["Max DD ($)"]     = round(float(dd.min()), 2)
    # DD % and Annual % both relative to the single initial deposit entered by user
    s["Max DD (%)"]     = round(float(dd.min() / deposit * 100), 2)
    s["Ret/DD"]         = round(s["Net Profit ($)"] / abs(s["Max DD ($)"]), 2) if s["Max DD ($)"] else 0.0

    # Date span
    if "close_time" in df.columns and "open_time" in df.columns:
        vc  = df["close_time"].dropna()
        vo  = df["open_time"].dropna()
        if not vc.empty:
            start = vo.min() if not vo.empty else vc.min()
            end   = vc.max()
            days  = max((end - start).days, 1)
            yrs   = days / 365.25
            s["Annual Profit ($)"] = round(s["Net Profit ($)"] / yrs, 2)
            s["Annual Profit (%)"] = round(s["Net Profit ($)"] / deposit / yrs * 100, 2)
        else:
            s["Annual Profit ($)"] = 0.0
            s["Annual Profit (%)"] = 0.0
    else:
        s["Annual Profit ($)"] = 0.0
        s["Annual Profit (%)"] = 0.0

    # Max position exposure — peak number of simultaneously open trades
    # Uses a timeline sweep: +1 at open_time, -1 at close_time
    # Works correctly for both single strategies and combined portfolios
    if "open_time" in df.columns and "close_time" in df.columns:
        try:
            trades = df[["open_time","close_time"]].dropna()
            # Build event list: (timestamp, change, is_open)
            opens  = pd.DataFrame({"dt": pd.to_datetime(trades["open_time"],  errors="coerce"), "chg": 1})
            closes = pd.DataFrame({"dt": pd.to_datetime(trades["close_time"], errors="coerce"), "chg": -1})
            ev = pd.concat([opens, closes]).dropna(subset=["dt"]).sort_values("dt").reset_index(drop=True)
            cur = mx = 0; mx_dt = None
            for _, row in ev.iterrows():
                cur += int(row["chg"])
                if cur > mx:
                    mx = cur
                    mx_dt = row["dt"]
            s["Max Pos Exposure"]    = mx
            s["Max Pos Exposure Dt"] = str(mx_dt)[:10] if mx_dt else ""
        except Exception:
            s["Max Pos Exposure"]    = 0
            s["Max Pos Exposure Dt"] = ""
    else:
        s["Max Pos Exposure"]    = 0
        s["Max Pos Exposure Dt"] = ""

    # Stagnation
    if "close_time" in df.columns:
        eq_ts = df[["close_time","net_profit"]].dropna().sort_values("close_time").copy()
        if not eq_ts.empty:
            eq_ts["cum"]  = deposit + eq_ts["net_profit"].cumsum()
            eq_ts["date"] = eq_ts["close_time"].dt.date
            dly           = eq_ts.groupby("date")["cum"].last().reset_index()
            total_days    = max((dly["date"].iloc[-1] - dly["date"].iloc[0]).days, 1)
            peak = float(dly["cum"].iloc[0])
            stag_start    = dly["date"].iloc[0]
            max_stag = 0
            for _, r in dly.iterrows():
                if float(r["cum"]) > peak:
                    peak = float(r["cum"]); stag_start = r["date"]
                else:
                    max_stag = max(max_stag, (r["date"] - stag_start).days)
            s["Stagnation (days)"] = max_stag
            s["Stagnation (%)"]    = round(max_stag / total_days * 100, 2)
        else:
            s["Stagnation (days)"] = 0
            s["Stagnation (%)"]    = 0.0
    else:
        s["Stagnation (days)"] = 0
        s["Stagnation (%)"]    = 0.0

    # Stability — R² of linear regression on equity curve
    if len(eq) > 2:
        x = np.arange(len(eq))
        slope, intercept, r, p, se = scipy_stats.linregress(x, eq.values)
        s["Stability"] = round(float(r ** 2), 4)
    else:
        s["Stability"] = 0.0

    return s


# ─────────────────────────────────────────────────────────────────────────────
# Daily P&L series for correlation
# ─────────────────────────────────────────────────────────────────────────────
def _daily_pnl(df: pd.DataFrame) -> pd.Series:
    if df.empty or "close_time" not in df.columns or "net_profit" not in df.columns:
        return pd.Series(dtype=float)
    tmp = df[["close_time","net_profit"]].dropna().copy()
    tmp["date"] = pd.to_datetime(tmp["close_time"]).dt.tz_localize(None).dt.normalize()
    return tmp.groupby("date")["net_profit"].sum()


def _correlation_matrix(dfs: dict) -> pd.DataFrame:
    series = {label: _daily_pnl(df) for label, df in dfs.items()}
    aligned = pd.DataFrame(series).fillna(0)
    return aligned.corr()


def _portfolio_exceeds_corr(members: list, corr_matrix: pd.DataFrame, max_corr: float) -> bool:
    for a, b in itertools.combinations(members, 2):
        if a in corr_matrix.index and b in corr_matrix.columns:
            if abs(corr_matrix.loc[a, b]) > max_corr:
                return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio stats (combined)
# ─────────────────────────────────────────────────────────────────────────────
def _portfolio_score(members: list, dfs: dict, deposit: float, rank_by: str) -> dict:
    if not members:
        return {}
    frames = [dfs[m].copy() for m in members if m in dfs]
    if not frames:
        return {}
    combined = pd.concat(frames, ignore_index=True)
    if "close_time" in combined.columns:
        combined = combined.sort_values("close_time").reset_index(drop=True)

    # Reuse _full_stats on the combined df — give it a synthetic label
    combined["_strategy"] = " + ".join(members)
    full = _full_stats(combined, deposit, 0, " + ".join(members))

    net_p  = full.get("Net Profit ($)", 0.0)
    max_dd = full.get("Max DD ($)", 0.0)
    ret_dd = full.get("Ret/DD", 0.0)
    stag_pct = full.get("Stagnation (%)", 0.0)

    if rank_by == "Return/DD":
        score = ret_dd
    elif rank_by == "Net Profit":
        score = net_p
    else:  # Stagnation % — lower is better, invert
        score = -stag_pct

    return {
        "members":   members,
        "score":     score,
        "net_profit":net_p,
        "max_dd":    max_dd,
        "ret_dd":    ret_dd,
        "stag_pct":  stag_pct,
        "full_stats":full,           # full column set for results table
    }


# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────
def _init_state():
    for k, v in {
        "pm_files":        {},    # label → df
        "pm_custom_names": {},    # label → custom name string
        "pm_results":      [],    # list of result dicts
        "pm_deposit":      10000.0,
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────
def render():
    _init_state()

    st.markdown("""<style>
    .pm-title{font-size:22px;font-weight:700;color:#CDD6F4;letter-spacing:.04em}
    .pm-sub{font-size:13px;color:#6C7A8D;margin-bottom:14px}
    .sh{font-size:11px;font-weight:600;color:#8899BB;text-transform:uppercase;
        letter-spacing:.1em;margin:14px 0 6px;border-bottom:1px solid #1E2535;padding-bottom:4px}
    .chip{display:inline-block;padding:3px 10px;border-radius:12px;font-size:11px;
          font-weight:600;margin:2px;border:1px solid #2A3550;color:#8899CC}
    </style>""", unsafe_allow_html=True)

    st.markdown('<p class="pm-title">🏆 Portfolio Master</p>', unsafe_allow_html=True)
    st.markdown('<p class="pm-sub">Automated portfolio construction — rank, filter and score strategy combinations</p>',
                unsafe_allow_html=True)

    # ── Upload ───────────────────────────────────────────────────────────────
    with st.expander("📂  Upload Backtest Files",
                     expanded=not bool(st.session_state.pm_files)):
        st.caption("Accepts `.htm` · `.html` · `.csv`")
        uploaded = st.file_uploader(
            "Select files", type=None, accept_multiple_files=True, key="pm_uploader",
        )
        if uploaded:
            uploaded = [f for f in uploaded
                        if f.name.lower().endswith((".htm",".html",".csv"))]
            for f in uploaded:
                stem = os.path.splitext(f.name)[0]
                if stem not in st.session_state.pm_files:
                    df = _parse_file(f)
                    if df is not None:
                        df = _normalise(df, stem)
                        st.session_state.pm_files[stem] = df
                        st.success(f"✅ **{stem}** — {len(df):,} trades")

        if st.session_state.pm_files:
            to_remove = []
            for label in list(st.session_state.pm_files):
                c1, c2 = st.columns([6,1])
                c1.markdown(f"<span class='chip'>📈 {label}</span>", unsafe_allow_html=True)
                if c2.button("✕", key=f"pmrm_{label}"):
                    to_remove.append(label)
            for k in to_remove:
                del st.session_state.pm_files[k]
                st.session_state.pm_custom_names.pop(k, None)
                st.rerun()

    strategy_dfs: dict = st.session_state.pm_files
    if not strategy_dfs:
        st.info("Upload backtest files above to get started.")
        return

    labels = list(strategy_dfs.keys())

    # ── Tabs ─────────────────────────────────────────────────────────────────
    tab_config, tab_strategies, tab_results, tab_compare = st.tabs([
        "⚙️ Configure & Run", "📊 Strategy Stats", "🏆 Results", "🔀 Compare Import",
    ])

    # ═════════════════════════════════════════════════════════════════════════
    # CONFIGURE & RUN
    # ═════════════════════════════════════════════════════════════════════════
    with tab_config:
        st.markdown('<div class="sh">Capital & Scoring</div>', unsafe_allow_html=True)
        cfg1, cfg2 = st.columns(2)
        deposit  = cfg1.number_input("Initial Deposit ($)", min_value=100.0,
                                      max_value=10_000_000.0,
                                      value=st.session_state.pm_deposit,
                                      step=1000.0, format="%.2f", key="pm_deposit")

        rank_by  = cfg2.selectbox("Rank portfolios by",
                                   ["Return/DD", "Net Profit", "% Stagnation (lower = better)"],
                                   key="pm_rank")
        rank_key = rank_by.split(" ")[0] if "Stagnation" not in rank_by else "Stagnation %"

        st.markdown('<div class="sh">Portfolio Size</div>', unsafe_allow_html=True)
        sz1, sz2, sz3 = st.columns(3)
        min_strats = sz1.number_input("Min strategies", min_value=1,
                                       max_value=len(labels), value=2,
                                       step=1, key="pm_min")
        max_strats = sz2.number_input("Max strategies", min_value=1,
                                       max_value=len(labels),
                                       value=min(5, len(labels)),
                                       step=1, key="pm_max")
        max_results= sz3.number_input("Max portfolios to store", min_value=1,
                                       max_value=500, value=50,
                                       step=10, key="pm_maxres")

        st.markdown('<div class="sh">Correlation Filter</div>', unsafe_allow_html=True)
        use_corr = st.checkbox("Enable correlation filter", value=True, key="pm_use_corr")
        corr_limit = st.slider("Max allowed pairwise correlation",
                                min_value=0.10, max_value=0.70,
                                value=0.50, step=0.05, key="pm_corr",
                                disabled=not use_corr,
                                help="Portfolios containing any pair of strategies "
                                     "with |correlation| > this value are excluded. "
                                     "Correlation is computed on daily P&L.")

        st.markdown('<div class="sh">Date Range Filter</div>', unsafe_allow_html=True)
        # Build global min/max from all loaded files
        all_dates = []
        for df in strategy_dfs.values():
            if "close_time" in df.columns:
                all_dates.append(pd.to_datetime(df["close_time"]).dt.tz_localize(None).dropna())
        if all_dates:
            g_min = min(s.min().date() for s in all_dates)
            g_max = max(s.max().date() for s in all_dates)
            use_date = st.checkbox("Filter by date range", value=False, key="pm_use_date")
            if use_date and g_min != g_max:
                import datetime as _dt
                total_days = (g_max - g_min).days
                step = max(1, total_days // 500)
                date_opts = [g_min + _dt.timedelta(days=i)
                             for i in range(0, total_days+1, step)]
                if date_opts[-1] != g_max:
                    date_opts.append(g_max)
                date_sel = st.select_slider(
                    "Date range", options=date_opts, value=(g_min, g_max),
                    format_func=lambda d: d.strftime("%d %b %Y"),
                    key="pm_daterange",
                )
                date_from, date_to = date_sel
            else:
                date_from, date_to = None, None
        else:
            date_from, date_to = None, None

        st.markdown('<div class="sh">Strategy Selection</div>', unsafe_allow_html=True)
        st.caption("Choose which uploaded strategies to include in the search.")
        sel_labels = st.multiselect(
            "Strategies to include", labels, default=labels, key="pm_sel_labels",
        )

        st.markdown("---")
        run_btn = st.button("🚀  Run Portfolio Search", type="primary", key="pm_run")

        if run_btn:
            if len(sel_labels) < max(min_strats, 1):
                st.error(f"Need at least {min_strats} strategies selected.")
            else:
                with st.spinner("Searching combinations…"):
                    # Apply date filter to each df
                    filtered_dfs = {}
                    for lbl in sel_labels:
                        df = strategy_dfs[lbl].copy()
                        if date_from and date_to and "close_time" in df.columns:
                            ct = pd.to_datetime(df["close_time"]).dt.tz_localize(None)
                            df = df[(ct >= pd.Timestamp(date_from)) &
                                    (ct <= pd.Timestamp(date_to) + timedelta(days=1))]
                        if not df.empty:
                            filtered_dfs[lbl] = df

                    if not filtered_dfs:
                        st.error("No data in selected date range.")
                    else:
                        corr_matrix = _correlation_matrix(filtered_dfs) if use_corr else None

                        results = []
                        total_combos = sum(
                            len(list(itertools.combinations(list(filtered_dfs.keys()), r)))
                            for r in range(min_strats, max_strats + 1)
                        )
                        prog = st.progress(0, text="Evaluating combinations…")
                        done = 0

                        for size in range(int(min_strats), int(max_strats) + 1):
                            for combo in itertools.combinations(list(filtered_dfs.keys()), size):
                                combo = list(combo)
                                done += 1
                                if done % 50 == 0:
                                    prog.progress(min(done / max(total_combos, 1), 1.0),
                                                  text=f"Evaluated {done:,} / {total_combos:,}")

                                if use_corr and corr_matrix is not None:
                                    if _portfolio_exceeds_corr(combo, corr_matrix, corr_limit):
                                        continue

                                result = _portfolio_score(combo, filtered_dfs, deposit, rank_key)
                                if result:
                                    results.append(result)

                        prog.progress(1.0, text="Done.")
                        results.sort(key=lambda x: x["score"], reverse=True)
                        st.session_state.pm_results = results[:int(max_results)]
                        st.success(f"Found **{len(results):,}** valid portfolios → "
                                   f"showing top **{len(st.session_state.pm_results)}**.")

    # ═════════════════════════════════════════════════════════════════════════
    # STRATEGY STATS TABLE
    # ═════════════════════════════════════════════════════════════════════════
    with tab_strategies:
        st.markdown("##### Individual Strategy Statistics")
        st.caption("Edit the Strategy Name column to assign custom names. "
                   "These names carry through to the Results tab.")

        deposit_s = st.session_state.pm_deposit
        rows = []
        for i, label in enumerate(labels):
            custom = st.session_state.pm_custom_names.get(label, "")
            row = _full_stats(strategy_dfs[label], deposit_s, i + 1, custom)
            if row:
                rows.append(row)

        if rows:
            stats_df = pd.DataFrame(rows)

            # Column order
            col_order = [
                "#", "Strategy Name", "Symbol", "# Trades",
                "Net Profit ($)", "Max DD ($)", "Max DD (%)",
                "Annual Profit ($)", "Annual Profit (%)",
                "Avg Win ($)", "Avg Loss ($)", "% Wins",
                "Commissions ($)", "Max Pos Exposure", "Max Pos Exposure Dt",
                "Stagnation (%)", "Stagnation (days)", "Profit Factor",
                "Ret/DD", "Stability",
            ]
            col_order = [c for c in col_order if c in stats_df.columns]
            stats_df  = stats_df[col_order]

            # Editable table — only Strategy Name is editable
            edited = st.data_editor(
                stats_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "#":                  st.column_config.NumberColumn("#", disabled=True, width="small"),
                    "Strategy Name":      st.column_config.TextColumn("Strategy Name", width="medium"),
                    "Symbol":             st.column_config.TextColumn("Symbol", disabled=True),
                    "# Trades":           st.column_config.NumberColumn("# Trades", disabled=True, format="%d"),
                    "Net Profit ($)":     st.column_config.NumberColumn("Net Profit ($)", disabled=True, format="%.2f"),
                    "Max DD ($)":         st.column_config.NumberColumn("Max DD ($)", disabled=True, format="%.2f"),
                    "Max DD (%)":         st.column_config.NumberColumn("Max DD (%)", disabled=True, format="%.2f"),
                    "Annual Profit ($)":  st.column_config.NumberColumn("Annual Profit ($)", disabled=True, format="%.2f"),
                    "Annual Profit (%)":  st.column_config.NumberColumn("Annual Profit (%)", disabled=True, format="%.2f"),
                    "Avg Win ($)":        st.column_config.NumberColumn("Avg Win ($)", disabled=True, format="%.2f"),
                    "Avg Loss ($)":       st.column_config.NumberColumn("Avg Loss ($)", disabled=True, format="%.2f"),
                    "% Wins":             st.column_config.NumberColumn("% Wins", disabled=True, format="%.2f"),
                    "Commissions ($)":    st.column_config.NumberColumn("Commissions ($)", disabled=True, format="%.2f"),
                    "Max Pos Exposure":   st.column_config.NumberColumn("Max Pos Exp", disabled=True, format="%d"),
                    "Max Pos Exposure Dt":st.column_config.TextColumn("Max Pos Date", disabled=True),
                    "Stagnation (%)":     st.column_config.NumberColumn("Stagnation (%)", disabled=True, format="%.2f"),
                    "Stagnation (days)":  st.column_config.NumberColumn("Stagnation (d)", disabled=True, format="%d"),
                    "Profit Factor":      st.column_config.NumberColumn("PF", disabled=True, format="%.2f"),
                    "Ret/DD":             st.column_config.NumberColumn("Ret/DD", disabled=True, format="%.2f"),
                    "Stability":          st.column_config.NumberColumn("Stability", disabled=True, format="%.4f",
                                          help="R² of linear regression on equity curve. 1.0 = perfectly straight rising line."),
                },
                key="pm_stats_editor",
            )

            # Save any custom name edits back to session state
            for _, row in edited.iterrows():
                orig_label = labels[int(row["#"]) - 1]
                new_name   = str(row["Strategy Name"]).strip()
                if new_name and new_name != orig_label:
                    st.session_state.pm_custom_names[orig_label] = new_name
                else:
                    st.session_state.pm_custom_names.pop(orig_label, None)

            # Correlation heatmap
            if len(labels) > 1:
                st.markdown("##### Pairwise Correlation (Daily P&L)")
                corr = _correlation_matrix(strategy_dfs)
                display_labels = [
                    st.session_state.pm_custom_names.get(l, l) for l in corr.columns
                ]
                # Text colour: dark for light cells (near zero), white for dark cells
                text_vals = np.round(corr.values, 2)
                text_colors = [["#1a1a2e" if abs(v) < 0.4 else "#FFFFFF"
                                 for v in row] for row in corr.values]

                fig_corr = go.Figure(go.Heatmap(
                    z=corr.values,
                    x=display_labels, y=display_labels,
                    colorscale=[
                        [0.00, "#2166AC"],   # strong negative — blue
                        [0.25, "#92C5DE"],   # mild negative — light blue
                        [0.50, "#E8E8E8"],   # zero — light grey
                        [0.75, "#F4A582"],   # mild positive — salmon
                        [1.00, "#B2182B"],   # strong positive — red
                    ],
                    zmid=0, zmin=-1, zmax=1,
                    text=text_vals,
                    texttemplate="%{text}",
                    textfont=dict(size=11, color="#1a1a2e"),
                    hovertemplate="%{x} / %{y}: %{z:.3f}<extra></extra>",
                ))
                fig_corr.update_layout(
                    height=max(300, len(labels) * 55),
                    margin=dict(l=20, r=80, t=10, b=10),
                    paper_bgcolor="#F0F2F6",
                    plot_bgcolor="#F0F2F6",
                    xaxis=dict(tickfont=dict(size=10, color="#333"),
                               tickangle=-30),
                    yaxis=dict(tickfont=dict(size=10, color="#333")),
                    coloraxis_colorbar=dict(
                        tickfont=dict(color="#333"),
                        outlinecolor="#ccc",
                    ),
                )
                st.plotly_chart(fig_corr, use_container_width=True)

    # ═════════════════════════════════════════════════════════════════════════
    # RESULTS
    # ═════════════════════════════════════════════════════════════════════════
    with tab_results:
        results = st.session_state.pm_results
        if not results:
            st.info("Run the portfolio search on the Configure tab first.")
        else:
            st.markdown(f"##### Top {len(results)} Portfolios")

            def _name(lbl):
                return st.session_state.pm_custom_names.get(lbl, lbl)

            rank_label = {
                "Return/DD":    "Ret/DD",
                "Net Profit":   "Net Profit ($)",
                "Stagnation %": "Stagnation (%)",
            }.get(rank_key, "Score")

            # Build results table with same columns as Strategy Stats
            col_order = [
                "Rank", "Strategies", "# Strategies",
                "# Trades", "Net Profit ($)", "Max DD ($)", "Max DD (%)",
                "Annual Profit ($)", "Annual Profit (%)",
                "Avg Win ($)", "Avg Loss ($)", "% Wins",
                "Commissions ($)", "Max Pos Exposure", "Max Pos Exposure Dt",
                "Stagnation (%)", "Stagnation (days)", "Profit Factor",
                "Ret/DD", "Stability",
            ]
            rows_r = []
            for i, r in enumerate(results):
                member_names = " + ".join(_name(m) for m in r["members"])
                fs = r.get("full_stats", {})
                row = {"Rank": i + 1, "Strategies": member_names,
                       "# Strategies": len(r["members"])}
                for col in col_order[3:]:   # skip Rank, Strategies, # Strategies
                    row[col] = fs.get(col, 0)
                rows_r.append(row)

            res_df = pd.DataFrame(rows_r)
            res_df = res_df[[c for c in col_order if c in res_df.columns]]

            def _cc(val, low=0):
                if not isinstance(val, (int,float)): return ""
                return "color:#34C27A" if val > low else "color:#E05555" if val < low else ""

            int_cols  = {"Rank", "# Strategies", "# Trades", "Max Pos Exposure",
                         "Stagnation (days)"}
            num_cols  = res_df.select_dtypes(include="number").columns.tolist()
            fmt       = {c: ("{:.0f}" if c in int_cols else "{:.2f}") for c in num_cols}
            fmt["Stability"] = "{:.4f}"

            pos_cols  = [c for c in ["Net Profit ($)", "Annual Profit ($)",
                                      "Annual Profit (%)", "Avg Win ($)"] if c in res_df.columns]
            neg_cols  = [c for c in ["Max DD ($)", "Max DD (%)",
                                      "Avg Loss ($)"] if c in res_df.columns]

            styled = (
                res_df.style
                .format(fmt)
                .map(_cc,              subset=pos_cols if pos_cols else [])
                .map(lambda v: "color:#E05555" if isinstance(v,(int,float)) and v < 0 else "",
                     subset=neg_cols if neg_cols else [])
                .map(lambda v: _cc(v, 1.0),
                     subset=["Profit Factor"] if "Profit Factor" in res_df.columns else [])
            )
            st.dataframe(styled, use_container_width=True, hide_index=True)

            # Export
            buf = io.StringIO()
            res_df.to_csv(buf, index=False)
            st.download_button("⬇️ Export Results CSV", buf.getvalue(),
                               file_name="portfolio_master_results.csv", mime="text/csv")

            # Expandable detail for top N portfolios
            st.markdown("##### Portfolio Detail")
            show_top = st.slider("Show detail for top N portfolios", 1, min(10, len(results)),
                                  min(5, len(results)), key="pm_show_top")
            for i, r in enumerate(results[:show_top]):
                member_names = " + ".join(_name(m) for m in r["members"])
                with st.expander(f"#{i+1}  {member_names}  "
                                  f"| Ret/DD {r['ret_dd']:.2f} "
                                  f"| Net ${r['net_profit']:,.2f} "
                                  f"| DD ${r['max_dd']:,.2f}"):
                    # Mini equity chart
                    frames = [strategy_dfs[m].copy() for m in r["members"] if m in strategy_dfs]
                    if frames:
                        combined = pd.concat(frames, ignore_index=True)
                        if "close_time" in combined.columns:
                            combined = combined.sort_values("close_time").reset_index(drop=True)
                        eq = st.session_state.pm_deposit + combined["net_profit"].cumsum()
                        rm = eq.cummax(); dd_c = eq - rm
                        pfig = go.Figure()
                        pfig.add_trace(go.Scatter(
                            x=combined["close_time"], y=eq,
                            name="Equity", line=dict(color="#4C8EF5", width=2),
                            mode="lines",
                        ))
                        pfig.add_trace(go.Scatter(
                            x=combined["close_time"], y=dd_c,
                            name="DD", fill="tozeroy",
                            fillcolor="rgba(220,50,50,0.25)",
                            line=dict(color="rgba(220,50,50,0.6)", width=1),
                            mode="lines", yaxis="y2",
                        ))
                        pfig.update_layout(
                            height=220,
                            margin=dict(l=40,r=40,t=10,b=10),
                            paper_bgcolor="rgba(0,0,0,0)",
                            plot_bgcolor="#0E1117",
                            hovermode="x unified",
                            legend=dict(orientation="h", y=1.1, font=dict(size=10)),
                            yaxis=dict(gridcolor="#1E2130", tickprefix="$"),
                            yaxis2=dict(overlaying="y", side="right",
                                        gridcolor="#1E2130", tickprefix="$",
                                        showgrid=False),
                        )
                        st.plotly_chart(pfig, use_container_width=True)

                    # Member stats
                    m_rows = []
                    for m in r["members"]:
                        if m not in strategy_dfs: continue
                        s = _full_stats(strategy_dfs[m], st.session_state.pm_deposit,
                                        labels.index(m)+1,
                                        st.session_state.pm_custom_names.get(m,""))
                        if s:
                            m_rows.append({
                                "Strategy":       s.get("Strategy Name", m),
                                "Symbol":         s.get("Symbol",""),
                                "Net Profit ($)": s.get("Net Profit ($)",0),
                                "Max DD ($)":     s.get("Max DD ($)",0),
                                "Ret/DD":         s.get("Ret/DD",0),
                                "% Wins":         s.get("% Wins",0),
                                "Profit Factor":  s.get("Profit Factor",0),
                                "Stability":      s.get("Stability",0),
                            })
                    if m_rows:
                        mdf = pd.DataFrame(m_rows)
                        st.dataframe(
                            mdf.style.format({c:"{:.2f}" for c in mdf.select_dtypes("number").columns}),
                            use_container_width=True, hide_index=True,
                        )

    # ═════════════════════════════════════════════════════════════════════════
    # COMPARE IMPORT
    # ═════════════════════════════════════════════════════════════════════════
    with tab_compare:
        st.markdown("##### Compare External Portfolio Export")
        st.caption(
            "Upload a CSV exported from another portfolio tool (e.g. Quant Analyzer) "
            "alongside your own results export from the Results tab. "
            "Portfolios are matched by their strategy members — mismatches are flagged."
        )

        cc1, cc2 = st.columns(2)
        ext_file  = cc1.file_uploader("External tool export (CSV)", type=None,
                                       key="pm_ext_file",
                                       help="e.g. Portfolios_13_04_2026.csv")
        our_file  = cc2.file_uploader("Our results export (CSV)", type=None,
                                       key="pm_our_file",
                                       help="Export from the Results tab above")

        # ── Column mapping from external format → our format ─────────────────
        # External: Strategy Name, Initial deposit, Symbol, # of trades,
        #   Net profit, Drawdown, Max DD %, Annual % Return,
        #   Annual % Return/Max DD %, Avg. Loss, Avg. Win, Win/Loss ratio,
        #   % Wins, Commission, Max Positions Exposure, Max Positions Exposure Date,
        #   % Stagnation, Profit factor, Ret/DD Ratio, R Expectancy, Sharpe Ratio, Stability
        EXT_MAP = {
            "# of trades":               "# Trades",
            "Net profit":                "Net Profit ($)",
            "Drawdown":                  "Max DD ($)",
            "Max DD %":                  "Max DD (%)",
            "Annual % Return":           "Annual Profit (%)",
            "Annual % Return/Max DD %":  "Ret/DD",
            "Avg. Loss":                 "Avg Loss ($)",
            "Avg. Win":                  "Avg Win ($)",
            "% Wins":                    "% Wins",
            "Commission":                "Commissions ($)",
            "Max Positions Exposure":    "Max Pos Exposure",
            "Max Positions Exposure Date":"Max Pos Exposure Dt",
            "% Stagnation":              "Stagnation (%)",
            "Profit factor":             "Profit Factor",
            "Ret/DD Ratio":              "Ret/DD",
            "Stability":                 "Stability",
        }

        COMPARE_COLS = [
            "# Trades", "Net Profit ($)", "Max DD ($)", "Max DD (%)",
            "Annual Profit (%)", "Avg Win ($)", "Avg Loss ($)", "% Wins",
            "Commissions ($)", "Max Pos Exposure", "Stagnation (%)",
            "Profit Factor", "Ret/DD", "Stability",
        ]

        def _parse_ext(f) -> pd.DataFrame:
            raw = f.read().decode("utf-8-sig", errors="replace")
            from io import StringIO
            df  = pd.read_csv(StringIO(raw))
            df  = df.rename(columns=EXT_MAP)
            # Build a normalised member key from Symbol column
            # Symbol looks like "audusd_a,chfjpy_a,Portfolio" — strip "Portfolio",
            # strip broker suffix (.a/.b), sort alphabetically
            def _key(sym):
                parts = [s.strip().lower() for s in str(sym).split(",")
                         if s.strip().lower() not in ("portfolio","")]
                parts = [p.split(".")[0] if "." in p else p for p in parts]
                return " + ".join(sorted(parts))
            df["_match_key"] = df["Symbol"].apply(_key)
            # Normalise Max DD to negative (external stores as positive)
            if "Max DD ($)" in df.columns:
                df["Max DD ($)"] = -df["Max DD ($)"].abs()
            if "Max DD (%)" in df.columns:
                df["Max DD (%)"] = -df["Max DD (%)"].abs()
            if "Avg Loss ($)" in df.columns:
                df["Avg Loss ($)"] = -df["Avg Loss ($)"].abs()
            if "Commissions ($)" in df.columns:
                df["Commissions ($)"] = -df["Commissions ($)"].abs()
            return df

        def _parse_our(f) -> pd.DataFrame:
            raw = f.read().decode("utf-8-sig", errors="replace")
            from io import StringIO
            df  = pd.read_csv(StringIO(raw))
            # Build match key from Strategies column "audusd_a + chfjpy_a"
            def _key(strat):
                parts = [s.strip().lower() for s in str(strat).split("+")]
                parts = [p.split(".")[0] if "." in p else p for p in parts]
                return " + ".join(sorted(parts))
            df["_match_key"] = df["Strategies"].apply(_key)
            return df

        if ext_file and our_file:
            try:
                ext_df = _parse_ext(ext_file)
                our_df = _parse_our(our_file)

                # ── Match portfolios by member key ────────────────────────────
                ext_keys = set(ext_df["_match_key"].tolist())
                our_keys = set(our_df["_match_key"].tolist())
                matched  = ext_keys & our_keys
                only_ext = ext_keys - our_keys
                only_our = our_keys - ext_keys

                st.markdown(f"**{len(matched)} matched** · "
                            f"{len(only_ext)} only in external · "
                            f"{len(only_our)} only in our results")

                if only_ext:
                    with st.expander(f"⚠️ {len(only_ext)} portfolios only in external file"):
                        for k in sorted(only_ext):
                            st.markdown(f"- `{k}`")
                if only_our:
                    with st.expander(f"⚠️ {len(only_our)} portfolios only in our results"):
                        for k in sorted(only_our):
                            st.markdown(f"- `{k}`")

                if matched:
                    # ── Side-by-side diff table ───────────────────────────────
                    st.markdown("##### Side-by-Side Comparison (matched portfolios)")

                    show_cols = [c for c in COMPARE_COLS
                                 if c in ext_df.columns and c in our_df.columns]

                    diff_rows = []
                    for key in sorted(matched):
                        ext_row = ext_df[ext_df["_match_key"] == key].iloc[0]
                        our_row = our_df[our_df["_match_key"] == key].iloc[0]

                        # External deposit (each portfolio has its own)
                        ext_dep = float(ext_row.get("Initial deposit", 10000))

                        row_base = {"Portfolio": key.replace(" + ", " + ")}
                        for col in show_cols:
                            e_val = ext_row.get(col, None)
                            o_val = our_row.get(col, None)
                            try:
                                e_f = float(e_val) if e_val is not None else None
                                o_f = float(o_val) if o_val is not None else None
                            except (ValueError, TypeError):
                                e_f = o_f = None

                            row_base[f"{col} [ext]"] = round(e_f, 2) if e_f is not None else ""
                            row_base[f"{col} [ours]"] = round(o_f, 2) if o_f is not None else ""

                            # Delta — only for numeric, skip date/text cols
                            if e_f is not None and o_f is not None:
                                row_base[f"{col} Δ"] = round(o_f - e_f, 2)
                            else:
                                row_base[f"{col} Δ"] = ""

                        diff_rows.append(row_base)

                    diff_df = pd.DataFrame(diff_rows)

                    # Toggle: show all columns or just deltas
                    view_mode = st.radio("Show", ["All columns", "Deltas only", "External only", "Ours only"],
                                          horizontal=True, key="pm_cmp_mode")

                    if view_mode == "Deltas only":
                        keep = ["Portfolio"] + [c for c in diff_df.columns if c.endswith(" Δ")]
                    elif view_mode == "External only":
                        keep = ["Portfolio"] + [c for c in diff_df.columns if c.endswith("[ext]")]
                    elif view_mode == "Ours only":
                        keep = ["Portfolio"] + [c for c in diff_df.columns if c.endswith("[ours]")]
                    else:
                        keep = diff_df.columns.tolist()

                    disp = diff_df[keep].copy()

                    # Colour delta columns: green = improvement, red = worse
                    # "improvement" depends on metric direction
                    HIGHER_BETTER = {"Net Profit ($)", "Annual Profit (%)", "% Wins",
                                     "Profit Factor", "Ret/DD", "Stability", "Avg Win ($)"}
                    LOWER_BETTER  = {"Max DD ($)", "Max DD (%)", "Stagnation (%)",
                                     "Commissions ($)", "Avg Loss ($)"}

                    def _delta_style(val, col_name):
                        if not isinstance(val, (int,float)) or val == 0:
                            return ""
                        metric = col_name.replace(" Δ","").strip()
                        if metric in HIGHER_BETTER:
                            return "color:#34C27A" if val > 0 else "color:#E05555"
                        if metric in LOWER_BETTER:
                            return "color:#34C27A" if val < 0 else "color:#E05555"
                        return ""

                    num_c = disp.select_dtypes(include="number").columns.tolist()
                    fmt_d = {c: "{:.2f}" for c in num_c}

                    styler = disp.style.format(fmt_d, na_rep="—")
                    for col in [c for c in disp.columns if c.endswith(" Δ")]:
                        styler = styler.map(lambda v, c=col: _delta_style(v, c), subset=[col])

                    st.dataframe(styler, use_container_width=True, hide_index=True)

                    # ── Summary metrics ───────────────────────────────────────
                    st.markdown("##### Average Deltas (Ours − External)")
                    delta_cols = [c for c in diff_df.columns if c.endswith(" Δ")]
                    if delta_cols:
                        delta_means = {}
                        for col in delta_cols:
                            vals = pd.to_numeric(diff_df[col], errors="coerce").dropna()
                            if not vals.empty:
                                delta_means[col.replace(" Δ","")] = round(vals.mean(), 3)

                        dm_cols = st.columns(min(len(delta_means), 5))
                        for i, (metric, val) in enumerate(delta_means.items()):
                            col_idx = i % len(dm_cols)
                            m = metric
                            if m in HIGHER_BETTER:
                                delta_str = f"+{val:.3f}" if val >= 0 else f"{val:.3f}"
                                color = "#34C27A" if val >= 0 else "#E05555"
                            elif m in LOWER_BETTER:
                                delta_str = f"{val:.3f}"
                                color = "#34C27A" if val <= 0 else "#E05555"
                            else:
                                delta_str = f"{val:+.3f}"
                                color = "#CDD6F4"
                            dm_cols[col_idx].markdown(
                                f"<div style='text-align:center;padding:8px;background:#131720;"
                                f"border-radius:6px;margin:2px'>"
                                f"<div style='font-size:10px;color:#6C7A8D'>{m}</div>"
                                f"<div style='font-size:16px;font-weight:700;color:{color}'>"
                                f"{delta_str}</div></div>",
                                unsafe_allow_html=True
                            )

                    # Export comparison
                    buf = io.StringIO()
                    diff_df.to_csv(buf, index=False)
                    st.download_button("⬇️ Export Comparison CSV", buf.getvalue(),
                                       file_name="portfolio_comparison.csv", mime="text/csv")

            except Exception as e:
                st.error(f"Error processing files: {e}")
                import traceback
                st.code(traceback.format_exc())

        else:
            st.info("Upload both files above to run the comparison.")