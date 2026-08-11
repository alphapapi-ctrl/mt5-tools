"""
view_ea_cost_stress.py — 💸 Cost Stress tab for the EA Stress Tester.

Models broker execution costs on top of an ideal-fill backtest:
  - fixed slippage (points per side) and extra commission
  - execution delay (e.g. FTMO's ~200 ms simulated delay on AU servers),
    costed either statistically from the EA's own price speed, or replayed
    against real tick data (QuantDataManager / MT5 / Dukascopy CSV) via
    tick_data.py — the file is binary-searched, never loaded.

Everything is derived from the report's own trade list: $-per-price-unit,
point size and price speed are inferred and overridable.
"""

import os

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from tick_data import (TickFile, TickFormatError, collect_event_windows,
                       delay_costs)


# ─────────────────────────────────────────────────────────────────────────────
# Inference from the trade list
# ─────────────────────────────────────────────────────────────────────────────
def _dir_sign(type_series) -> np.ndarray:
    return np.where(type_series.astype(str).str.lower()
                    .str.contains("buy"), 1.0, -1.0)


def _infer_contract_value(d: pd.DataFrame) -> float:
    """$ per 1.0 price unit per lot — median of net_profit ÷ (move × lots).
    Commission/swap inside net_profit is noise the median shrugs off."""
    need = {"open_price", "close_price", "net_profit", "volume"}
    if not need.issubset(d.columns):
        return 0.0
    op = pd.to_numeric(d["open_price"], errors="coerce")
    cp = pd.to_numeric(d["close_price"], errors="coerce")
    np_ = pd.to_numeric(d["net_profit"], errors="coerce")
    vol = pd.to_numeric(d["volume"], errors="coerce")
    move = (cp - op) * _dir_sign(d["type"])
    ok = move.abs().gt(1e-9) & vol.gt(0) & np_.notna()
    if not ok.any():
        return 0.0
    est = (np_[ok] / (move[ok] * vol[ok]))
    est = est[(est > 1e-4) & (est < 1e7)]
    return float(est.median()) if len(est) else 0.0


def _infer_point_size(prices) -> float:
    """1 point in price units, from the price decimals (gold 0.01, FX 1e-5)."""
    s = pd.to_numeric(pd.Series(prices), errors="coerce").dropna().head(500)
    if s.empty:
        return 0.01
    decs = []
    for v in s:
        txt = f"{v:.6f}".rstrip("0")
        decs.append(len(txt.split(".")[1]) if "." in txt and
                    not txt.endswith(".") else 0)
    return float(10 ** -int(np.percentile(decs, 90)))


def _price_speed(d: pd.DataFrame):
    """Median / p90 price speed (price units per second) from each trade's
    net displacement over its holding time. Conservative — the tick path
    moves faster than the displacement; tick replay measures the truth."""
    if "duration_min" not in d.columns:
        return 0.0, 0.0
    dur_s = pd.to_numeric(d["duration_min"], errors="coerce") * 60
    op = pd.to_numeric(d["open_price"], errors="coerce")
    cp = pd.to_numeric(d["close_price"], errors="coerce")
    spd = ((cp - op).abs() / dur_s)[dur_s >= 1].dropna()
    if spd.empty:
        return 0.0, 0.0
    return float(spd.median()), float(np.percentile(spd, 90))


def _to_epoch(series) -> np.ndarray:
    t = pd.to_datetime(series)
    try:
        t = t.dt.tz_localize(None)
    except TypeError:
        pass
    return ((t - pd.Timestamp("1970-01-01")).dt.total_seconds()).values


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────
def _metrics(dollars: np.ndarray, account: float) -> dict:
    wins = dollars[dollars > 0]
    losses = dollars[dollars < 0]
    cum = np.cumsum(dollars) / account * 100
    dd = float((cum - np.maximum.accumulate(cum)).min()) if len(cum) else 0.0
    net = float(dollars.sum())
    pf = float(wins.sum() / abs(losses.sum())) if losses.sum() else float("inf")
    return {
        "Net profit ($)": net,
        "Profit factor": pf,
        "Win rate (%)": float((dollars > 0).mean() * 100) if len(dollars) else 0,
        "Avg trade ($)": float(dollars.mean()) if len(dollars) else 0,
        "Max DD (% acct)": dd,
        "Return / DD": (net / account * 100) / abs(dd) if dd else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tab
# ─────────────────────────────────────────────────────────────────────────────
def render_cost_stress(d_sorted: pd.DataFrame, account: float, trade_lots,
                       lots_lbl: str, sym: str, page_key: str):
    st.markdown("*A backtest fills at the ideal price. This tab re-prices every "
                "trade with real-world costs: slippage, commission, and "
                "execution delay — FTMO's AU servers add up to ~200 ms, which "
                "just means the market moves before you're filled.*")

    n = len(d_sorted)
    if n < 5 or "_per_lot" not in d_sorted.columns:
        st.info("Not enough trades to stress.")
        return
    per_lot = d_sorted["_per_lot"].values.astype(float)
    lots_arr = (np.full(n, float(trade_lots)) if np.isscalar(trade_lots)
                else np.asarray(trade_lots, dtype=float))

    # ── Cost model ────────────────────────────────────────────────────────
    cv_inf = _infer_contract_value(d_sorted)
    pt_inf = _infer_point_size(d_sorted.get("open_price"))
    c1, c2, c3, c4 = st.columns(4)
    contract_val = c1.number_input(
        "$ / price unit / lot", value=round(cv_inf, 2) if cv_inf else 100.0,
        min_value=0.01, key=f"cs_cv_{page_key}",
        help="Inferred from the trade list: net_profit ÷ (price move × lots), "
             "median across trades. Gold ≈ 100, FX majors ≈ 100k × quote ccy.")
    point_size = c4.number_input(
        "Point size", value=pt_inf, min_value=1e-6, format="%.6f",
        key=f"cs_pt_{page_key}",
        help="1 point in price units, inferred from the price decimals — "
             "0.01 on gold, 0.00001 on 5-digit FX.")
    slip_pts = c2.number_input(
        "Slippage (points / side)", value=0.0, min_value=0.0, step=1.0,
        key=f"cs_slip_{page_key}",
        help="Fixed adverse slippage applied per fill, on top of any delay "
             "cost below.")
    comm = c3.number_input(
        "Extra commission ($ / lot round turn)", value=0.0, min_value=0.0,
        step=0.5, key=f"cs_comm_{page_key}",
        help="For comparing brokers — cost added per lot per round turn.")
    s1, s2, _ = st.columns([1, 1, 2])
    ap_entry = s1.checkbox("Apply to entries", value=True, key=f"cs_ae_{page_key}")
    ap_exit = s2.checkbox("Apply to exits", value=True, key=f"cs_ax_{page_key}",
                          help="Limit-order EAs largely sidestep entry slippage "
                               "— untick a side to model that. The report can't "
                               "tell order types apart.")
    n_sides = int(ap_entry) + int(ap_exit)

    # ── Execution delay ───────────────────────────────────────────────────
    st.markdown("##### Execution delay")
    dm1, dm2 = st.columns([2, 2])
    mode = dm1.radio("Delay costing", ["Statistical estimate",
                                       "Tick-data replay (CSV)"],
                     horizontal=True, key=f"cs_mode_{page_key}",
                     help="Statistical assumes every delayed fill is adverse, "
                          "at the symbol's typical price speed. Tick replay "
                          "measures the actual move during the delay at every "
                          "fill — favourable and adverse.")
    delay_ms = dm2.slider("Delay (ms)", 0, 1000, 200, step=10,
                          key=f"cs_delay_{page_key}",
                          help="FTMO quotes up to ~200 ms simulated delay on "
                               "AU servers; raw ECN is usually < 50 ms.")
    delay_s = delay_ms / 1000.0

    entry_cost = np.zeros(n)          # price units, per trade
    exit_cost = np.zeros(n)
    tick_note = None
    is_tick_mode = mode.startswith("Tick")

    if not is_tick_mode:
        med_spd, p90_spd = _price_speed(d_sorted)
        b1, b2, b3 = st.columns([2, 1, 1])
        basis = b1.radio("Price speed basis",
                         ["Median", "90th percentile", "Custom"],
                         horizontal=True, key=f"cs_basis_{page_key}")
        b2.metric("Median speed", f"{med_spd / point_size:.2f} pts/s")
        b3.metric("90th pctile", f"{p90_spd / point_size:.2f} pts/s")
        if basis == "Custom":
            spd_pts = st.number_input("Custom speed (points/sec)",
                                      value=round(med_spd / point_size, 2),
                                      min_value=0.0, key=f"cs_spd_{page_key}")
            speed = spd_pts * point_size
        else:
            speed = med_spd if basis == "Median" else p90_spd
        d_cost = speed * delay_s
        entry_cost[:] = d_cost
        exit_cost[:] = d_cost
        st.caption("Speed comes from each trade's net displacement ÷ holding "
                   "time — conservative, since the tick path moves faster than "
                   "the displacement, and it can't see news bursts. Every "
                   "delayed fill is assumed adverse. Point tick replay at real "
                   "data for the honest answer.")
    else:
        tp1, tp2, tp3 = st.columns([3, 1, 1])
        path = tp1.text_input("Tick CSV path", key=f"cs_path_{page_key}",
                              placeholder=r"C:\QuantDataManager124\export\XAUUSD.csv",
                              help="QuantDataManager MT5 export, MT5 symbol "
                                   "export, Dukascopy — auto-detected. The "
                                   "file is binary-searched, never loaded, so "
                                   "20 GB+ is fine.")
        # Explorer's "Copy as path" wraps in quotes — strip them
        path = os.path.expandvars(os.path.expanduser(
            path.strip().strip('"').strip("'"))) if path else path
        tz_off = tp2.number_input("Tick − report (hrs)", value=0.0, step=0.5,
                                  key=f"cs_tz_{page_key}",
                                  help="Timezone offset: tick-file time minus "
                                       "report server time, in hours.")
        stale_s = tp3.number_input("Stale limit (s)", value=5.0, min_value=0.5,
                                   step=0.5, key=f"cs_stale_{page_key}",
                                   help="An event only matches if a tick "
                                        "exists within this many seconds "
                                        "before it.")
        if not path:
            st.info("Enter the tick CSV path to enable replay.")
        elif not os.path.isfile(path):
            import socket
            st.error(f"File not found: `{path}` — checked on "
                     f"**{socket.gethostname()}**, the machine running the "
                     "app. The tick file must be on (or reachable from) "
                     "that machine.")
        else:
            try:
                tf_cache = st.session_state.setdefault("cs_tickfiles", {})
                fkey = (path, os.path.getmtime(path))
                if fkey not in tf_cache:
                    tf_cache.clear()
                    tf_cache[fkey] = TickFile(path)
                tf = tf_cache[fkey]

                open_ep = _to_epoch(d_sorted["open_time"]) + tz_off * 3600
                close_ep = _to_epoch(d_sorted["close_time"]) + tz_off * 3600
                f_ts, l_ts = tf.first_ts, tf.last_ts
                _fmt = lambda e: pd.Timestamp(e, unit="s").strftime("%Y-%m-%d %H:%M")
                st.caption(f"Tick data: **{_fmt(f_ts)} → {_fmt(l_ts)}** · "
                           f"backtest events (offset applied): "
                           f"**{_fmt(open_ep.min())} → {_fmt(close_ep.max())}**")
                if close_ep.max() < f_ts or open_ep.min() > l_ts:
                    st.error("No overlap between tick data and backtest — "
                             "wrong file or timezone offset.")

                is_buy = _dir_sign(d_sorted["type"]) > 0
                ev_t = np.concatenate([open_ep, close_ep])
                # buys pay the ask on entry; sells pay it on exit
                ev_buy = np.concatenate([is_buy, ~is_buy])
                order = np.argsort(ev_t, kind="stable")

                scan_key = (page_key, fkey, round(tz_off, 2), round(stale_s, 1),
                            n, float(ev_t.min()), float(ev_t.max()))
                scans = st.session_state.setdefault("cs_scans", {})
                if st.button("Scan tick data", type="primary",
                             key=f"cs_scan_{page_key}"):
                    bar = st.progress(0.0, text="Scanning tick windows…")
                    windows = collect_event_windows(
                        tf, ev_t[order], pre_s=stale_s, post_s=1.5,
                        progress=lambda f: bar.progress(
                            f, text=f"Scanning tick windows… {f * 100:.0f}%"))
                    bar.empty()
                    scans.clear()
                    scans[scan_key] = windows

                if scan_key in scans:
                    costs_s, spreads_s = delay_costs(
                        scans[scan_key], ev_t[order], ev_buy[order],
                        delay_s, stale_s)
                    inv = np.empty(len(order), dtype=int)
                    inv[order] = np.arange(len(order))
                    costs = costs_s[inv]
                    spreads = spreads_s[inv]
                    unmatched = int(np.isnan(costs).sum())
                    entry_cost = np.nan_to_num(costs[:n])
                    exit_cost = np.nan_to_num(costs[n:])
                    med_sp = np.nanmedian(spreads)
                    tick_note = (
                        f"{2 * n - unmatched}/{2 * n} events matched"
                        + (f" · **{unmatched} unmatched** (tick gaps — "
                           f"costed at zero)" if unmatched else "")
                        + (f" · median live spread "
                           f"{med_sp / point_size:.1f} pts"
                           if np.isfinite(med_sp) else ""))
                    st.caption(f"✅ {tick_note}")
                else:
                    st.info("Press **Scan tick data** — one pass locates every "
                            "trade event in the file (a minute or two for a "
                            "10-year report); after that the delay slider "
                            "re-prices instantly.")
            except TickFormatError as e:
                st.error(f"Could not read tick file: {e}")

    # ── Apply costs ───────────────────────────────────────────────────────
    cost_pu = (entry_cost * ap_entry + exit_cost * ap_exit
               + slip_pts * point_size * n_sides)
    cost_per_lot = cost_pu * contract_val + comm
    stressed_per_lot = per_lot - cost_per_lot

    base_d = per_lot * lots_arr
    strs_d = stressed_per_lot * lots_arr

    st.markdown("##### Baseline vs stressed")
    mb, ms = _metrics(base_d, account), _metrics(strs_d, account)
    fmt = {"Net profit ($)": "{:,.0f}", "Profit factor": "{:.2f}",
           "Win rate (%)": "{:.1f}", "Avg trade ($)": "{:,.2f}",
           "Max DD (% acct)": "{:.2f}", "Return / DD": "{:.2f}"}
    rows = []
    for k in mb:
        delta = ms[k] - mb[k]
        rows.append({"Metric": k, "Baseline": fmt[k].format(mb[k]),
                     "Stressed": fmt[k].format(ms[k]),
                     "Δ": ("+" if delta >= 0 else "−") +
                          fmt[k].format(abs(delta)).lstrip("+-")})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    flipped = int(((base_d > 0) & (strs_d <= 0)).sum())
    tot_cost = float((cost_per_lot * lots_arr).sum())
    st.caption(f"Total cost **${tot_cost:,.0f}** at {lots_lbl} on "
               f"{account:,.0f} · **{flipped}** winner(s) flipped to losers.")

    # Equity overlay
    fig = go.Figure()
    dates = pd.to_datetime(d_sorted["close_time"])
    for arr, name_, col in [(base_d, "Baseline (ideal fills)", "#7c6af7"),
                            (strs_d, "Stressed", "#ef5350")]:
        fig.add_trace(go.Scatter(
            x=dates, y=np.cumsum(arr) / account * 100, name=name_,
            mode="lines", line=dict(color=col, width=1.8)))
    fig.add_hline(y=0, line_dash="dash", line_color="#555")
    fig.update_layout(height=360, margin=dict(l=0, r=0, t=30, b=0),
                      paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", font=dict(color="#ccc"),
                      legend=dict(orientation="h", y=1.1),
                      hovermode="x unified",
                      yaxis=dict(gridcolor="#2d3250", ticksuffix="%",
                                 title="Cumulative % of account"),
                      xaxis=dict(gridcolor="#2d3250"))
    st.plotly_chart(fig, width="stretch", key=f"cs_eq_{page_key}")

    # Delay-cost distribution — tick mode only (statistical is a constant)
    if is_tick_mode and tick_note:
        trade_delay_d = (np.nan_to_num(entry_cost * ap_entry)
                         + np.nan_to_num(exit_cost * ap_exit)) \
                        * contract_val * lots_arr
        figh = go.Figure(go.Histogram(x=trade_delay_d, nbinsx=60,
                                      marker_color="#ff9800", opacity=0.75))
        figh.add_vline(x=0, line_dash="dash", line_color="#555")
        figh.update_layout(
            title=f"Measured delay cost per trade at {delay_ms} ms "
                  f"($ at sizing) — negative = market moved your way",
            height=280, margin=dict(l=0, r=0, t=40, b=0),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#ccc"),
            xaxis=dict(gridcolor="#2d3250", title="$ per trade"),
            yaxis=dict(gridcolor="#2d3250", title="Trades"))
        st.plotly_chart(figh, width="stretch", key=f"cs_hist_{page_key}")

    # ── Slippage sensitivity sweep ────────────────────────────────────────
    st.markdown("##### Slippage sensitivity — where does the edge die?")
    sw1, _ = st.columns([1, 3])
    sweep_max = sw1.number_input("Sweep range (points/side)", value=20.0,
                                 min_value=1.0, step=5.0,
                                 key=f"cs_sweep_{page_key}")
    if n_sides == 0:
        st.info("Enable at least one side to sweep slippage.")
    else:
        # Delay + commission stay as configured; the x-axis sweeps slippage
        base_after_delay = per_lot - ((entry_cost * ap_entry +
                                       exit_cost * ap_exit) * contract_val
                                      + comm)
        xs = np.linspace(0, sweep_max, 61)
        nets, pfs = [], []
        for s_ in xs:
            d_ = (base_after_delay
                  - s_ * point_size * n_sides * contract_val) * lots_arr
            nets.append(d_.sum())
            l_ = d_[d_ < 0].sum()
            pfs.append(d_[d_ > 0].sum() / abs(l_) if l_ else float("inf"))
        nets = np.array(nets)
        step_cost = point_size * n_sides * contract_val * lots_arr.sum()
        be = nets[0] / step_cost if step_cost > 0 else float("inf")

        figs = make_subplots(specs=[[{"secondary_y": True}]])
        figs.add_trace(go.Scatter(x=xs, y=nets, name="Net profit ($)",
                                  line=dict(color="#4a90d9", width=2)))
        figs.add_trace(go.Scatter(x=xs, y=pfs, name="Profit factor",
                                  line=dict(color="#26a69a", width=2,
                                            dash="dot")), secondary_y=True)
        figs.add_hline(y=0, line_dash="dash", line_color="#ef5350")
        if 0 <= be <= sweep_max:
            figs.add_vline(x=be, line_dash="dash", line_color="#ffeb3b",
                           annotation_text=f"break-even {be:.1f} pts",
                           annotation_font_color="#ffeb3b")
        figs.update_layout(height=340, margin=dict(l=0, r=0, t=30, b=0),
                           paper_bgcolor="rgba(0,0,0,0)",
                           plot_bgcolor="rgba(0,0,0,0)",
                           font=dict(color="#ccc"),
                           legend=dict(orientation="h", y=1.12),
                           xaxis=dict(gridcolor="#2d3250",
                                      title="Slippage (points per side)"),
                           yaxis=dict(gridcolor="#2d3250", title="Net $"))
        figs.update_yaxes(title_text="PF", secondary_y=True,
                          gridcolor="rgba(0,0,0,0)")
        st.plotly_chart(figs, width="stretch", key=f"cs_sweep_fig_{page_key}")
        if be > sweep_max:
            st.caption(f"Break-even slippage ≈ **{be:.1f} pts/side** — beyond "
                       "the sweep range.")
        elif be >= 0:
            st.caption(f"The edge survives **{be:.1f} points per side** of "
                       "cost (with the configured delay and commission "
                       "already applied). Avg profit per trade is the whole "
                       "story here — scalpers die first.")

    # ── Worst-hit trades ──────────────────────────────────────────────────
    worst_cost = cost_per_lot * lots_arr
    if worst_cost.max() > 0:
        with st.expander("Worst-hit trades", expanded=False):
            idx = np.argsort(-worst_cost)[:15]
            st.dataframe(pd.DataFrame([{
                "Close time": str(pd.Timestamp(dates.iloc[i])),
                "Type": str(d_sorted["type"].iloc[i]),
                "Lots": f"{lots_arr[i]:g}",
                "P&L before ($)": f"{base_d[i]:,.2f}",
                "P&L after ($)": f"{strs_d[i]:,.2f}",
                "Cost ($)": f"{worst_cost[i]:,.2f}",
                "Delay cost (pts)": f"{(entry_cost[i] * ap_entry + exit_cost[i] * ap_exit) / point_size:,.1f}",
            } for i in idx]), width="stretch", hide_index=True)
