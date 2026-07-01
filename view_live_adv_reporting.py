"""
view_live_adv_reporting.py
==========================
Advanced portfolio reporting across Live MT5 EA accounts.
Uses the same FTP cache / account config as view_live_mt5_eas.
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from datetime import date, datetime, timedelta
from pathlib import Path
import json, pickle, io

ACCOUNTS_FILE = Path("ftp_accounts.json")
CACHE_DIR     = Path("cache")


# ── Data loading (shared with live EAs page) ─────────────────────────────────

def _load_account_configs() -> list:
    if ACCOUNTS_FILE.exists():
        return json.loads(ACCOUNTS_FILE.read_text())
    return []


def _load_cache(account_folder: str) -> dict | None:
    p = CACHE_DIR / f"ftp_{account_folder}.pkl"
    if not p.exists():
        return None
    try:
        return pickle.loads(p.read_bytes())
    except Exception:
        return None


def _build_combined_df(acc_cfgs: list) -> tuple[pd.DataFrame, list[dict]]:
    """Load all cached account data, return (combined_df, list_of_data_dicts)."""
    all_data = []
    for acfg in acc_cfgs:
        data = _load_cache(acfg["account"])
        if data:
            data["balance"] = acfg["balance"]
            data["label"]   = acfg["label"]
            data["type"]    = acfg.get("type", "Demo")
            all_data.append(data)

    dfs = []
    for d in all_data:
        df = d["df"].copy()
        if df.empty or "close_time" not in df.columns:
            continue
        df["_account"] = d["label"]
        df["_balance"] = d["balance"]
        dfs.append(df)

    if dfs:
        df_all = pd.concat(dfs, ignore_index=True)
        df_all["close_time"] = pd.to_datetime(df_all["close_time"], errors="coerce")
        df_all["open_time"]  = pd.to_datetime(df_all["open_time"],  errors="coerce")
        df_all["net_profit"] = pd.to_numeric(df_all["net_profit"], errors="coerce").fillna(0)
        df_all = df_all.dropna(subset=["close_time"]).sort_values("close_time").reset_index(drop=True)
        # Clean comment field: replace MT5 system comments (sl/tp/so values) with NaN
        # so they aren't treated as algo names
        import re
        _sys_comment = re.compile(r'^\[?(sl|tp|so)\s+[\d\.]+%?\]?$', re.IGNORECASE)
        df_all["comment"] = df_all["comment"].apply(
            lambda x: np.nan if (pd.isna(x) or not str(x).strip()
                                 or _sys_comment.match(str(x).strip())) else str(x).strip()
        )
    else:
        df_all = pd.DataFrame()

    return df_all, all_data


LAYOUT = dict(
    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    font=dict(family="sans-serif"),
    margin=dict(l=60, r=20, t=40, b=40),
    xaxis=dict(gridcolor="rgba(128,128,128,0.15)"),
    yaxis=dict(gridcolor="rgba(128,128,128,0.15)"),
)


# ── Render ────────────────────────────────────────────────────────────────────

def render():
    st.title("📊 Live EA's Adv Reporting")

    acc_cfgs = _load_account_configs()
    if not acc_cfgs:
        st.info("No accounts configured. Set up accounts in the **Live MT5 EAs** page first.")
        return

    df_all, all_data = _build_combined_df(acc_cfgs)
    if df_all.empty:
        st.info("No cached trade data. Refresh accounts in the **Live MT5 EAs** page first.")
        return

    # ── Filters ──────────────────────────────────────────────────────────────
    with st.expander("🔍 Filters", expanded=False):
        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            all_labels = sorted(df_all["_account"].unique())
            sel_accounts = st.multiselect("Accounts", all_labels, default=all_labels,
                                          key="adv_accounts")
        with fc2:
            valid_times = df_all["open_time"].dropna()
            d_min = valid_times.min().date()
            d_max = valid_times.max().date()
            date_from = st.date_input("From", value=d_min, min_value=d_min,
                                      max_value=d_max, key="adv_from")
            date_to = st.date_input("To", value=d_max, min_value=d_min,
                                    max_value=d_max, key="adv_to")
        with fc3:
            syms = sorted(df_all["symbol"].dropna().unique())
            sel_syms = st.multiselect("Symbols", syms, key="adv_syms")

    df = df_all.copy()
    if sel_accounts:
        df = df[df["_account"].isin(sel_accounts)]
    df = df[(df["open_time"].dt.date >= date_from) & (df["open_time"].dt.date <= date_to)]
    if sel_syms:
        df = df[df["symbol"].isin(sel_syms)]
    df = df.reset_index(drop=True)

    if df.empty:
        st.info("No trades match the current filters.")
        return

    total_balance = sum(
        d["balance"] for d in all_data
        if d["label"] in (sel_accounts if sel_accounts else all_labels)
    )

    st.caption(f"Analysing **{len(df):,}** trades across **{df['_account'].nunique()}** accounts  ·  "
               f"Combined balance: **${total_balance:,.0f}**")

    # ── Section tabs ─────────────────────────────────────────────────────────
    tabs = st.tabs([
        "Portfolio Overview",
        "Algo Scorecard",
        "Correlation Analysis",
        "Symbol Exposure",
        "Weak Algo Detection",
        "Algo Consistency",
        "Generate AI Report",
    ])

    with tabs[0]:
        _render_portfolio_overview(df, total_balance, all_data, sel_accounts, df_all, date_from)
    with tabs[1]:
        _render_algo_scorecard(df, total_balance)
    with tabs[2]:
        _render_correlation(df)
    with tabs[3]:
        _render_symbol_exposure(df, total_balance)
    with tabs[4]:
        _render_weak_algos(df, total_balance)
    with tabs[5]:
        _render_algo_consistency(df, total_balance)
    with tabs[6]:
        _render_ai_report(df, total_balance, all_data, sel_accounts)


# ── 1. Portfolio Overview ─────────────────────────────────────────────────────

def _render_portfolio_overview(df, total_balance, all_data, sel_accounts, df_all=None, date_from=None):
    from mt5_parser import calc_stats

    st.subheader("Portfolio Overview")

    # Compute period-start balances (initial deposit + P&L before filter start)
    period_balances = {}
    for d in all_data:
        acc = d["label"]
        if sel_accounts and acc not in sel_accounts:
            continue
        base_bal = d["balance"]
        if df_all is not None and date_from is not None:
            prior = df_all[(df_all["_account"] == acc) &
                           (df_all["open_time"].dt.date < date_from)]
            base_bal += prior["net_profit"].sum()
        period_balances[acc] = base_bal
    period_total_balance = sum(period_balances.values())

    stats = calc_stats(df, deposit=period_total_balance or total_balance)
    if not stats:
        return

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Net Profit", f"${stats['net_profit']:,.2f}")
    c2.metric("Win Rate", f"{stats['win_rate']}%")
    c3.metric("Profit Factor", f"{stats['profit_factor']}")
    c4.metric("Expectancy", f"${stats['expectancy']:,.2f}")
    dd_pct = stats.get('max_drawdown_pct', 0)
    c5.metric("Max Drawdown", f"${stats['max_drawdown']:,.2f} ({abs(dd_pct):.1f}%)")
    sharpe = _calc_sharpe(df)
    c6.metric("Sharpe Ratio", f"{sharpe:.2f}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Trades", f"{stats['total_trades']:,}")
    c2.metric("Trading Days", stats['trading_days'])
    c3.metric("Trades/Day", stats['trades_per_day'])
    n_algos = df["comment"].dropna().nunique()
    c4.metric("Active Algos", n_algos)

    st.markdown("---")

    eq_mode = st.radio("Equity display", ["% Return", "$ Value"],
                       horizontal=True, key="adv_eq_mode")
    show_pct = eq_mode == "% Return"

    st.markdown("**Combined Equity Curve**")
    df_s = df.sort_values("close_time").copy()
    df_s["_cum"] = df_s["net_profit"].cumsum()
    if show_pct:
        denom = period_total_balance or total_balance
        df_s["_y"] = df_s["_cum"] / denom * 100 if denom else 0
        y_fmt = dict(ticksuffix="%", **LAYOUT["yaxis"])
    else:
        df_s["_y"] = df_s["_cum"]
        y_fmt = dict(tickprefix="$", **LAYOUT["yaxis"])
    fig_eq = go.Figure(go.Scatter(
        x=df_s["close_time"], y=df_s["_y"], mode="lines",
        line=dict(color="#7c6af7", width=2, shape="spline", smoothing=0.6),
        fill="tozeroy", fillcolor="rgba(124,106,247,0.08)"))
    fig_eq.update_layout(height=300, hovermode="x unified",
                         yaxis=y_fmt,
                         **{k: v for k, v in LAYOUT.items() if k != "yaxis"})
    st.plotly_chart(fig_eq, use_container_width=True, key="adv_eq")

    st.markdown("**Per-Account Equity Curves**")
    fig_acc = go.Figure()
    for acc in sorted(df["_account"].unique()):
        sub = df[df["_account"] == acc].sort_values("close_time").copy()
        sub["_cum"] = sub["net_profit"].cumsum()
        bal = period_balances.get(acc, sub["_balance"].iloc[0] if not sub.empty else 0)
        if show_pct:
            sub["_y"] = sub["_cum"] / bal * 100 if bal else 0
        else:
            sub["_y"] = sub["_cum"]
        fig_acc.add_trace(go.Scatter(
            x=sub["close_time"], y=sub["_y"], mode="lines", name=acc,
            line=dict(width=2, shape="spline", smoothing=0.6)))
    if show_pct:
        acc_y_fmt = dict(ticksuffix="%", **LAYOUT["yaxis"])
    else:
        acc_y_fmt = dict(tickprefix="$", **LAYOUT["yaxis"])
    fig_acc.update_layout(height=300, hovermode="x unified",
                          yaxis=acc_y_fmt,
                          legend=dict(bgcolor="rgba(0,0,0,0)"),
                          **{k: v for k, v in LAYOUT.items() if k not in ("yaxis",)})
    st.plotly_chart(fig_acc, use_container_width=True, key="adv_acc_eq")

    st.markdown("**Account Comparison**")
    rows = []
    for acc in sorted(df["_account"].unique()):
        sub = df[df["_account"] == acc]
        bal = sub["_balance"].iloc[0] if not sub.empty else 0
        s = calc_stats(sub, deposit=bal)
        if not s:
            continue
        rows.append({
            "Account": acc,
            "Trades": s["total_trades"],
            "Net P&L": round(s["net_profit"], 2),
            "Return %": round(s["net_profit"] / bal * 100, 2) if bal else 0,
            "Win Rate %": s["win_rate"],
            "Profit Factor": s["profit_factor"],
            "Expectancy": s["expectancy"],
            "Max DD": s["max_drawdown"],
            "Max DD %": abs(s.get("max_drawdown_pct", 0)),
            "Sharpe": _calc_sharpe(sub),
            "Algos": sub["comment"].dropna().nunique(),
        })
    if rows:
        st.dataframe(pd.DataFrame(rows).sort_values("Net P&L", ascending=False),
                     use_container_width=True, hide_index=True)


# ── 2. Algo Scorecard ────────────────────────────────────────────────────────

def _render_algo_scorecard(df, total_balance):
    from mt5_parser import calc_stats

    st.subheader("Algo Scorecard")
    st.caption("Composite score ranks each algo across profitability, consistency, and risk.")

    algos = df["comment"].dropna().unique()
    algos = [a for a in algos if str(a).strip()]
    if not algos:
        st.info("No algo comments found in trade data.")
        return

    view_mode = st.radio("View", ["All Accounts Combined", "By Account"],
                         horizontal=True, key="adv_scorecard_mode")

    if view_mode == "By Account":
        accounts = sorted(df["_account"].unique())
        for acc in accounts:
            acc_df = df[df["_account"] == acc]
            acc_bal = acc_df["_balance"].iloc[0] if not acc_df.empty else total_balance
            st.markdown(f"---")
            st.markdown(f"**{acc}**")
            rows = _build_scorecard_rows(acc_df, acc_bal, calc_stats)
            if not rows:
                st.caption("Not enough data for this account.")
                continue
            score_df = pd.DataFrame(rows).sort_values("Score", ascending=False).reset_index(drop=True)
            score_df.index += 1
            score_df.index.name = "Rank"
            tbl_height = max(200, min(len(score_df) * 38 + 40, 600))
            st.dataframe(score_df, use_container_width=True, height=tbl_height,
                         key=f"adv_scorecard_tbl_{acc}")
    else:
        rows = _build_scorecard_rows(df, total_balance, calc_stats, include_accounts=True)
        if not rows:
            st.info("Not enough data to score algos.")
            return
        score_df = pd.DataFrame(rows).sort_values("Score", ascending=False).reset_index(drop=True)
        score_df.index += 1
        score_df.index.name = "Rank"
        tbl_height = max(400, min(len(score_df) * 38 + 40, 800))
        st.dataframe(score_df, use_container_width=True, height=tbl_height)

    # Per-algo equity curves — ALL algos (always combined view)
    all_rows = _build_scorecard_rows(df, total_balance, calc_stats, include_accounts=True)
    if not all_rows:
        return
    score_df = pd.DataFrame(all_rows).sort_values("Score", ascending=False).reset_index(drop=True)
    st.markdown("---")
    st.markdown("**Per-Algo Equity Curves**")
    all_algo_names = score_df["Algo"].tolist()
    fig_ae = go.Figure()
    for algo in all_algo_names:
        sub = df[df["comment"] == algo].sort_values("close_time").copy()
        sub["_cum"] = sub["net_profit"].cumsum()
        fig_ae.add_trace(go.Scatter(
            x=sub["close_time"], y=sub["_cum"], mode="lines", name=algo,
            line=dict(width=2, shape="spline", smoothing=0.6)))
    fig_ae.update_layout(height=400, hovermode="x unified",
                         yaxis=dict(tickprefix="$", **LAYOUT["yaxis"]),
                         legend=dict(bgcolor="rgba(0,0,0,0)"),
                         **{k: v for k, v in LAYOUT.items() if k != "yaxis"})
    st.plotly_chart(fig_ae, use_container_width=True, key="adv_algo_eq")


def _build_scorecard_rows(df, deposit, calc_stats, include_accounts=False):
    algos = df["comment"].dropna().unique()
    algos = [a for a in algos if str(a).strip()]
    rows = []
    for algo in sorted(algos):
        sub = df[df["comment"] == algo]
        s = calc_stats(sub, deposit=deposit)
        if not s or s["total_trades"] < 1:
            continue
        sharpe = _calc_sharpe(sub)
        recovery = round(s["net_profit"] / abs(s["max_drawdown"]), 2) if s["max_drawdown"] != 0 else 0
        consistency = _calc_consistency(sub)
        score = _composite_score(s, sharpe, recovery, consistency)
        row = {
            "Algo": algo,
            "Score": score,
            "Trades": s["total_trades"],
            "Net P&L": round(s["net_profit"], 2),
            "Win Rate %": s["win_rate"],
            "Profit Factor": s["profit_factor"],
            "Expectancy": round(s["expectancy"], 2),
            "R:R": s["rr_ratio"],
            "Sharpe": sharpe,
            "Recovery Factor": recovery,
            "Consistency %": consistency,
            "Max DD": round(s["max_drawdown"], 2),
            "Avg Duration (min)": s["avg_duration_min"],
        }
        if include_accounts:
            row["Accounts"] = sub["_account"].nunique()
        row["Symbols"] = ", ".join(sorted(sub["symbol"].dropna().unique()))
        rows.append(row)
    return rows


# ── 3. Correlation Analysis ──────────────────────────────────────────────────

def _render_correlation(df):
    st.subheader("Correlation Analysis")

    df_daily = df.copy()
    df_daily["_day"] = df_daily["close_time"].dt.date

    # ── Account daily P&L correlation ────────────────────────────────────────
    pivot_acc = df_daily.groupby(["_day", "_account"])["net_profit"].sum().unstack(fill_value=0)

    if pivot_acc.shape[1] < 2:
        st.info("Need at least 2 accounts for correlation analysis.")
    else:
        st.markdown("**Account Daily P&L Correlation**")
        st.caption("High correlation between accounts means concentrated risk — they win and lose on the same days.")

        acc_threshold = st.slider("Account correlation threshold (|r|)",
                                  min_value=0.1, max_value=1.0, value=0.5, step=0.05,
                                  key="adv_acc_corr_thresh")

        corr_acc = pivot_acc.corr().round(3)
        _render_heatmap(corr_acc, "adv_corr_acc")

        # Notable correlations with algo detail + per-pair correlation slider
        pairs = []
        cols = corr_acc.columns.tolist()
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                r = corr_acc.iloc[i, j]
                if abs(r) >= acc_threshold:
                    acc_a, acc_b = cols[i], cols[j]
                    algos_a = set(df[df["_account"] == acc_a]["comment"].dropna().unique())
                    algos_b = set(df[df["_account"] == acc_b]["comment"].dropna().unique())
                    shared_algos = sorted(algos_a & algos_b)
                    shared_syms = sorted(
                        set(df[df["_account"] == acc_a]["symbol"].dropna().unique()) &
                        set(df[df["_account"] == acc_b]["symbol"].dropna().unique())
                    )
                    pairs.append({
                        "Account A": acc_a,
                        "Account B": acc_b,
                        "Correlation": r,
                        "Shared Algos": shared_algos,
                        "Shared Symbols": shared_syms,
                        "Algos A": sorted(algos_a),
                        "Algos B": sorted(algos_b),
                    })

        if pairs:
            st.markdown(f"**Notable Correlations (|r| >= {acc_threshold})**")
            for p_idx, pair in enumerate(sorted(pairs, key=lambda p: abs(p["Correlation"]), reverse=True)):
                r = pair["Correlation"]
                emoji = "🔴" if abs(r) > 0.7 else "🟡"
                label = "HIGH" if abs(r) > 0.7 else "MODERATE"
                direction = "positive" if r > 0 else "negative"

                with st.expander(
                    f"{emoji} {pair['Account A']} ↔ {pair['Account B']}: "
                    f"{r:.3f} ({label} {direction})"
                ):
                    pair_thresh = st.slider(
                        "Filter detail by correlation value",
                        min_value=0.0, max_value=1.0, value=0.0, step=0.05,
                        key=f"adv_acc_pair_thresh_{p_idx}")
                    detail_rows = []
                    # Per-algo correlation between the two accounts
                    for algo in pair["Shared Algos"]:
                        algo_a = df[(df["_account"] == pair["Account A"]) & (df["comment"] == algo)]
                        algo_b = df[(df["_account"] == pair["Account B"]) & (df["comment"] == algo)]
                        daily_a = algo_a.groupby(algo_a["close_time"].dt.date)["net_profit"].sum()
                        daily_b = algo_b.groupby(algo_b["close_time"].dt.date)["net_profit"].sum()
                        merged = pd.DataFrame({"a": daily_a, "b": daily_b}).dropna()
                        algo_r = merged["a"].corr(merged["b"]) if len(merged) >= 3 else 0
                        if abs(algo_r) >= pair_thresh:
                            detail_rows.append({
                                "Type": "Shared Algo",
                                "Item": algo,
                                pair["Account A"]: round(algo_r, 3),
                                pair["Account B"]: round(algo_r, 3),
                            })
                    for sym in pair["Shared Symbols"]:
                        sym_a = df[(df["_account"] == pair["Account A"]) & (df["symbol"] == sym)]
                        sym_b = df[(df["_account"] == pair["Account B"]) & (df["symbol"] == sym)]
                        daily_a = sym_a.groupby(sym_a["close_time"].dt.date)["net_profit"].sum()
                        daily_b = sym_b.groupby(sym_b["close_time"].dt.date)["net_profit"].sum()
                        merged = pd.DataFrame({"a": daily_a, "b": daily_b}).dropna()
                        sym_r = merged["a"].corr(merged["b"]) if len(merged) >= 3 else 0
                        if abs(sym_r) >= pair_thresh:
                            detail_rows.append({
                                "Type": "Shared Symbol",
                                "Item": sym,
                                pair["Account A"]: round(sym_r, 3),
                                pair["Account B"]: round(sym_r, 3),
                            })
                    for algo in sorted(set(pair["Algos A"]) - set(pair["Shared Algos"])):
                        detail_rows.append({
                            "Type": "Algo (A only)",
                            "Item": algo,
                            pair["Account A"]: "—",
                            pair["Account B"]: "—",
                        })
                    for algo in sorted(set(pair["Algos B"]) - set(pair["Shared Algos"])):
                        detail_rows.append({
                            "Type": "Algo (B only)",
                            "Item": algo,
                            pair["Account A"]: "—",
                            pair["Account B"]: "—",
                        })
                    if detail_rows:
                        st.dataframe(pd.DataFrame(detail_rows),
                                     use_container_width=True, hide_index=True)
                    else:
                        st.caption("No items meet the correlation filter.")
        elif pivot_acc.shape[1] >= 2:
            st.success(f"No account pairs exceed the correlation threshold of {acc_threshold}.")

    # ── Algo daily P&L correlation ───────────────────────────────────────────
    st.markdown("---")
    st.markdown("**Algo Daily P&L Correlation**")
    st.caption("Algos that correlate may trade the same setups — diversification benefit is low.")

    algo_list = [a for a in df["comment"].dropna().unique() if str(a).strip()]
    if len(algo_list) < 2:
        st.info("Need at least 2 algos for correlation analysis.")
    else:
        algo_threshold = st.slider("Algo correlation threshold (|r|)",
                                   min_value=0.1, max_value=1.0, value=0.5, step=0.05,
                                   key="adv_algo_corr_thresh")

        pivot_algo = df_daily.groupby(["_day", "comment"])["net_profit"].sum().unstack(fill_value=0)
        min_days = 5
        valid_algos = pivot_algo.columns[pivot_algo.astype(bool).sum() >= min_days]
        pivot_algo = pivot_algo[valid_algos]

        if pivot_algo.shape[1] < 2:
            st.info(f"Need at least 2 algos with {min_days}+ trading days.")
        else:
            corr_algo = pivot_algo.corr().round(3)

            # Slider to hide low correlations from heatmap
            algo_heatmap_min = st.slider("Hide heatmap values below (|r|)",
                                         min_value=0.0, max_value=1.0, value=0.0, step=0.05,
                                         key="adv_algo_heatmap_min")
            corr_display = corr_algo.copy()
            mask = corr_display.abs() < algo_heatmap_min
            mask_arr = mask.values.copy()
            np.fill_diagonal(mask_arr, False)
            mask = pd.DataFrame(mask_arr, index=mask.index, columns=mask.columns)
            corr_display = corr_display.where(~mask, other=np.nan)
            _render_heatmap_with_nan(corr_display, "adv_corr_algo")

            # Notable algo correlations as table
            algo_pairs = []
            acols = corr_algo.columns.tolist()
            for i in range(len(acols)):
                for j in range(i + 1, len(acols)):
                    r = corr_algo.iloc[i, j]
                    if abs(r) >= algo_threshold:
                        algo_a, algo_b = acols[i], acols[j]
                        syms_a = set(df[df["comment"] == algo_a]["symbol"].dropna().unique())
                        syms_b = set(df[df["comment"] == algo_b]["symbol"].dropna().unique())
                        accs_a = set(df[df["comment"] == algo_a]["_account"].dropna().unique())
                        accs_b = set(df[df["comment"] == algo_b]["_account"].dropna().unique())
                        algo_pairs.append({
                            "Algo A": algo_a,
                            "Algo B": algo_b,
                            "Correlation": round(r, 3),
                            "Strength": "HIGH" if abs(r) > 0.7 else "MODERATE",
                            "Direction": "positive" if r > 0 else "negative",
                            "Shared Symbols": ", ".join(sorted(syms_a & syms_b)) or "—",
                            "Shared Accounts": ", ".join(sorted(accs_a & accs_b)) or "—",
                            "Symbols A": ", ".join(sorted(syms_a)),
                            "Symbols B": ", ".join(sorted(syms_b)),
                        })

            if algo_pairs:
                st.markdown(f"**Notable Algo Correlations (|r| >= {algo_threshold})**")
                algo_pair_df = pd.DataFrame(algo_pairs).sort_values("Correlation",
                    key=lambda x: x.abs(), ascending=False)
                tbl_h = max(200, min(len(algo_pair_df) * 38 + 40, 600))
                st.dataframe(algo_pair_df, use_container_width=True, hide_index=True, height=tbl_h)
            else:
                st.success(f"No algo pairs exceed the correlation threshold of {algo_threshold}.")

    # ── Position correlation (trades within 1h) ──────────────────────────────
    st.markdown("---")
    st.markdown("**Position Correlation (trades within 1 hour)**")
    st.caption("Detects trades across different accounts/algos that opened or closed within 1 hour of each other — "
               "indicating they may be reacting to the same market event.")

    _render_position_correlation(df)

    # ── Symbol correlation ───────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("**Symbol Daily P&L Correlation**")
    st.caption("Symbols that correlate may expose the portfolio to the same market moves.")

    pivot_sym = df_daily.groupby(["_day", "symbol"])["net_profit"].sum().unstack(fill_value=0)
    min_days = 5
    valid_syms = pivot_sym.columns[pivot_sym.astype(bool).sum() >= min_days]
    pivot_sym = pivot_sym[valid_syms]

    if pivot_sym.shape[1] >= 2:
        corr_sym = pivot_sym.corr().round(3)
        _render_heatmap(corr_sym, "adv_corr_sym")
    else:
        st.info("Need at least 2 symbols with enough data.")


def _render_position_correlation(df):
    """Find trades opened or closed within 1h of each other across different accounts/algos."""
    if len(df) < 2:
        st.info("Not enough trades for position correlation.")
        return

    window_h = st.slider("Time window (hours)", min_value=0.5, max_value=4.0,
                          value=1.0, step=0.5, key="adv_pos_corr_window")
    window_td = timedelta(hours=window_h)

    sub = df[["open_time", "close_time", "_account", "comment", "symbol", "type",
              "net_profit", "volume"]].copy()
    sub = sub.dropna(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)

    clusters = []
    used = set()

    for i in range(len(sub)):
        if i in used:
            continue
        cluster = [i]
        for j in range(i + 1, len(sub)):
            if j in used:
                continue
            # Same account+algo = same EA, skip
            if sub.loc[i, "_account"] == sub.loc[j, "_account"] and \
               sub.loc[i, "comment"] == sub.loc[j, "comment"]:
                continue
            open_diff = abs((sub.loc[j, "open_time"] - sub.loc[i, "open_time"]).total_seconds())
            if open_diff <= window_td.total_seconds():
                cluster.append(j)
                used.add(j)
            elif open_diff > window_td.total_seconds() * 2:
                break

        if len(cluster) >= 2:
            used.update(cluster)
            members = sub.loc[cluster]
            clusters.append({
                "Time": members["open_time"].min().strftime("%Y-%m-%d %H:%M"),
                "Trades": len(cluster),
                "Accounts": ", ".join(sorted(members["_account"].unique())),
                "Algos": ", ".join(sorted(members["comment"].dropna().unique())),
                "Symbols": ", ".join(sorted(members["symbol"].unique())),
                "Directions": ", ".join(members["type"].values),
                "Combined P&L": round(members["net_profit"].sum(), 2),
                "Combined Vol": round(members["volume"].sum(), 2),
            })

    if clusters:
        cluster_df = pd.DataFrame(clusters).sort_values("Time", ascending=False)
        st.markdown(f"Found **{len(clusters)}** position clusters within {window_h}h window")
        tbl_height = max(300, min(len(cluster_df) * 38 + 40, 600))
        st.dataframe(cluster_df, use_container_width=True, hide_index=True, height=tbl_height)
    else:
        st.success(f"No clustered positions found within {window_h}h window.")


def _render_heatmap(corr_matrix, key):
    labels = corr_matrix.columns.tolist()
    z = corr_matrix.values.tolist()
    fig = go.Figure(go.Heatmap(
        z=z, x=labels, y=labels,
        colorscale=[[0, "#E05555"], [0.5, "#1a1a2e"], [1, "#34C27A"]],
        zmin=-1, zmax=1,
        text=[[f"{v:.2f}" for v in row] for row in z],
        texttemplate="%{text}",
        showscale=True,
    ))
    fig.update_layout(
        height=max(300, len(labels) * 35),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(family="sans-serif"),
        margin=dict(l=120, r=20, t=10, b=100),
        xaxis=dict(tickangle=-45),
    )
    st.plotly_chart(fig, use_container_width=True, key=key)


def _render_heatmap_with_nan(corr_matrix, key):
    """Heatmap that renders NaN cells as blank."""
    labels = corr_matrix.columns.tolist()
    z = corr_matrix.values
    text = [[f"{v:.2f}" if not np.isnan(v) else "" for v in row] for row in z]
    z_list = [[v if not np.isnan(v) else None for v in row] for row in z]
    fig = go.Figure(go.Heatmap(
        z=z_list, x=labels, y=labels,
        colorscale=[[0, "#E05555"], [0.5, "#1a1a2e"], [1, "#34C27A"]],
        zmin=-1, zmax=1,
        text=text,
        texttemplate="%{text}",
        showscale=True,
    ))
    fig.update_layout(
        height=max(300, len(labels) * 35),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(family="sans-serif"),
        margin=dict(l=120, r=20, t=10, b=100),
        xaxis=dict(tickangle=-45),
    )
    st.plotly_chart(fig, use_container_width=True, key=key)


# ── 4. Symbol Exposure ────────────────────────────────────────────────────────

def _render_symbol_exposure(df, total_balance):
    from mt5_parser import calc_stats

    st.subheader("Symbol Exposure")

    sym_pnl = df.groupby("symbol").agg(
        net_profit=("net_profit", "sum"),
        trades=("net_profit", "count"),
        volume=("volume", "sum"),
    ).sort_values("net_profit", ascending=False).reset_index()

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**P&L Distribution by Symbol**")
        colors = ["#34C27A" if v >= 0 else "#E05555" for v in sym_pnl["net_profit"]]
        fig_bar = go.Figure(go.Bar(
            x=sym_pnl["symbol"], y=sym_pnl["net_profit"].round(2),
            marker_color=colors))
        fig_bar.update_layout(height=350, yaxis=dict(tickprefix="$", **LAYOUT["yaxis"]),
                              xaxis=dict(type="category", **LAYOUT["xaxis"]),
                              **{k: v for k, v in LAYOUT.items() if k not in ("xaxis", "yaxis")})
        st.plotly_chart(fig_bar, use_container_width=True, key="adv_sym_pnl")

    with col2:
        st.markdown("**Trade Count by Symbol**")
        fig_pie = go.Figure(go.Pie(
            labels=sym_pnl["symbol"], values=sym_pnl["trades"],
            hole=0.4, textinfo="label+percent",
            marker=dict(line=dict(color="rgba(0,0,0,0.3)", width=1))))
        fig_pie.update_layout(height=350, plot_bgcolor="rgba(0,0,0,0)",
                              paper_bgcolor="rgba(0,0,0,0)",
                              font=dict(family="sans-serif"),
                              margin=dict(l=20, r=20, t=10, b=10),
                              showlegend=False)
        st.plotly_chart(fig_pie, use_container_width=True, key="adv_sym_pie")

    st.markdown("**Symbol Performance Table**")
    sym_rows = []
    for sym in sym_pnl["symbol"]:
        sub = df[df["symbol"] == sym]
        s = calc_stats(sub, deposit=total_balance)
        if not s:
            continue
        sym_rows.append({
            "Symbol": sym,
            "Trades": s["total_trades"],
            "Net P&L": round(s["net_profit"], 2),
            "Win Rate %": s["win_rate"],
            "Profit Factor": s["profit_factor"],
            "Expectancy": round(s["expectancy"], 2),
            "Max DD": round(s["max_drawdown"], 2),
            "Avg Duration (min)": s["avg_duration_min"],
            "Algos": sub["comment"].dropna().nunique(),
        })
    if sym_rows:
        st.dataframe(pd.DataFrame(sym_rows).sort_values("Net P&L", ascending=False),
                     use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("**Symbol × Algo P&L Heatmap**")
    st.caption("Which algo is profitable on which symbol?")

    valid_comments = df[df["comment"].notna() & (df["comment"].str.strip() != "")]
    if valid_comments.empty:
        st.info("No algo comments available.")
        return

    pivot = valid_comments.pivot_table(
        index="symbol", columns="comment", values="net_profit",
        aggfunc="sum", fill_value=0
    ).round(2)

    if pivot.shape[0] >= 1 and pivot.shape[1] >= 1:
        raw = pivot.values
        max_abs = max(abs(raw.min()), abs(raw.max()), 1)
        z_display = [[v if v != 0 else None for v in row] for row in raw]
        text = [[f"${v:,.0f}" if v != 0 else "" for v in row] for row in raw]
        fig = go.Figure(go.Heatmap(
            z=z_display, x=pivot.columns.tolist(), y=pivot.index.tolist(),
            colorscale=[[0, "#E05555"], [0.5, "#1a1a2e"], [1, "#34C27A"]],
            zmin=-max_abs, zmax=max_abs,
            text=text,
            texttemplate="%{text}",
            showscale=True,
            xgap=2, ygap=2,
        ))
        fig.update_layout(
            height=max(300, len(pivot.index) * 35),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            font=dict(family="sans-serif"),
            margin=dict(l=120, r=20, t=10, b=100),
            xaxis=dict(tickangle=-45, showgrid=True,
                       gridcolor="rgba(128,128,128,0.2)"),
            yaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.2)"),
        )
        st.plotly_chart(fig, use_container_width=True, key="adv_sym_algo_heat")


# ── 5. Weak Algo Detection ───────────────────────────────────────────────────

def _render_weak_algos(df, total_balance):
    from mt5_parser import calc_stats

    st.subheader("Weak Algo Detection")
    st.caption("Flags algos that may be underperforming and need review or removal. "
               "Rated per-account so you can see where an algo is strong vs weak.")

    algos = [a for a in df["comment"].dropna().unique() if str(a).strip()]
    if not algos:
        st.info("No algo comments found.")
        return

    # Build per-algo-per-account assessment
    all_rows = []
    for algo in sorted(algos):
        sub_algo = df[df["comment"] == algo]
        accounts = sorted(sub_algo["_account"].unique())

        for acc in accounts:
            sub = sub_algo[sub_algo["_account"] == acc]
            bal = sub["_balance"].iloc[0] if not sub.empty else total_balance
            s = calc_stats(sub, deposit=bal)
            if not s or s["total_trades"] < 3:
                continue

            flags = []
            severity = 0
            strengths = []

            # Weaknesses
            if s["net_profit"] < 0:
                flags.append(f"Negative P&L: ${s['net_profit']:,.2f}")
                severity += 3
            if s["win_rate"] < 40:
                flags.append(f"Low win rate: {s['win_rate']}%")
                severity += 2
            if s["profit_factor"] < 1.0 and s["profit_factor"] != float("inf"):
                flags.append(f"PF < 1: {s['profit_factor']}")
                severity += 3
            if s["expectancy"] < 0:
                flags.append(f"Neg expectancy: ${s['expectancy']:,.2f}")
                severity += 3
            if s["max_consec_losses"] >= 5:
                flags.append(f"Loss streak: {s['max_consec_losses']}")
                severity += 1

            recovery = (s["net_profit"] / abs(s["max_drawdown"])) if s["max_drawdown"] != 0 else 0
            if 0 < recovery < 0.5 and s["total_trades"] >= 10:
                flags.append(f"Low recovery: {recovery:.2f}")
                severity += 2

            consistency = _calc_consistency(sub)
            if consistency < 40 and s["total_trades"] >= 10:
                flags.append(f"Low consistency: {consistency:.0f}%")
                severity += 1

            # Stagnation
            sub_sorted = sub.sort_values("close_time")
            cum = sub_sorted["net_profit"].cumsum()
            if not cum.empty:
                peak_idx = cum.cummax()
                at_peak = sub_sorted[cum >= peak_idx]
                if not at_peak.empty:
                    last_high = pd.to_datetime(at_peak["close_time"].max())
                    stag = (datetime.now() - last_high).days
                    if stag >= 21:
                        flags.append(f"Stagnation: {stag}d")
                        severity += 1

            # Strengths
            if s["net_profit"] > 0:
                strengths.append(f"Profitable: ${s['net_profit']:,.2f}")
            if s["win_rate"] >= 60:
                strengths.append(f"High WR: {s['win_rate']}%")
            if s["profit_factor"] >= 1.5 and s["profit_factor"] != float("inf"):
                strengths.append(f"Strong PF: {s['profit_factor']}")
            if s["expectancy"] > 0:
                strengths.append(f"Pos expectancy: ${s['expectancy']:,.2f}")
            if recovery >= 1.5:
                strengths.append(f"Good recovery: {recovery:.2f}")
            if consistency >= 60:
                strengths.append(f"Consistent: {consistency:.0f}%")

            # Rating
            if severity >= 6:
                rating = "CRITICAL"
            elif severity >= 3:
                rating = "WEAK"
            elif flags:
                rating = "MIXED"
            elif len(strengths) >= 3:
                rating = "STRONG"
            else:
                rating = "OK"

            all_rows.append({
                "Algo": algo,
                "Account": acc,
                "Rating": rating,
                "Trades": s["total_trades"],
                "Net P&L": round(s["net_profit"], 2),
                "Win Rate %": s["win_rate"],
                "Profit Factor": s["profit_factor"],
                "Expectancy": round(s["expectancy"], 2),
                "Recovery": round(recovery, 2),
                "Consistency %": round(consistency, 1),
                "Weaknesses": " · ".join(flags) if flags else "—",
                "Strengths": " · ".join(strengths) if strengths else "—",
                "_severity": severity,
            })

    if not all_rows:
        st.success("Not enough data to assess algos (need 3+ trades per algo per account).")
        return

    result_df = pd.DataFrame(all_rows).sort_values("_severity", ascending=False).reset_index(drop=True)

    # Summary counts
    critical = len(result_df[result_df["Rating"] == "CRITICAL"])
    weak = len(result_df[result_df["Rating"] == "WEAK"])
    mixed = len(result_df[result_df["Rating"] == "MIXED"])
    ok_count = len(result_df[result_df["Rating"] == "OK"])
    strong = len(result_df[result_df["Rating"] == "STRONG"])

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Critical", critical)
    c2.metric("Weak", weak)
    c3.metric("Mixed", mixed)
    c4.metric("OK", ok_count)
    c5.metric("Strong", strong)

    # Filters
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        rating_filter = st.multiselect("Filter by rating",
                                       ["CRITICAL", "WEAK", "MIXED", "OK", "STRONG"],
                                       default=["CRITICAL", "WEAK", "MIXED"],
                                       key="adv_weak_rating_filter")
    with fc2:
        weak_acc_opts = sorted(result_df["Account"].unique())
        weak_acc_filter = st.multiselect("Filter by account", weak_acc_opts,
                                         key="adv_weak_acc_filter")
    with fc3:
        weak_sym_opts = sorted(df["symbol"].dropna().unique())
        weak_sym_filter = st.multiselect("Filter by symbol", weak_sym_opts,
                                          key="adv_weak_sym_filter")

    display_df = result_df.copy()
    if rating_filter:
        display_df = display_df[display_df["Rating"].isin(rating_filter)]
    if weak_acc_filter:
        display_df = display_df[display_df["Account"].isin(weak_acc_filter)]
    if weak_sym_filter:
        algo_with_sym = set()
        for algo in display_df["Algo"].unique():
            algo_syms = set(df[df["comment"] == algo]["symbol"].dropna().unique())
            if algo_syms & set(weak_sym_filter):
                algo_with_sym.add(algo)
        display_df = display_df[display_df["Algo"].isin(algo_with_sym)]
    display_df = display_df.drop(columns=["_severity"])

    tbl_height = max(400, min(len(display_df) * 38 + 40, 800))
    st.dataframe(display_df, use_container_width=True, hide_index=True, height=tbl_height)

    # Algo summary — aggregated rating across accounts
    st.markdown("---")
    st.markdown("**Algo Summary (across all accounts)**")
    st.caption("Shows how each algo performs across different accounts.")

    algo_summary = []
    for algo in sorted(algos):
        algo_rows = result_df[result_df["Algo"] == algo]
        if algo_rows.empty:
            continue
        ratings = algo_rows["Rating"].tolist()
        algo_summary.append({
            "Algo": algo,
            "Accounts": len(algo_rows),
            "Total Trades": int(algo_rows["Trades"].sum()),
            "Total P&L": round(algo_rows["Net P&L"].sum(), 2),
            "Avg Win Rate %": round(algo_rows["Win Rate %"].mean(), 1),
            "Avg PF": round(algo_rows["Profit Factor"].mean(), 2),
            "Ratings": ", ".join(ratings),
            "Critical Count": ratings.count("CRITICAL"),
            "Weak Count": ratings.count("WEAK"),
            "Strong Count": ratings.count("STRONG"),
        })

    if algo_summary:
        summary_df = pd.DataFrame(algo_summary).sort_values("Critical Count", ascending=False)
        st.dataframe(summary_df, use_container_width=True, hide_index=True)


# ── 6. Algo Consistency ──────────────────────────────────────────────────────

def _render_algo_consistency(df, total_balance):
    st.subheader("Algo Consistency Over Time")
    st.caption("Rolling performance shows whether an algo is improving, stable, or degrading.")

    algos = [a for a in df["comment"].dropna().unique() if str(a).strip()]
    if not algos:
        st.info("No algo comments found.")
        return

    algo_sel = st.selectbox("Select algo", sorted(algos), key="adv_consist_algo")
    sub = df[df["comment"] == algo_sel].sort_values("close_time").copy()

    if len(sub) < 10:
        st.info(f"Not enough trades for rolling analysis ({len(sub)} trades, need 10+).")
        return

    window = st.slider("Rolling window (trades)", min_value=5, max_value=min(100, len(sub)),
                        value=min(20, len(sub)), key="adv_consist_window")

    sub = sub.reset_index(drop=True)
    sub["_cum"] = sub["net_profit"].cumsum()
    sub["_peak"] = sub["_cum"].cummax()
    sub["_dd"] = sub["_cum"] - sub["_peak"]

    # Rolling by trade count
    sub["_rolling_pnl"] = sub["net_profit"].rolling(window, min_periods=1).sum()
    sub["_rolling_wr"] = sub["win"].rolling(window, min_periods=1).mean() * 100

    # Rolling calendar P&L and WR (user-selectable window)
    cal_window = st.select_slider("Calendar rolling window",
                                   options=[30, 60, 90],
                                   value=30, format_func=lambda x: f"{x} days",
                                   key="adv_consist_cal_window")
    sub["_close_dt"] = pd.to_datetime(sub["close_time"])
    sub = sub.set_index("_close_dt", drop=False)
    sub["_roll_cal_pnl"] = sub["net_profit"].rolling(f"{cal_window}D", min_periods=1).sum()
    sub["_roll_cal_wr"] = sub["win"].rolling(f"{cal_window}D", min_periods=1).mean() * 100
    sub = sub.reset_index(drop=True)

    # Equity curve
    fig_eq = go.Figure(go.Scatter(
        x=sub["close_time"], y=sub["_cum"], mode="lines", name="Equity",
        line=dict(color="#7c6af7", width=2, shape="spline", smoothing=0.6),
        fill="tozeroy", fillcolor="rgba(124,106,247,0.08)"))
    fig_eq.update_layout(height=250, title=f"{algo_sel} — Equity Curve",
                         hovermode="x unified",
                         yaxis=dict(tickprefix="$", **LAYOUT["yaxis"]),
                         **{k: v for k, v in LAYOUT.items() if k != "yaxis"})
    st.plotly_chart(fig_eq, use_container_width=True, key="adv_consist_eq")

    # Drawdown chart
    fig_dd = go.Figure(go.Scatter(
        x=sub["close_time"], y=sub["_dd"], mode="lines",
        fill="tozeroy",
        line=dict(color="rgba(220,80,80,0.8)", width=1.5, shape="spline", smoothing=0.6),
        fillcolor="rgba(220,80,80,0.15)",
        hovertemplate="%{x}<br>DD: $%{y:.2f}<extra></extra>"))
    fig_dd.update_layout(height=150, showlegend=False, title="Drawdown",
                         xaxis=dict(gridcolor="rgba(128,128,128,0.15)"),
                         yaxis=dict(gridcolor="rgba(128,128,128,0.15)", tickprefix="$"),
                         plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                         font=dict(family="sans-serif"),
                         margin=dict(l=60, r=20, t=30, b=4))
    st.plotly_chart(fig_dd, use_container_width=True, key="adv_consist_dd")

    # Rolling trade-window charts
    col1, col2 = st.columns(2)

    with col1:
        fig_rpnl = go.Figure(go.Scatter(
            x=sub["close_time"], y=sub["_rolling_pnl"], mode="lines",
            name=f"Rolling {window}-trade P&L",
            line=dict(color="#34C27A", width=2, shape="spline", smoothing=0.6)))
        fig_rpnl.add_hline(y=0, line_dash="dash", line_color="rgba(128,128,128,0.3)")
        fig_rpnl.update_layout(height=250, title=f"Rolling {window}-Trade P&L",
                               yaxis=dict(tickprefix="$", **LAYOUT["yaxis"]),
                               **{k: v for k, v in LAYOUT.items() if k != "yaxis"})
        st.plotly_chart(fig_rpnl, use_container_width=True, key="adv_consist_rpnl")

    with col2:
        fig_rwr = go.Figure(go.Scatter(
            x=sub["close_time"], y=sub["_rolling_wr"], mode="lines",
            name=f"Rolling {window}-trade WR",
            line=dict(color="#F5A623", width=2, shape="spline", smoothing=0.6)))
        fig_rwr.add_hline(y=50, line_dash="dash", line_color="rgba(128,128,128,0.3)")
        fig_rwr.update_layout(height=250, title=f"Rolling {window}-Trade Win Rate",
                              yaxis=dict(ticksuffix="%", **LAYOUT["yaxis"]),
                              **{k: v for k, v in LAYOUT.items() if k != "yaxis"})
        st.plotly_chart(fig_rwr, use_container_width=True, key="adv_consist_rwr")

    # Rolling calendar charts
    st.markdown(f"**Rolling {cal_window}-Day Calendar Window**")
    col3, col4 = st.columns(2)

    with col3:
        fig_cpnl = go.Figure(go.Scatter(
            x=sub["close_time"], y=sub["_roll_cal_pnl"], mode="lines",
            name=f"{cal_window}-day P&L",
            line=dict(color="#7c6af7", width=2, shape="spline", smoothing=0.6)))
        fig_cpnl.add_hline(y=0, line_dash="dash", line_color="rgba(128,128,128,0.3)")
        fig_cpnl.update_layout(height=250, title=f"Rolling {cal_window}-Day P&L",
                                yaxis=dict(tickprefix="$", **LAYOUT["yaxis"]),
                                **{k: v for k, v in LAYOUT.items() if k != "yaxis"})
        st.plotly_chart(fig_cpnl, use_container_width=True, key="adv_consist_calpnl")

    with col4:
        fig_cwr = go.Figure(go.Scatter(
            x=sub["close_time"], y=sub["_roll_cal_wr"], mode="lines",
            name=f"{cal_window}-day WR",
            line=dict(color="#F5A623", width=2, shape="spline", smoothing=0.6)))
        fig_cwr.add_hline(y=50, line_dash="dash", line_color="rgba(128,128,128,0.3)")
        fig_cwr.update_layout(height=250, title=f"Rolling {cal_window}-Day Win Rate",
                               yaxis=dict(ticksuffix="%", **LAYOUT["yaxis"]),
                               **{k: v for k, v in LAYOUT.items() if k != "yaxis"})
        st.plotly_chart(fig_cwr, use_container_width=True, key="adv_consist_calwr")

    st.markdown("---")
    st.markdown(f"**{algo_sel} — Monthly Performance**")
    sub["_month"] = sub["close_time"].dt.to_period("M")
    monthly = sub.groupby("_month").agg(
        pnl=("net_profit", "sum"),
        trades=("net_profit", "count"),
        wins=("win", "sum"),
    ).reset_index()
    monthly["wr"] = (monthly["wins"] / monthly["trades"] * 100).round(1)
    monthly["_month_str"] = monthly["_month"].astype(str)

    fig_m = go.Figure(go.Bar(
        x=monthly["_month_str"], y=monthly["pnl"].round(2),
        marker_color=["rgba(52,194,122,0.85)" if v >= 0 else "rgba(220,80,80,0.85)"
                      for v in monthly["pnl"]],
        text=[f"{wr:.0f}%" for wr in monthly["wr"]],
        textposition="outside", textfont_size=10,
    ))
    fig_m.update_layout(height=300, title="Monthly P&L",
                        yaxis=dict(tickprefix="$", **LAYOUT["yaxis"]),
                        xaxis=dict(type="category", **LAYOUT["xaxis"]),
                        **{k: v for k, v in LAYOUT.items() if k not in ("xaxis", "yaxis")})
    st.plotly_chart(fig_m, use_container_width=True, key="adv_consist_monthly")


# ── 7. Generate AI Report ────────────────────────────────────────────────────

def _render_ai_report(df, total_balance, all_data, sel_accounts):
    from mt5_parser import calc_stats

    st.subheader("Generate AI Report")
    st.caption("Export a structured JSON report and optional trade history files for upload to an AI agent for analysis.")

    st.markdown(
        '<div style="background:rgba(124,106,247,0.08);border:1px solid rgba(124,106,247,0.2);'
        'border-radius:8px;padding:10px 14px;font-size:13px;color:#aaa;margin-bottom:12px">'
        '<b style="color:#c5beff">Format:</b> JSON — structured, machine-readable, and the best format '
        'for AI agent consumption. Trade history files are CSV (one per account) for easy parsing. '
        'Upload the JSON report to any AI chat (Claude, ChatGPT, etc.) and ask it to analyse your portfolio.'
        '</div>',
        unsafe_allow_html=True
    )

    # Time frame selector
    c1, c2 = st.columns(2)
    with c1:
        period = st.selectbox("Report period", [
            "Last Month",
            "Last Quarter (3 months)",
            "Last 6 Months",
            "Last 12 Months",
            "All Time",
        ], key="adv_report_period")
    with c2:
        include_trades = st.checkbox("Include individual trade history files (CSV per account)",
                                     value=False, key="adv_report_trades")

    # Calculate date range
    today = date.today()
    first_of_month = today.replace(day=1)
    last_month_end = first_of_month - timedelta(days=1)

    if period == "Last Month":
        start_date = last_month_end.replace(day=1)
        end_date = last_month_end
    elif period == "Last Quarter (3 months)":
        end_date = last_month_end
        m = last_month_end.month - 2
        y = last_month_end.year
        if m <= 0:
            m += 12
            y -= 1
        start_date = date(y, m, 1)
    elif period == "Last 6 Months":
        end_date = last_month_end
        m = last_month_end.month - 5
        y = last_month_end.year
        if m <= 0:
            m += 12
            y -= 1
        start_date = date(y, m, 1)
    elif period == "Last 12 Months":
        end_date = last_month_end
        m = last_month_end.month
        y = last_month_end.year - 1
        start_date = date(y, m, 1)
    else:
        start_date = df["open_time"].min().date()
        end_date = df["close_time"].max().date()

    st.caption(f"Report range: **{start_date}** to **{end_date}**")

    df_period = df[(df["open_time"].dt.date >= start_date) &
                   (df["open_time"].dt.date <= end_date)].copy()

    if df_period.empty:
        st.warning("No trades in the selected period.")
        return

    st.caption(f"**{len(df_period):,}** trades in period across "
               f"**{df_period['_account'].nunique()}** accounts")

    if st.button("Generate Report", type="primary", key="adv_gen_report"):
        with st.spinner("Building report..."):
            report = _build_ai_report(df_period, total_balance, all_data,
                                      sel_accounts, start_date, end_date)

            report_json = json.dumps(report, indent=2, default=str)
            st.download_button(
                label="📥 Download AI Report (JSON)",
                data=report_json,
                file_name=f"mt5_portfolio_report_{start_date}_{end_date}.json",
                mime="application/json",
                key="adv_download_report",
            )

            if include_trades:
                st.markdown("**Trade History Files (CSV)**")
                for acc in sorted(df_period["_account"].unique()):
                    acc_df = df_period[df_period["_account"] == acc].copy()
                    export_cols = [c for c in [
                        "open_time", "close_time", "symbol", "type", "volume",
                        "open_price", "close_price", "sl", "tp", "commission",
                        "swap", "profit", "net_profit", "comment", "duration_min",
                        "win", "day_of_week", "hour",
                    ] if c in acc_df.columns]
                    csv_buf = io.StringIO()
                    acc_df[export_cols].to_csv(csv_buf, index=False)
                    safe_name = acc.replace(" ", "_").replace("/", "_")
                    st.download_button(
                        label=f"📥 {acc} trades ({len(acc_df)} trades)",
                        data=csv_buf.getvalue(),
                        file_name=f"trades_{safe_name}_{start_date}_{end_date}.csv",
                        mime="text/csv",
                        key=f"adv_dl_trades_{safe_name}",
                    )

            # Preview
            with st.expander("Preview report structure", expanded=False):
                st.json(report)


def _build_ai_report(df, total_balance, all_data, sel_accounts, start_date, end_date):
    from mt5_parser import calc_stats

    report = {
        "report_type": "MT5 EA Portfolio Analysis",
        "generated_at": datetime.now().isoformat(),
        "period": {
            "start": str(start_date),
            "end": str(end_date),
            "trading_days": int(df["open_time"].dt.date.nunique()),
        },
        "portfolio": {},
        "accounts": [],
        "algo_scorecard": [],
        "algo_per_account": [],
        "symbols": [],
        "symbol_algo_matrix": [],
        "correlation": {},
        "position_clusters": [],
        "weak_algo_assessments": [],
        "monthly_summary": [],
    }

    # ── Portfolio-level stats ────────────────────────────────────────────────
    stats = calc_stats(df, deposit=total_balance)
    if stats:
        report["portfolio"] = {
            "total_balance": total_balance,
            "total_trades": stats["total_trades"],
            "net_profit": stats["net_profit"],
            "return_pct": round(stats["net_profit"] / total_balance * 100, 2) if total_balance else 0,
            "win_rate": stats["win_rate"],
            "profit_factor": stats["profit_factor"],
            "expectancy": stats["expectancy"],
            "max_drawdown": stats["max_drawdown"],
            "max_drawdown_pct": stats.get("max_drawdown_pct", 0),
            "sharpe_ratio": _calc_sharpe(df),
            "rr_ratio": stats["rr_ratio"],
            "avg_win": stats["avg_win"],
            "avg_loss": stats["avg_loss"],
            "best_trade": stats["best_trade"],
            "worst_trade": stats["worst_trade"],
            "max_consec_wins": stats["max_consec_wins"],
            "max_consec_losses": stats["max_consec_losses"],
            "long_trades": stats["long_trades"],
            "long_win_rate": stats["long_win_rate"],
            "short_trades": stats["short_trades"],
            "short_win_rate": stats["short_win_rate"],
            "active_algos": int(df["comment"].dropna().nunique()),
            "active_symbols": int(df["symbol"].dropna().nunique()),
            "active_accounts": int(df["_account"].nunique()),
        }

    # ── Per-account stats ────────────────────────────────────────────────────
    for acc in sorted(df["_account"].unique()):
        sub = df[df["_account"] == acc]
        bal = sub["_balance"].iloc[0] if not sub.empty else 0
        s = calc_stats(sub, deposit=bal)
        if not s:
            continue
        acc_type = next((d["type"] for d in all_data if d["label"] == acc), "Unknown")
        report["accounts"].append({
            "name": acc,
            "type": acc_type,
            "balance": bal,
            "trades": s["total_trades"],
            "net_profit": s["net_profit"],
            "return_pct": round(s["net_profit"] / bal * 100, 2) if bal else 0,
            "win_rate": s["win_rate"],
            "profit_factor": s["profit_factor"],
            "expectancy": s["expectancy"],
            "max_drawdown": s["max_drawdown"],
            "max_drawdown_pct": s.get("max_drawdown_pct", 0),
            "sharpe": _calc_sharpe(sub),
            "rr_ratio": s["rr_ratio"],
            "max_consec_wins": s["max_consec_wins"],
            "max_consec_losses": s["max_consec_losses"],
            "avg_duration_min": s["avg_duration_min"],
            "algos": sorted(sub["comment"].dropna().unique().tolist()),
            "symbols": sorted(sub["symbol"].dropna().unique().tolist()),
        })

    # ── Algo scorecard (combined) ────────────────────────────────────────────
    for algo in sorted(df["comment"].dropna().unique()):
        if not str(algo).strip():
            continue
        sub = df[df["comment"] == algo]
        s = calc_stats(sub, deposit=total_balance)
        if not s or s["total_trades"] < 1:
            continue
        sharpe = _calc_sharpe(sub)
        recovery = round(s["net_profit"] / abs(s["max_drawdown"]), 2) if s["max_drawdown"] != 0 else 0
        consistency = _calc_consistency(sub)
        report["algo_scorecard"].append({
            "name": algo,
            "composite_score": _composite_score(s, sharpe, recovery, consistency),
            "trades": s["total_trades"],
            "net_profit": s["net_profit"],
            "win_rate": s["win_rate"],
            "profit_factor": s["profit_factor"],
            "expectancy": s["expectancy"],
            "rr_ratio": s["rr_ratio"],
            "max_drawdown": s["max_drawdown"],
            "sharpe": sharpe,
            "recovery_factor": recovery,
            "consistency_pct": consistency,
            "avg_duration_min": s["avg_duration_min"],
            "accounts": sorted(sub["_account"].unique().tolist()),
            "symbols": sorted(sub["symbol"].dropna().unique().tolist()),
        })

    # ── Algo per account (with strength/weakness ratings) ────────────────────
    for algo in sorted(df["comment"].dropna().unique()):
        if not str(algo).strip():
            continue
        sub_algo = df[df["comment"] == algo]
        for acc in sorted(sub_algo["_account"].unique()):
            sub = sub_algo[sub_algo["_account"] == acc]
            bal = sub["_balance"].iloc[0] if not sub.empty else total_balance
            s = calc_stats(sub, deposit=bal)
            if not s or s["total_trades"] < 3:
                continue

            weaknesses = []
            strengths = []
            severity = 0

            if s["net_profit"] < 0:
                weaknesses.append("negative_pnl")
                severity += 3
            if s["win_rate"] < 40:
                weaknesses.append("low_win_rate")
                severity += 2
            if s["profit_factor"] < 1.0 and s["profit_factor"] != float("inf"):
                weaknesses.append("profit_factor_below_1")
                severity += 3
            if s["expectancy"] < 0:
                weaknesses.append("negative_expectancy")
                severity += 3
            if s["max_consec_losses"] >= 5:
                weaknesses.append("long_loss_streak")
                severity += 1

            recovery = (s["net_profit"] / abs(s["max_drawdown"])) if s["max_drawdown"] != 0 else 0
            if 0 < recovery < 0.5 and s["total_trades"] >= 10:
                weaknesses.append("low_recovery_factor")
                severity += 2

            consistency = _calc_consistency(sub)
            if consistency < 40 and s["total_trades"] >= 10:
                weaknesses.append("low_consistency")
                severity += 1

            if s["net_profit"] > 0:
                strengths.append("profitable")
            if s["win_rate"] >= 60:
                strengths.append("high_win_rate")
            if s["profit_factor"] >= 1.5 and s["profit_factor"] != float("inf"):
                strengths.append("strong_profit_factor")
            if s["expectancy"] > 0:
                strengths.append("positive_expectancy")
            if recovery >= 1.5:
                strengths.append("good_recovery")
            if consistency >= 60:
                strengths.append("consistent")

            if severity >= 6:
                rating = "CRITICAL"
            elif severity >= 3:
                rating = "WEAK"
            elif weaknesses:
                rating = "MIXED"
            elif len(strengths) >= 3:
                rating = "STRONG"
            else:
                rating = "OK"

            report["algo_per_account"].append({
                "algo": algo,
                "account": acc,
                "rating": rating,
                "trades": s["total_trades"],
                "net_profit": s["net_profit"],
                "win_rate": s["win_rate"],
                "profit_factor": s["profit_factor"],
                "expectancy": s["expectancy"],
                "recovery_factor": round(recovery, 2),
                "consistency_pct": round(consistency, 1),
                "weaknesses": weaknesses,
                "strengths": strengths,
            })

    # ── Per-symbol stats ─────────────────────────────────────────────────────
    for sym in sorted(df["symbol"].dropna().unique()):
        sub = df[df["symbol"] == sym]
        s = calc_stats(sub, deposit=total_balance)
        if not s:
            continue
        report["symbols"].append({
            "name": sym,
            "trades": s["total_trades"],
            "net_profit": s["net_profit"],
            "win_rate": s["win_rate"],
            "profit_factor": s["profit_factor"],
            "expectancy": s["expectancy"],
            "max_drawdown": s["max_drawdown"],
            "avg_duration_min": s["avg_duration_min"],
            "algos": sorted(sub["comment"].dropna().unique().tolist()),
            "accounts": sorted(sub["_account"].dropna().unique().tolist()),
        })

    # ── Symbol × Algo P&L matrix ─────────────────────────────────────────────
    valid_comments = df[df["comment"].notna() & (df["comment"].str.strip() != "")]
    if not valid_comments.empty:
        pivot = valid_comments.pivot_table(
            index="symbol", columns="comment", values="net_profit",
            aggfunc="sum", fill_value=0
        ).round(2)
        for sym in pivot.index:
            for algo in pivot.columns:
                val = float(pivot.loc[sym, algo])
                if val != 0:
                    report["symbol_algo_matrix"].append({
                        "symbol": sym, "algo": algo, "net_profit": val,
                    })

    # ── Correlation data ─────────────────────────────────────────────────────
    df_daily = df.copy()
    df_daily["_day"] = df_daily["close_time"].dt.date

    # Account correlation
    pivot_acc = df_daily.groupby(["_day", "_account"])["net_profit"].sum().unstack(fill_value=0)
    if pivot_acc.shape[1] >= 2:
        corr_acc = pivot_acc.corr().round(3)
        acc_corr_pairs = []
        cols = corr_acc.columns.tolist()
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                r = float(corr_acc.iloc[i, j])
                acc_a, acc_b = cols[i], cols[j]
                algos_a = sorted(df[df["_account"] == acc_a]["comment"].dropna().unique().tolist())
                algos_b = sorted(df[df["_account"] == acc_b]["comment"].dropna().unique().tolist())
                shared = sorted(set(algos_a) & set(algos_b))
                acc_corr_pairs.append({
                    "account_a": acc_a,
                    "account_b": acc_b,
                    "correlation": r,
                    "strength": "HIGH" if abs(r) > 0.7 else ("MODERATE" if abs(r) > 0.5 else "LOW"),
                    "shared_algos": shared,
                    "shared_symbols": sorted(
                        set(df[df["_account"] == acc_a]["symbol"].dropna().unique()) &
                        set(df[df["_account"] == acc_b]["symbol"].dropna().unique())
                    ),
                })
        report["correlation"]["account_pairs"] = acc_corr_pairs

    # Algo correlation
    pivot_algo = df_daily.groupby(["_day", "comment"])["net_profit"].sum().unstack(fill_value=0)
    valid_algos = pivot_algo.columns[pivot_algo.astype(bool).sum() >= 5]
    pivot_algo = pivot_algo[valid_algos]
    if pivot_algo.shape[1] >= 2:
        corr_algo = pivot_algo.corr().round(3)
        algo_corr_pairs = []
        acols = corr_algo.columns.tolist()
        for i in range(len(acols)):
            for j in range(i + 1, len(acols)):
                r = float(corr_algo.iloc[i, j])
                algo_a, algo_b = acols[i], acols[j]
                algo_corr_pairs.append({
                    "algo_a": algo_a,
                    "algo_b": algo_b,
                    "correlation": r,
                    "strength": "HIGH" if abs(r) > 0.7 else ("MODERATE" if abs(r) > 0.5 else "LOW"),
                    "shared_symbols": sorted(
                        set(df[df["comment"] == algo_a]["symbol"].dropna().unique()) &
                        set(df[df["comment"] == algo_b]["symbol"].dropna().unique())
                    ),
                    "shared_accounts": sorted(
                        set(df[df["comment"] == algo_a]["_account"].dropna().unique()) &
                        set(df[df["comment"] == algo_b]["_account"].dropna().unique())
                    ),
                })
        report["correlation"]["algo_pairs"] = algo_corr_pairs

    # Symbol correlation
    pivot_sym = df_daily.groupby(["_day", "symbol"])["net_profit"].sum().unstack(fill_value=0)
    valid_syms = pivot_sym.columns[pivot_sym.astype(bool).sum() >= 5]
    pivot_sym = pivot_sym[valid_syms]
    if pivot_sym.shape[1] >= 2:
        corr_sym = pivot_sym.corr().round(3)
        sym_corr_pairs = []
        scols = corr_sym.columns.tolist()
        for i in range(len(scols)):
            for j in range(i + 1, len(scols)):
                r = float(corr_sym.iloc[i, j])
                sym_corr_pairs.append({
                    "symbol_a": scols[i],
                    "symbol_b": scols[j],
                    "correlation": r,
                    "strength": "HIGH" if abs(r) > 0.7 else ("MODERATE" if abs(r) > 0.5 else "LOW"),
                })
        report["correlation"]["symbol_pairs"] = sym_corr_pairs

    # ── Position clusters (trades within 1h) ─────────────────────────────────
    pos_sub = df[["open_time", "close_time", "_account", "comment", "symbol", "type",
                  "net_profit", "volume"]].copy()
    pos_sub = pos_sub.dropna(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
    window_td = timedelta(hours=1)
    used = set()
    for i in range(len(pos_sub)):
        if i in used:
            continue
        cluster = [i]
        for j in range(i + 1, len(pos_sub)):
            if j in used:
                continue
            if pos_sub.loc[i, "_account"] == pos_sub.loc[j, "_account"] and \
               pos_sub.loc[i, "comment"] == pos_sub.loc[j, "comment"]:
                continue
            diff = abs((pos_sub.loc[j, "open_time"] - pos_sub.loc[i, "open_time"]).total_seconds())
            if diff <= window_td.total_seconds():
                cluster.append(j)
                used.add(j)
            elif diff > window_td.total_seconds() * 2:
                break
        if len(cluster) >= 2:
            used.update(cluster)
            members = pos_sub.loc[cluster]
            report["position_clusters"].append({
                "time": str(members["open_time"].min()),
                "trade_count": len(cluster),
                "accounts": sorted(members["_account"].unique().tolist()),
                "algos": sorted(members["comment"].dropna().unique().tolist()),
                "symbols": sorted(members["symbol"].unique().tolist()),
                "directions": members["type"].tolist(),
                "combined_pnl": round(float(members["net_profit"].sum()), 2),
            })

    # ── Monthly P&L summary ──────────────────────────────────────────────────
    df_monthly = df.copy()
    df_monthly["_ym"] = df_monthly["close_time"].dt.to_period("M").astype(str)
    monthly_pnl = df_monthly.groupby("_ym").agg(
        net_profit=("net_profit", "sum"),
        trades=("net_profit", "count"),
        wins=("win", "sum"),
    ).reset_index()
    monthly_pnl["win_rate"] = (monthly_pnl["wins"] / monthly_pnl["trades"] * 100).round(1)
    report["monthly_summary"] = monthly_pnl.rename(columns={"_ym": "month"}).to_dict(orient="records")

    # ── Monthly per-algo P&L ─────────────────────────────────────────────────
    algo_monthly = []
    for algo in sorted(df["comment"].dropna().unique()):
        if not str(algo).strip():
            continue
        asub = df[df["comment"] == algo].copy()
        asub["_ym"] = asub["close_time"].dt.to_period("M").astype(str)
        am = asub.groupby("_ym").agg(
            net_profit=("net_profit", "sum"),
            trades=("net_profit", "count"),
            wins=("win", "sum"),
        ).reset_index()
        am["win_rate"] = (am["wins"] / am["trades"] * 100).round(1)
        for _, row in am.iterrows():
            algo_monthly.append({
                "algo": algo,
                "month": row["_ym"],
                "net_profit": round(float(row["net_profit"]), 2),
                "trades": int(row["trades"]),
                "win_rate": float(row["win_rate"]),
            })
    report["algo_monthly_summary"] = algo_monthly

    return report


# ── Utility functions ─────────────────────────────────────────────────────────

def _calc_sharpe(df, risk_free_annual=0.0):
    daily = df.groupby(df["close_time"].dt.date)["net_profit"].sum()
    if len(daily) < 2:
        return 0.0
    mean = daily.mean()
    std = daily.std()
    if std == 0:
        return 0.0
    daily_rf = risk_free_annual / 252
    return round((mean - daily_rf) / std * np.sqrt(252), 2)


def _calc_consistency(df):
    df_tmp = df.copy()
    df_tmp["_week"] = df_tmp["close_time"].dt.isocalendar().week.astype(int)
    df_tmp["_year"] = df_tmp["close_time"].dt.isocalendar().year.astype(int)
    weekly = df_tmp.groupby(["_year", "_week"])["net_profit"].sum()
    if len(weekly) < 2:
        return 0.0
    profitable = (weekly > 0).sum()
    return round(profitable / len(weekly) * 100, 1)


def _composite_score(stats, sharpe, recovery, consistency):
    scores = []
    pf = min(stats["profit_factor"], 3.0) if stats["profit_factor"] != float("inf") else 3.0
    scores.append(min(pf / 3.0 * 25, 25))
    scores.append(min(stats["win_rate"] / 100 * 20, 20))
    s = min(max(sharpe, 0), 3.0)
    scores.append(s / 3.0 * 20)
    r = min(max(recovery, 0), 3.0)
    scores.append(r / 3.0 * 15)
    scores.append(min(consistency / 100 * 20, 20))
    return round(sum(scores), 1)
