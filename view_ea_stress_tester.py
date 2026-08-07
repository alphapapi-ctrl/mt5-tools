"""
view_ea_stress_tester.py — EA Stress Tester page for MT5 Tools
Standalone assessment of a single EA backtest BEFORE it goes anywhere near a
portfolio or a prop account. Ported from the School Run App Analysis page's
robustness suite, adapted to MT5 report data with lot normalisation.

Tabs:
  🚩 Integrity   — red-flag checks (grid/martingale signatures, DD concealment)
  📋 Overview    — headline stats, equity/DD chart, monthly table
  📉 Loss Streaks — consecutive-loss distribution + recovery analysis
  🎲 Monte Carlo — block bootstrap DD distribution at your sizing
  🎯 Random Start — random-window forward sim vs a DD limit
  🔬 IS/OOS      — in/out-of-sample comparison + regime scanner

Sizing is Wim-first: fixed-lot backtests are normalised to PnL-per-lot, then
sized with the budget-DD% lot-step method. A manual-lots mode covers EAs from
other devs whose risk systems don't translate.
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import importlib, sys, os

from trade_stats import (drawdown_stats, ordered_profits, basic_stats,
                         consec_streaks, max_stagnation_days, equity_regression)
from view_prop_planner import _bootstrap_blocks, _is_index_symbol, _load_ea_baselines


def _get_parser():
    if "mt5_parser" in sys.modules:
        return importlib.reload(sys.modules["mt5_parser"])
    import mt5_parser
    return mt5_parser


# ─────────────────────────────────────────────────────────────────────────────
# Integrity checks — the "is this EA hiding something" panel
# ─────────────────────────────────────────────────────────────────────────────
def _max_concurrent(df: pd.DataFrame) -> dict:
    """Max simultaneously-open positions (and same-direction max) via event sweep."""
    d = df.dropna(subset=["open_time", "close_time"])
    if d.empty:
        return {"max_open": 0, "max_same_dir": 0}
    events = []
    for _, r in d.iterrows():
        dirn = str(r.get("type", "")).lower()
        events.append((r["open_time"], 1, dirn))
        events.append((r["close_time"], -1, dirn))
    events.sort(key=lambda e: (e[0], e[1]))
    cur = peak = 0
    cur_dir = {"buy": 0, "sell": 0}
    peak_dir = 0
    for _, delta, dirn in events:
        cur += delta
        peak = max(peak, cur)
        if dirn in cur_dir:
            cur_dir[dirn] += delta
            peak_dir = max(peak_dir, cur_dir[dirn])
    return {"max_open": peak, "max_same_dir": peak_dir}


def _integrity_checks(df: pd.DataFrame, summ: dict, subset: bool = False) -> list:
    """Run the red-flag battery. Returns [{check, value, status, note}]
    status: 'ok' | 'warn' | 'flag'. subset=True skips the report-level checks
    (floating-DD ratio, deal pairing) that only apply to the whole EA."""
    out = []
    profits = pd.to_numeric(df["net_profit"], errors="coerce").fillna(0)
    wins    = profits[profits > 0]
    losses  = profits[profits < 0]

    # 1. Equity DD vs balance DD (from the report header) — whole-EA only
    eq_dd, bal_dd = summ.get("equity_dd_max"), summ.get("balance_dd_max")
    if not subset and eq_dd and bal_dd and bal_dd > 0:
        ratio = eq_dd / bal_dd
        status = "ok" if ratio < 2 else "warn" if ratio < 3.5 else "flag"
        out.append({
            "check": "Floating vs realised DD",
            "value": f"{ratio:.1f}× (eq {eq_dd:,.2f} / bal {bal_dd:,.2f})",
            "status": status,
            "note": "Equity DD far above balance DD means losses ride in floating "
                    "positions before being realised — grid/martingale signature. "
                    "The trade list understates the real pain.",
        })

    # 2. Win rate vs payoff ratio (martingale profile)
    wr = (profits > 0).mean() * 100 if len(profits) else 0
    payoff = abs(wins.mean() / losses.mean()) if len(wins) and len(losses) and losses.mean() else 0
    if len(profits):
        _mart = wr >= 75 and payoff < 0.5
        status = "flag" if _mart else "warn" if (wr >= 85 or payoff < 0.35) else "ok"
        out.append({
            "check": "Win rate vs payoff",
            "value": f"WR {wr:.1f}% · avg win/avg loss {payoff:.2f}",
            "status": status,
            "note": "Very high win rate with small wins and big losses is the "
                    "loss-deferral profile: many tiny wins, rare catastrophic losses.",
        })

    # 3. Concurrent positions (grid / basket detection)
    conc = _max_concurrent(df)
    status = "ok" if conc["max_same_dir"] <= 2 else "warn" if conc["max_same_dir"] <= 4 else "flag"
    out.append({
        "check": "Concurrent positions",
        "value": f"max {conc['max_open']} open · {conc['max_same_dir']} same direction",
        "status": status,
        "note": "Many same-direction positions open together = averaging into "
                "losers (grid). Fine for hedged pairs, dangerous for baskets.",
    })

    # 4. Loser holding time
    if "duration_min" in df.columns and len(wins) and len(losses):
        d = pd.to_numeric(df["duration_min"], errors="coerce")
        w_dur = d[profits > 0].mean()
        l_dur = d[profits < 0].mean()
        if w_dur and w_dur > 0:
            dr = l_dur / w_dur
            status = "ok" if dr < 2 else "warn" if dr < 4 else "flag"
            out.append({
                "check": "Loser holding time",
                "value": f"losses held {dr:.1f}× longer ({l_dur:.0f}m vs {w_dur:.0f}m)",
                "status": status,
                "note": "Losers held much longer than winners = no real stop loss; "
                        "the EA waits for losers to come back.",
            })

    # 5. Profit concentration
    if len(profits) >= 50 and profits.sum() > 0:
        top10 = profits.nlargest(10).sum() / profits.sum() * 100
        status = "ok" if top10 < 40 else "warn" if top10 < 70 else "flag"
        out.append({
            "check": "Profit concentration",
            "value": f"top 10 trades = {top10:.0f}% of net profit",
            "status": status,
            "note": "Edge concentrated in a handful of trades is fragile — remove "
                    "a few lucky outliers and the backtest collapses.",
        })

    # 6. Worst-loss tail
    if len(losses) and len(wins):
        tail = abs(losses.min()) / wins.mean() if wins.mean() else 0
        status = "ok" if tail < 10 else "warn" if tail < 25 else "flag"
        out.append({
            "check": "Worst-loss tail",
            "value": f"worst loss = {tail:.0f}× the average win",
            "status": status,
            "note": "A single loss that erases dozens of average wins points to "
                    "unbounded risk per position.",
        })

    # 7. Fixed-lot backtest?
    vols = pd.to_numeric(df["volume"], errors="coerce").dropna()
    if len(vols):
        fixed = vols.nunique() == 1
        status = "ok" if fixed else "warn"
        out.append({
            "check": "Backtest sizing",
            "value": f"fixed {vols.iloc[0]:g} lots" if fixed
                     else f"variable ({vols.min():g}–{vols.max():g} lots)",
            "status": status,
            "note": "Fixed lots → per-lot normalisation is exact. Variable lots → "
                    "the EA's own money management is baked into the results; "
                    "per-lot rescaling and the report DD figure are approximations.",
        })

    # 8. Trade-list vs report net (pairing sanity) — whole-EA only
    rep_net = summ.get("total_net_profit")
    if not subset and rep_net:
        diff_pct = abs(profits.sum() - rep_net) / abs(rep_net) * 100
        status = "ok" if diff_pct < 1 else "warn" if diff_pct < 5 else "flag"
        out.append({
            "check": "Deal pairing",
            "value": f"trade list {profits.sum():,.2f} vs report {rep_net:,.2f} "
                     f"({diff_pct:.1f}% diff)",
            "status": status,
            "note": "A gap between the paired trade list and the report's own net "
                    "usually means partial closes / basket exits the FIFO pairing "
                    "can't fully reconstruct — common with grids.",
        })

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Page
# ─────────────────────────────────────────────────────────────────────────────
def render():
    st.title("🧪 EA Stress Tester")
    st.markdown(
        '<div class="info-card">Standalone assessment of a single EA backtest before it '
        'goes anywhere near a portfolio or prop account: integrity red-flags, Monte Carlo, '
        'random-start and regime analysis — all at your position sizing.</div>',
        unsafe_allow_html=True)

    parser = _get_parser()

    if "est_files" not in st.session_state:
        st.session_state.est_files = {}   # name -> {"df": df, "summary": dict}

    up_col, dir_col = st.columns([3, 2])
    with up_col:
        uploads = st.file_uploader("Upload MT5 backtest HTML report(s)",
                                   type=["htm", "html"], accept_multiple_files=True,
                                   key="est_uploads")
    with dir_col:
        folder = st.text_input("…or load all reports from a folder", value="",
                               key="est_folder", placeholder=r"C:\path\to\reports")

    def _ingest(name, raw):
        if name in st.session_state.est_files:
            return
        try:
            df, fmt = parser.detect_and_parse(raw, name)
        except Exception as e:
            st.error(f"Failed to parse **{name}**: {e}")
            return
        if df is None or len(df) == 0 or fmt != "MT5 Backtest Report":
            st.warning(f"**{name}**: not a Strategy Tester backtest report — skipped.")
            return
        summ = parser.parse_backtest_summary(raw) or {}
        st.session_state.est_files[name] = {"df": df, "summary": summ}

    for f in uploads or []:
        _ingest(os.path.splitext(f.name)[0], f.read())
    if folder and os.path.isdir(folder):
        import glob as _glob
        for p in sorted(_glob.glob(os.path.join(folder, "*.htm*"))):
            with open(p, "rb") as fh:
                _ingest(os.path.splitext(os.path.basename(p))[0], fh.read())

    if not st.session_state.est_files:
        st.info("Upload one or more MT5 Strategy Tester reports to begin.")
        return

    names = list(st.session_state.est_files.keys())
    sc1, sc2 = st.columns([3, 1])
    sel = sc1.selectbox("EA under test", names, key="est_sel")
    if sc2.button("Clear loaded reports", key="est_clear"):
        st.session_state.est_files = {}
        st.rerun()

    entry = st.session_state.est_files[sel]
    df, summ = entry["df"], entry["summary"]

    # ── Strategy scope — EAs like Gold Reaper tag sub-strategies in the
    #    comment field; assess the whole EA or a single strategy standalone
    all_strats = sorted(df["strategy"].dropna().unique().tolist()) \
        if "strategy" in df.columns else []
    _ALL = "All strategies (combined)"
    scope = _ALL
    if len(all_strats) > 1:
        scope = st.selectbox("Strategy scope", [_ALL] + all_strats,
                             key=f"est_scope_{sel}",
                             help="This report tags multiple strategies in the comment "
                                  "field. Pick one to assess it standalone — the report-"
                                  "level integrity checks (floating DD, deal pairing) "
                                  "only apply to the whole EA.")
        if scope != _ALL:
            df = df[df["strategy"] == scope].reset_index(drop=True)
    subset = scope != _ALL

    profits = pd.to_numeric(df["net_profit"], errors="coerce").fillna(0)
    vols    = pd.to_numeric(df["volume"], errors="coerce")
    sym     = str(df["symbol_base"].iloc[0]) if "symbol_base" in df.columns else ""
    st.caption(f"**{summ.get('expert', sel)}** · {sym} · {summ.get('period', '?')} · "
               f"{len(df)} trades{' · scope: ' + scope if subset else ''} · "
               f"report currency {summ.get('currency', 'USD')}")

    # ── Sizing (Wim-first) ────────────────────────────────────────────────
    with st.expander("⚙️ Position Sizing", expanded=True):
        sz_mode = st.radio("Sizing method",
                           ["Wim (max DD + risk %)", "Manual lots"],
                           horizontal=True, key="est_sz_mode",
                           help="Wim EAs: fixed-lot backtest + max DD drive the lot-step "
                                "method — the same calculation the EAs use internally. "
                                "For other devs whose risk systems don't translate, set "
                                "lots directly.")
        bt_vol = float(vols.mode().iloc[0]) if vols.notna().any() else 0.01
        step_default = 0.1 if _is_index_symbol(sym) else 0.01
        lots_map = None   # strategy -> lots, when sizing per-strategy

        if sz_mode.startswith("Wim"):
            multi = (not subset) and len(all_strats) > 1
            dd_basis = "combined"
            if multi:
                _basis = st.radio("Max DD basis",
                                  ["Combined — one DD for the whole EA",
                                   "Per-strategy — EA-style internal balancing"],
                                  horizontal=True, key="est_dd_basis",
                                  help="These EAs balance risk internally: each comment-"
                                       "tagged strategy is sized from its own backtest max "
                                       "DD and risk %. Per-strategy mode mimics that.")
                dd_basis = "per" if _basis.startswith("Per") else "combined"

            z1, z2, z3, z4 = st.columns(4)
            account  = z1.number_input("Account size", value=100000.0, step=1000.0,
                                       key="est_acct")
            lot_step = z4.selectbox("Lot step", [0.01, 0.1],
                                    index=1 if step_default == 0.1 else 0, key="est_step")

            if dd_basis == "per":
                budget_default = z3.number_input("Default risk %", value=2.0, min_value=0.1,
                                                 step=0.5, key="est_budget",
                                                 help="Pre-fills the Risk % column — edit "
                                                      "per strategy below.")
                z2.metric("Backtest volume", f"{bt_vol:g}")
                st.caption("Max DD prefers the EA's hard-coded baseline from "
                           "`ea_baselines.json` (the figure its internal risk sizing uses); "
                           "otherwise the strategy's balance DD computed from its own "
                           "trades — override with the dev's set-file figures if you have "
                           "them (the EA's numbers include floating DD).")
                _baselines = _load_ea_baselines()
                _rows = []
                for s_name, g in df.groupby("strategy"):
                    _bl = _baselines.get(s_name)
                    _dd_s = _bl if _bl else \
                        (abs(drawdown_stats(ordered_profits(g), 0.0)["max_dd"]) or 1.0)
                    _rows.append({"Strategy": s_name, "Trades": len(g),
                                  "Max DD": round(_dd_s, 2),
                                  "Source": "EA baseline" if _bl else "computed",
                                  "Risk %": budget_default})
                _ed = st.data_editor(
                    pd.DataFrame(_rows), hide_index=True, width="stretch",
                    column_config={
                        "Strategy": st.column_config.TextColumn(disabled=True),
                        "Trades":   st.column_config.NumberColumn(disabled=True, format="%d"),
                        "Max DD":   st.column_config.NumberColumn(
                            format="%.2f", help="At the backtest lot size, report currency"),
                        "Source":   st.column_config.TextColumn(disabled=True,
                                     help="EA baseline = from ea_baselines.json; "
                                          "computed = balance DD from the trade list"),
                        "Risk %":   st.column_config.NumberColumn(format="%.2f"),
                    },
                    key=f"est_ddtable_{sel}")
                lots_map = {}
                for _, r_ in _ed.iterrows():
                    _bps = (max(float(r_["Max DD"]), 0.01) / bt_vol * lot_step
                            / (max(float(r_["Risk %"]), 0.01) / 100))
                    lots_map[r_["Strategy"]] = max(1, int(account // _bps)) * lot_step
                st.caption("Lots per strategy → " +
                           " · ".join(f"**{k}**: {v:g}" for k, v in lots_map.items()))
                lots = float(np.mean(list(lots_map.values())))
            else:
                if subset:
                    _bl = _load_ea_baselines().get(scope)
                    _dd_default = _bl if _bl else \
                        (abs(drawdown_stats(ordered_profits(df), 0.0)["max_dd"]) or 100.0)
                    _dd_help = ("EA's hard-coded baseline from ea_baselines.json." if _bl
                                else "Defaults to this strategy's balance DD from its own "
                                     "trades — fill ea_baselines.json with the dev's "
                                     "set-file figure to use the EA's true number.")
                else:
                    _dd_default = float(summ.get("equity_dd_max") or 100.0)
                    _dd_help = "Equity Drawdown Maximal at the backtest lot size."
                max_dd = z2.number_input("Max DD (report ccy)", value=round(_dd_default, 2),
                                         min_value=0.01, step=1.0,
                                         key=f"est_dd_{sel}_{scope}", help=_dd_help)
                budget = z3.number_input("Risk %", value=2.0, min_value=0.1,
                                         step=0.5, key="est_budget")
                bal_per_step = (max_dd / bt_vol * lot_step) / (budget / 100)
                lots = max(1, int(account // bal_per_step)) * lot_step
                st.caption(f"Balance/step **{bal_per_step:,.0f}** → trading **{lots:g} lots** "
                           f"on {account:,.0f} (forced minimum if below balance-per-step).")
        else:
            z1, z2, z3, _ = st.columns(4)
            account = z1.number_input("Account size", value=100000.0, step=1000.0,
                                      key="est_acct")
            lots = z2.number_input("Lots", value=float(bt_vol), min_value=0.01,
                                   step=0.01, format="%.2f", key="est_lots")
            z3.metric("Backtest volume", f"{bt_vol:g}")

    # Per-trade % returns at the chosen sizing
    per_lot = (profits / vols.replace(0, np.nan)).fillna(0)
    d_sorted = df.assign(_per_lot=per_lot).sort_values("close_time").reset_index(drop=True)
    if lots_map:
        trade_lots = d_sorted["strategy"].map(lots_map) \
                        .fillna(min(lots_map.values())).values
        _lots_lbl = "per-strategy lots"
    else:
        trade_lots = lots
        _lots_lbl = f"{lots:g} lots"
    r_pct = (d_sorted["_per_lot"] * trade_lots / account * 100).values
    # R proxy: average losing trade = 1R (EAs rarely expose SL distance)
    _loss_mean = abs(d_sorted["_per_lot"][d_sorted["_per_lot"] < 0].mean())
    r_proxy = (d_sorted["_per_lot"] / _loss_mean).values if _loss_mean else r_pct
    dates = pd.to_datetime(d_sorted["close_time"])

    tab_flag, tab_ov, tab_streak, tab_mc, tab_rs, tab_oos = st.tabs([
        "🚩 Integrity", "📋 Overview", "📉 Loss Streaks",
        "🎲 Monte Carlo", "🎯 Random Start", "🔬 IS/OOS & Regime",
    ])

    # ══════════════════════════════════════════════════════════════════════
    # INTEGRITY
    # ══════════════════════════════════════════════════════════════════════
    with tab_flag:
        if subset:
            st.info(f"Scope: **{scope}** — report-level checks (floating DD, deal pairing) "
                    f"are skipped; they only apply to the whole EA.")
        checks = _integrity_checks(df, summ, subset=subset)
        n_flag = sum(1 for c in checks if c["status"] == "flag")
        n_warn = sum(1 for c in checks if c["status"] == "warn")
        if n_flag:
            st.error(f"**{n_flag} red flag(s), {n_warn} warning(s)** — treat this backtest "
                     f"with suspicion. Red-flagged behaviours typically hide risk that the "
                     f"trade list understates.")
        elif n_warn:
            st.warning(f"**No red flags, {n_warn} warning(s)** — mostly clean, review the "
                       f"warnings below.")
        else:
            st.success("**All integrity checks pass** — the trade list looks like an "
                       "honest representation of the strategy.")

        _icon = {"ok": "✅", "warn": "🟠", "flag": "🔴"}
        for c in checks:
            with st.container():
                cc1, cc2 = st.columns([1.5, 3])
                cc1.markdown(f"{_icon[c['status']]} **{c['check']}**")
                cc2.markdown(f"`{c['value']}`  \n<small style='color:#8899AA'>{c['note']}</small>",
                             unsafe_allow_html=True)
        st.caption("These checks catch the common ways EA backtests flatter themselves: "
                   "floating-loss concealment, loss-deferral (martingale/grid), outlier "
                   "dependence and broken deal pairing. Passing them doesn't prove an edge — "
                   "failing them almost always disproves one.")

    # ══════════════════════════════════════════════════════════════════════
    # OVERVIEW
    # ══════════════════════════════════════════════════════════════════════
    with tab_ov:
        bs = basic_stats(profits)
        dd = drawdown_stats(ordered_profits(df), float(summ.get("initial_deposit") or account))
        reg = equity_regression(df, float(summ.get("initial_deposit") or account))
        o1, o2, o3, o4, o5, o6 = st.columns(6)
        o1.metric("Trades", f"{bs['num_trades']:,}")
        o2.metric("Net (backtest lots)", f"{bs['net_profit']:,.2f}")
        o3.metric("Win Rate", f"{bs['win_rate']:.1f}%")
        _pf = bs["profit_factor"]
        o4.metric("Profit Factor", "∞" if _pf == float("inf") else f"{_pf:.2f}")
        o5.metric("Balance DD (list)", f"{dd['max_dd']:,.2f}")
        o6.metric("Report Equity DD", f"{summ.get('equity_dd_max', 0):,.2f}",
                  help="From the report header — includes floating DD the trade list can't see.")

        o7, o8, o9, o10, o11, o12 = st.columns(6)
        o7.metric("Avg Win", f"{bs['avg_win']:,.2f}")
        o8.metric("Avg Loss", f"{bs['avg_loss']:,.2f}")
        cs = consec_streaks(ordered_profits(df))
        o9.metric("Max Consec Losses", cs["max_consec_losses"])
        o10.metric("Stagnation", f"{max_stagnation_days(df, account)}d")
        o11.metric("Stability", f"{reg['stability']}")
        o12.metric("Growth Quality", f"{reg['growth_quality']}")

        # Equity + DD chart at chosen sizing
        cum = np.cumsum(r_pct)
        peak = np.maximum.accumulate(cum)
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            row_heights=[0.7, 0.3], vertical_spacing=0.04)
        fig.add_trace(go.Scatter(x=dates, y=cum, name="Equity",
                                 line=dict(color="#7c6af7", width=1.8)), row=1, col=1)
        fig.add_trace(go.Scatter(x=dates, y=cum - peak, name="Drawdown",
                                 fill="tozeroy", fillcolor="rgba(239,83,80,0.25)",
                                 line=dict(color="#ef5350", width=1)), row=2, col=1)
        fig.update_layout(height=460, margin=dict(l=0, r=0, t=30, b=0),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          font=dict(color="#ccc"), showlegend=False,
                          title=f"Equity at {_lots_lbl} on {account:,.0f} (% of account)")
        fig.update_xaxes(gridcolor="#2d3250")
        fig.update_yaxes(gridcolor="#2d3250", ticksuffix="%")
        st.plotly_chart(fig, width="stretch", key="est_equity")

        _real_dd = (cum - peak).min()
        _cap = f"Worst balance DD at this sizing: **{_real_dd:.2f}%** of account."
        if not subset and not lots_map:
            _cap += (f" Scaled report equity DD: "
                     f"**{-(summ.get('equity_dd_max', 0) / bt_vol * lots / account * 100):.2f}%**.")
        st.caption(_cap)

    # ══════════════════════════════════════════════════════════════════════
    # LOSS STREAKS
    # ══════════════════════════════════════════════════════════════════════
    with tab_streak:
        seq = r_proxy
        streaks, cur = [], 0
        for v in seq:
            if v <= 0:
                cur += 1
            else:
                if cur:
                    streaks.append(cur)
                cur = 0
        if cur:
            streaks.append(cur)

        if streaks:
            sr = pd.Series(streaks)
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("Max Loss Streak", int(sr.max()))
            s2.metric("Avg Loss Streak", f"{sr.mean():.1f}")
            s3.metric("Streaks ≥ 5", int((sr >= 5).sum()))
            s4.metric("Streaks ≥ 8", int((sr >= 8).sum()))

            dist = sr.value_counts().sort_index()
            fig_s = go.Figure(go.Bar(
                x=dist.index.astype(str), y=dist.values,
                marker_color=["#ef5350" if k >= 5 else "#ff9800" if k >= 3 else "#4a90d9"
                              for k in dist.index],
                text=dist.values, textposition="outside"))
            fig_s.update_layout(title="Loss Streak Distribution",
                                height=300, margin=dict(l=0, r=0, t=40, b=0),
                                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                                font=dict(color="#ccc"),
                                xaxis=dict(gridcolor="#2d3250", title="Streak Length"),
                                yaxis=dict(gridcolor="#2d3250", title="Occurrences"))
            st.plotly_chart(fig_s, width="stretch", key="est_streaks")

            # Recovery analysis (R proxy: |avg loss| = 1R)
            recov, i = [], 0
            while i < len(seq):
                if seq[i] <= 0:
                    start, lost = i, 0.0
                    while i < len(seq) and seq[i] <= 0:
                        lost += abs(seq[i]); i += 1
                    if i - start >= 3:
                        rec, n_r, j = 0.0, 0, i
                        while j < len(seq) and rec < lost:
                            rec += seq[j]; n_r += 1; j += 1
                        recov.append({"Streak Length": i - start,
                                      "R Lost": round(lost, 2),
                                      "Trades to Recover": str(n_r) if rec >= lost else "—",
                                      "Recovered": "✓" if rec >= lost else "✗"})
                else:
                    i += 1
            if recov:
                with st.expander(f"Recovery after streaks ≥ 3 losses ({len(recov)})"):
                    st.caption("R proxy: the average losing trade = 1R.")
                    st.dataframe(pd.DataFrame(recov), width="stretch", hide_index=True)
        else:
            st.info("No consecutive losses found.")

    # ══════════════════════════════════════════════════════════════════════
    # MONTE CARLO
    # ══════════════════════════════════════════════════════════════════════
    with tab_mc:
        st.markdown("*Block bootstrap — sample blocks with replacement, preserving "
                    "intra-block streak structure. Returns in % of account at your sizing.*")
        st.caption("⚠️ Built from closed trades. If the Integrity tab flags floating-vs-"
                   "realised DD, the true equity drawdown is a multiple of what this shows — "
                   "grids realise losses only when the basket closes.")
        m1, m2 = st.columns(2)
        mc_sims  = m1.slider("Simulations", 100, 5000, 1000, step=100, key="est_mc_sims")
        mc_block = m2.radio("Block size", ["Per trade", "Weekly", "Monthly"],
                            horizontal=True, key="est_mc_block")

        if st.button("Run Monte Carlo", key="est_mc_run", type="primary"):
            n = len(r_pct)
            if n < 10:
                st.warning("Need at least 10 trades.")
            else:
                blocks_ix = _bootstrap_blocks(d_sorted, mc_block)
                blocks = [r_pct[ix] for ix in (np.array(b) for b in blocks_ix)]
                nb = len(blocks)
                if nb < 3:
                    st.warning(f"Only {nb} complete blocks — need at least 3.")
                else:
                    rng = np.random.default_rng(42)
                    dds, finals, curves = np.zeros(mc_sims), np.zeros(mc_sims), []
                    for s in range(mc_sims):
                        pieces, total = [], 0
                        while total < n:
                            b = blocks[rng.integers(0, nb)]
                            pieces.append(b); total += len(b)
                        path = np.concatenate(pieces)[:n]
                        c = np.cumsum(path)
                        dds[s] = (c - np.maximum.accumulate(c)).min()
                        finals[s] = c[-1]
                        if s < 200:
                            curves.append(c)

                    pcts = [50, 75, 90, 95, 99]
                    vals = np.percentile(dds, [100 - p for p in pcts])
                    cols = st.columns(5)
                    for c_, p_, v_ in zip(cols, pcts, vals):
                        c_.metric(f"{p_}th %ile DD" if p_ != 50 else "Median DD", f"{v_:.2f}%")
                    st.caption(f"{nb} blocks · account {account:,.0f} · {_lots_lbl}")

                    fig_mc = go.Figure()
                    for c in curves:
                        fig_mc.add_trace(go.Scatter(x=list(range(len(c))), y=c, mode="lines",
                                                    line=dict(color="#4a90d9", width=0.3),
                                                    opacity=0.15, showlegend=False))
                    orig = np.cumsum(r_pct)
                    fig_mc.add_trace(go.Scatter(x=list(range(n)), y=orig, mode="lines",
                                                line=dict(color="#ff9800", width=2),
                                                name="Original sequence"))
                    arr = np.array(curves)
                    for p, col, lbl in [(5, "#ef5350", "5th"), (50, "#26a69a", "Median"),
                                        (95, "#4a90d9", "95th")]:
                        fig_mc.add_trace(go.Scatter(
                            x=list(range(arr.shape[1])), y=np.percentile(arr, p, axis=0),
                            mode="lines", line=dict(color=col, width=2, dash="dash"),
                            name=f"{lbl} percentile"))
                    fig_mc.add_hline(y=0, line_dash="dash", line_color="#555")
                    fig_mc.update_layout(
                        title=f"Monte Carlo — {mc_sims} paths",
                        height=400, margin=dict(l=0, r=0, t=40, b=0),
                        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                        font=dict(color="#ccc"), legend=dict(orientation="h", y=-0.12),
                        xaxis=dict(gridcolor="#2d3250", title="Trade #"),
                        yaxis=dict(gridcolor="#2d3250", title="Cumulative % of account"))
                    st.plotly_chart(fig_mc, width="stretch", key="est_mc_fan")

                    fig_dd = go.Figure(go.Histogram(x=dds, nbinsx=50,
                                                    marker_color="#ef5350", opacity=0.7))
                    for v_, p_ in zip(vals, pcts):
                        fig_dd.add_vline(x=v_, line_dash="dash", line_color="#ffeb3b",
                                         annotation_text=f"{p_}%ile: {v_:.1f}%",
                                         annotation_font_color="#ffeb3b",
                                         annotation_position="top right")
                    fig_dd.update_layout(title="Max Drawdown Distribution (% of account)",
                                         height=300, margin=dict(l=0, r=0, t=40, b=0),
                                         paper_bgcolor="rgba(0,0,0,0)",
                                         plot_bgcolor="rgba(0,0,0,0)", font=dict(color="#ccc"),
                                         xaxis=dict(gridcolor="#2d3250", title="Max DD %"),
                                         yaxis=dict(gridcolor="#2d3250", title="Frequency"))
                    st.plotly_chart(fig_dd, width="stretch", key="est_mc_dd")

    # ══════════════════════════════════════════════════════════════════════
    # RANDOM START
    # ══════════════════════════════════════════════════════════════════════
    with tab_rs:
        st.markdown("*Pick random start points and play forward — what would a trader who "
                    "joined at a random moment have experienced?*")
        n = len(r_pct)
        if n < 30:
            st.info("Need at least 30 trades.")
        else:
            r1, r2, r3 = st.columns(3)
            rs_sims   = r1.slider("Simulations", 100, 5000, 1000, step=100, key="est_rs_sims")
            rs_window = r2.slider("Trades forward", 20, min(n, 1000), min(200, n),
                                  step=10, key="est_rs_win")
            rs_limit  = r3.number_input("DD limit %", value=5.0, step=0.5, key="est_rs_lim")

            if st.button("Run Random Start", key="est_rs_run", type="primary"):
                rng = np.random.default_rng(42)
                max_dds  = np.zeros(rs_sims)
                fins     = np.zeros(rs_sims)
                breach_n = 0
                curves, starts = [], []
                for s in range(rs_sims):
                    ix = rng.integers(0, n - rs_window + 1)
                    c = np.cumsum(r_pct[ix:ix + rs_window])
                    mdd = (c - np.maximum.accumulate(c)).min()
                    max_dds[s], fins[s] = mdd, c[-1]
                    if abs(mdd) >= rs_limit:
                        breach_n += 1
                    if s < 200:
                        curves.append(c)
                        starts.append(dates.iloc[ix])

                dpcts = np.percentile(max_dds, [50, 25, 10, 5, 1])
                a = st.columns(5)
                for c_, lbl, v_ in zip(a, ["Median", "75th %ile", "90th %ile",
                                           "95th %ile", "99th %ile"], dpcts):
                    c_.metric(f"{lbl} DD", f"{v_:.2f}%")
                b = st.columns(5)
                fpcts = np.percentile(fins, [5, 25, 50, 75, 95])
                b[0].metric("Breach Rate", f"{breach_n / rs_sims * 100:.1f}%",
                            delta=f"{breach_n}/{rs_sims}", delta_color="inverse")
                b[1].metric("Median Return", f"{fpcts[2]:+.2f}%")
                b[2].metric("5th %ile Return", f"{fpcts[0]:+.2f}%")
                b[3].metric("95th %ile Return", f"{fpcts[4]:+.2f}%")
                b[4].metric("Worst Return", f"{fins.min():+.2f}%")

                fig_rs = go.Figure()
                for c in curves:
                    fig_rs.add_trace(go.Scatter(x=list(range(len(c))), y=c, mode="lines",
                                                line=dict(color="#4a90d9", width=0.3),
                                                opacity=0.15, showlegend=False,
                                                hoverinfo="skip"))
                arr = np.array(curves)
                for p, col, lbl in [(5, "#ef5350", "5th"), (50, "#26a69a", "Median"),
                                    (95, "#4a90d9", "95th")]:
                    fig_rs.add_trace(go.Scatter(
                        x=list(range(arr.shape[1])), y=np.percentile(arr, p, axis=0),
                        mode="lines", line=dict(color=col, width=2, dash="dash"),
                        name=f"{lbl} percentile"))
                fig_rs.add_hline(y=0, line_dash="dash", line_color="#555")
                fig_rs.add_hline(y=-rs_limit, line_dash="dash", line_color="#ef5350",
                                 line_width=2, annotation_text=f"DD limit -{rs_limit:.1f}%",
                                 annotation_font_color="#ef5350")
                fig_rs.update_layout(
                    title=f"{rs_sims} paths — {rs_window} trades from random start",
                    height=400, margin=dict(l=0, r=0, t=40, b=0),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color="#ccc"), legend=dict(orientation="h", y=-0.12),
                    xaxis=dict(gridcolor="#2d3250", title="Trade # from start"),
                    yaxis=dict(gridcolor="#2d3250", title="Cumulative % of account"))
                st.plotly_chart(fig_rs, width="stretch", key="est_rs_fan")

                with st.expander("Worst start dates", expanded=False):
                    worst = np.argsort(max_dds[:len(starts)])[:20]
                    st.dataframe(pd.DataFrame([{
                        "Start Date": str(starts[i].date()),
                        "Max DD %": f"{max_dds[i]:.2f}%",
                        "Final Return %": f"{fins[i]:+.2f}%",
                        "Breached": "Yes" if abs(max_dds[i]) >= rs_limit else "No",
                    } for i in worst]), width="stretch", hide_index=True)

    # ══════════════════════════════════════════════════════════════════════
    # IS/OOS & REGIME
    # ══════════════════════════════════════════════════════════════════════
    with tab_oos:
        st.markdown("*Split the history into training/test periods. A genuine edge looks "
                    "the same on both sides of any split.*")
        if len(dates) < 40:
            st.info("Need at least 40 trades.")
            return

        _min_d, _max_d = dates.min().date(), dates.max().date()
        _def = dates.iloc[len(dates) * 2 // 3].date()
        split = st.slider("OOS start date", min_value=_min_d, max_value=_max_d,
                          value=_def, key="est_oos_split")
        split_ts = pd.Timestamp(split)
        is_mask  = dates < split_ts
        oos_mask = ~is_mask
        is_r, oos_r = r_proxy[is_mask.values], r_proxy[oos_mask.values]

        def _pstats(label, r, dts):
            if len(r) == 0:
                return {"Period": label, "Trades": 0}
            yrs = max((dts.max() - dts.min()).days / 365.25, 0.1)
            wins = r[r > 0]
            c = np.cumsum(r)
            return {
                "Period": label, "Trades": len(r),
                "Win Rate": f"{(r > 0).mean() * 100:.1f}%",
                "Expectancy": f"{r.mean():.3f}R",
                "Total R": f"{r.sum():+.1f}R",
                "Avg Win": f"{wins.mean():.2f}R" if len(wins) else "—",
                "Avg Loss": f"{r[r <= 0].mean():.2f}R" if (r <= 0).any() else "—",
                "Max DD R": f"{(c - np.maximum.accumulate(c)).min():.1f}R",
                "Trades/Year": f"{len(r) / yrs:.0f}",
            }

        st.dataframe(pd.DataFrame([
            _pstats(f"In-Sample (→ {split})", is_r, dates[is_mask]),
            _pstats(f"Out-of-Sample ({split} →)", oos_r, dates[oos_mask]),
            _pstats("Combined", r_proxy, dates),
        ]), width="stretch", hide_index=True)
        st.caption("R proxy: the average losing trade = 1R (MT5 reports don't expose SL "
                   "distance).")

        if len(is_r) >= 10 and len(oos_r) >= 10:
            # Distribution comparison + diagnosis
            mean_diff = abs(is_r.mean() - oos_r.mean())
            wr_diff = abs((is_r > 0).mean() - (oos_r > 0).mean()) * 100
            if mean_diff < 0.05 and wr_diff < 5:
                st.success(f"**Consistent** — expectancy differs by {mean_diff:.3f}R, win "
                           f"rate by {wr_diff:.1f}pp across the split. The edge looks "
                           f"genuine rather than curve-fitted.")
            elif mean_diff < 0.15 and wr_diff < 10:
                st.warning(f"**Moderate divergence** — expectancy differs by {mean_diff:.3f}R, "
                           f"win rate by {wr_diff:.1f}pp. Move the split around; if OOS is "
                           f"consistently worse, parameters may be over-fitted.")
            else:
                st.error(f"**Significant divergence** — expectancy differs by {mean_diff:.3f}R, "
                         f"win rate by {wr_diff:.1f}pp. Strong overfitting / regime-change "
                         f"signal — do not trust the combined backtest numbers.")

            fig_oos = go.Figure()
            fig_oos.add_trace(go.Scatter(x=dates[is_mask], y=np.cumsum(r_pct[is_mask.values]),
                                         name="In-Sample", line=dict(color="#4a90d9", width=2)))
            _oos_cum = np.cumsum(r_pct[oos_mask.values])
            fig_oos.add_trace(go.Scatter(x=dates[oos_mask], y=_oos_cum,
                                         name="Out-of-Sample",
                                         line=dict(color="#ff9800", width=2)))
            fig_oos.add_hline(y=0, line_dash="dash", line_color="#555")
            fig_oos.update_layout(height=300, margin=dict(l=0, r=0, t=30, b=0),
                                  paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                                  font=dict(color="#ccc"), legend=dict(orientation="h", y=1.1),
                                  xaxis=dict(gridcolor="#2d3250"),
                                  yaxis=dict(gridcolor="#2d3250", title="Cumulative %",
                                             ticksuffix="%"))
            st.plotly_chart(fig_oos, width="stretch", key="est_oos_curve")

        # ── Regime scanner
        st.markdown("---")
        st.markdown("#### Regime Scanner — divergence across every split point")
        st.caption("Sweeps the IS/OOS split across the whole history. Flat near zero = "
                   "consistent behaviour wherever you cut; spikes reveal regime changes "
                   "(vol shifts, structural breaks) or a decaying edge.")
        if st.button("Run Regime Scanner", key="est_regime_run", type="primary"):
            uniq_days = pd.Series(dates.dt.normalize().unique()).sort_values().reset_index(drop=True)
            min_side = 20
            rows_rg = []
            for sp in uniq_days:
                m = dates < sp
                a, b_ = r_proxy[m.values], r_proxy[~m.values]
                if len(a) < min_side or len(b_) < min_side:
                    continue
                rows_rg.append({"split": sp,
                                "mean_diff": b_.mean() - a.mean(),
                                "wr_diff": ((b_ > 0).mean() - (a > 0).mean()) * 100,
                                "is_mean": a.mean(), "oos_mean": b_.mean()})
            if not rows_rg:
                st.warning("Not enough trades on both sides of any split.")
            else:
                rg = pd.DataFrame(rows_rg)
                fig_rg = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                       vertical_spacing=0.08,
                                       subplot_titles=["Expectancy divergence (OOS − IS, R)",
                                                       "Win-rate divergence (OOS − IS, pp)"])
                cols_m = ["#26a69a" if abs(v) < 0.05 else "#ff9800" if abs(v) < 0.15
                          else "#ef5350" for v in rg["mean_diff"]]
                fig_rg.add_trace(go.Bar(x=rg["split"], y=rg["mean_diff"],
                                        marker_color=cols_m, showlegend=False), row=1, col=1)
                cols_w = ["#26a69a" if abs(v) < 5 else "#ff9800" if abs(v) < 10
                          else "#ef5350" for v in rg["wr_diff"]]
                fig_rg.add_trace(go.Bar(x=rg["split"], y=rg["wr_diff"],
                                        marker_color=cols_w, showlegend=False), row=2, col=1)
                for rr in (1, 2):
                    fig_rg.add_hline(y=0, line_dash="dash", line_color="#555", row=rr, col=1)
                fig_rg.update_layout(height=480, margin=dict(l=0, r=0, t=40, b=0),
                                     paper_bgcolor="rgba(0,0,0,0)",
                                     plot_bgcolor="rgba(0,0,0,0)", font=dict(color="#ccc"))
                fig_rg.update_xaxes(gridcolor="#2d3250")
                fig_rg.update_yaxes(gridcolor="#2d3250")
                st.plotly_chart(fig_rg, width="stretch", key="est_regime")

                _worst = rg.loc[rg["mean_diff"].abs().idxmax()]
                st.caption(f"Largest divergence at split **{pd.Timestamp(_worst['split']).date()}** "
                           f"(OOS − IS = {_worst['mean_diff']:+.3f}R). If that date lines up "
                           f"with a known market event, the edge is regime-dependent; if not, "
                           f"suspect overfitting to one era.")
