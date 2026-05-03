"""
view_portfolio_builder.py — Portfolio Builder page for MT5 Tools
Tabs: Overview | Trades | Equity Chart | Strategies | Portfolios | What-If
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import io, importlib, sys, os

# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────
def _get_parser():
    if "mt5_parser" in sys.modules:
        return importlib.reload(sys.modules["mt5_parser"])
    import mt5_parser
    return mt5_parser


# ─────────────────────────────────────────────────────────────────────────────
# Parse & normalise
# ─────────────────────────────────────────────────────────────────────────────
def _parse_uploaded(file_obj):
    try:
        parser = _get_parser()
        raw    = file_obj.read()
        result = parser.detect_and_parse(raw, file_obj.name)
        return result[0] if isinstance(result, tuple) else result
    except Exception as e:
        st.error(f"Failed to parse **{file_obj.name}**: {e}")
        return None


def _ensure_columns(df: pd.DataFrame, label: str) -> pd.DataFrame:
    col_map = {}

    def _first(targets, dest):
        for c in targets:
            if c in df.columns and dest not in col_map.values():
                col_map[c] = dest
                return

    _first(["open_time","Open time","Open time ($)","Time"],   "open_time")
    _first(["close_time","Close time"],                        "close_time")
    _first(["symbol","Symbol"],                                "symbol")
    _first(["type","Type","Direction"],                        "type")
    _first(["net_profit","P/L in money","Profit","profit"],    "net_profit")
    _first(["open_price","Open price","Price"],                "open_price")
    _first(["close_price","Close price"],                      "close_price")
    _first(["volume","Volume","Size","size"],                  "volume")
    _first(["commission","Commission"],                        "commission")
    _first(["swap","Swap"],                                    "swap")
    _first(["comment","Comment"],                              "comment")

    df = df.rename(columns=col_map)

    if "net_profit" not in df.columns:
        for c in ["profit","Profit","P/L"]:
            if c in df.columns:
                comm  = pd.to_numeric(df.get("commission", 0), errors="coerce").fillna(0)
                swap_ = pd.to_numeric(df.get("swap", 0),       errors="coerce").fillna(0)
                df["net_profit"] = pd.to_numeric(df[c], errors="coerce").fillna(0) + comm + swap_
                break

    for tc in ["open_time","close_time"]:
        if tc in df.columns:
            df[tc] = pd.to_datetime(df[tc], dayfirst=True, errors="coerce")

    if "net_profit" in df.columns:
        df["net_profit"] = pd.to_numeric(df["net_profit"], errors="coerce").fillna(0)
        df["win"] = df["net_profit"] > 0

    df["_strategy"] = label
    df["_ea"]       = label   # EA = the uploaded filename stem
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Lot-size scaling — applies a multiplier to net_profit of a strategy copy
# ─────────────────────────────────────────────────────────────────────────────
def _scale_df(df: pd.DataFrame, multiplier: float) -> pd.DataFrame:
    """Return a copy of df with net_profit scaled by multiplier."""
    out = df.copy()
    out["net_profit"] = out["net_profit"] * multiplier
    if "win" in out.columns:
        out["win"] = out["net_profit"] > 0
    return out


def _get_effective_dfs(strategy_dfs: dict, lot_overrides: dict) -> dict:
    """Return strategy_dfs with lot-scaled copies substituted where overrides exist."""
    result = {}
    for label, df in strategy_dfs.items():
        mult = lot_overrides.get(label, 1.0)
        result[label] = _scale_df(df, mult) if mult != 1.0 else df
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Combine
# ─────────────────────────────────────────────────────────────────────────────
def _combine(dfs_dict: dict, deposit: float) -> pd.DataFrame:
    if not dfs_dict:
        return pd.DataFrame()
    combined = pd.concat([d.copy() for d in dfs_dict.values()], ignore_index=True)
    if "close_time" in combined.columns:
        combined = combined.sort_values("close_time").reset_index(drop=True)
    if "net_profit" in combined.columns:
        combined["equity"] = deposit + combined["net_profit"].cumsum()
    return combined


def _get_active_df(view_mode: str, eff_dfs: dict, portfolios: dict, deposit: float):
    if view_mode == "Portfolio (all)":
        return _combine(eff_dfs, deposit), "Portfolio (all)"
    if view_mode in portfolios:
        members = {k: eff_dfs[k] for k in portfolios[view_mode] if k in eff_dfs}
        return _combine(members, deposit), view_mode
    if view_mode in eff_dfs:
        df = eff_dfs[view_mode].copy()
        if "close_time" in df.columns:
            df = df.sort_values("close_time").reset_index(drop=True)
        if "net_profit" in df.columns:
            df["equity"] = deposit + df["net_profit"].cumsum()
        return df, view_mode
    return pd.DataFrame(), view_mode


# ─────────────────────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────────────────────
def _calc_stats(df: pd.DataFrame, deposit: float) -> dict:
    s = {}
    if df.empty or "net_profit" not in df.columns:
        return s
    profits = df["net_profit"].fillna(0)
    s["num_trades"]    = len(df)
    s["gross_profit"]  = float(profits[profits > 0].sum())
    s["gross_loss"]    = float(profits[profits < 0].sum())
    s["net_profit"]    = float(profits.sum())
    s["win_count"]     = int((profits > 0).sum())
    s["loss_count"]    = int((profits <= 0).sum())
    s["win_rate"]      = s["win_count"] / s["num_trades"] * 100 if s["num_trades"] else 0
    s["avg_win"]       = float(profits[profits > 0].mean()) if s["win_count"] else 0
    s["avg_loss"]      = float(profits[profits < 0].mean()) if s["loss_count"] else 0
    s["profit_factor"] = s["gross_profit"] / abs(s["gross_loss"]) if s["gross_loss"] else float("inf")
    s["avg_trade"]     = float(profits.mean())

    # Avg lot size
    if "volume" in df.columns:
        s["avg_lot"] = float(pd.to_numeric(df["volume"], errors="coerce").mean())
    else:
        s["avg_lot"] = 0.0

    eq = deposit + profits.cumsum()
    rm = eq.cummax()
    dd = eq - rm
    s["max_dd"]       = float(dd.min())
    s["max_dd_pct"]   = float(dd.min() / deposit * 100)
    s["ret_dd_ratio"] = s["net_profit"] / abs(s["max_dd"]) if s["max_dd"] else 0

    ws = (profits > 0).astype(int).tolist()
    cw = cl = mcw = mcl = 0
    for w in ws:
        if w: cw += 1; cl = 0
        else: cl += 1; cw = 0
        mcw = max(mcw, cw); mcl = max(mcl, cl)
    s["max_consec_wins"]   = mcw
    s["max_consec_losses"] = mcl

    if "close_time" in df.columns:
        vc = df["close_time"].dropna()
        vo = df["open_time"].dropna() if "open_time" in df.columns else vc
        if not vc.empty:
            s["start_date"]         = vo.min() if not vo.empty else vc.min()
            s["end_date"]           = vc.max()
            days                    = max((s["end_date"] - s["start_date"]).days, 1)
            s["years"]              = days / 365.25
            s["yearly_avg_profit"]  = s["net_profit"] / s["years"]
            s["monthly_avg_profit"] = s["net_profit"] / max(days / 30.44, 1)
            s["cagr"]               = ((deposit + s["net_profit"]) / deposit) ** (1 / s["years"]) - 1

    if "close_time" in df.columns:
        eq_ts = df[["close_time","net_profit"]].dropna().sort_values("close_time").copy()
        if not eq_ts.empty:
            eq_ts["cum"]  = deposit + eq_ts["net_profit"].cumsum()
            eq_ts["date"] = eq_ts["close_time"].dt.date
            dly = eq_ts.groupby("date")["cum"].last().reset_index()
            peak = float(dly["cum"].iloc[0]); stag_start = dly["date"].iloc[0]; max_stag = 0
            for _, r in dly.iterrows():
                if float(r["cum"]) > peak:
                    peak = float(r["cum"]); stag_start = r["date"]
                else:
                    max_stag = max(max_stag, (r["date"] - stag_start).days)
            s["max_stagnation_days"] = max_stag

    # Monthly tables — both $ and %
    if "close_time" in df.columns:
        mdf = df[["close_time","net_profit"]].dropna().copy()
        mdf["year"]  = mdf["close_time"].dt.year
        mdf["month"] = mdf["close_time"].dt.month
        monthly = mdf.groupby(["year","month"])["net_profit"].sum().reset_index()
        pivot_d = monthly.pivot(index="year", columns="month", values="net_profit").fillna(0)
        pivot_d.columns = [pd.Timestamp(2000, int(m), 1).strftime("%b") for m in pivot_d.columns]
        pivot_d["YTD"] = pivot_d.sum(axis=1)
        s["monthly_table_dollar"] = pivot_d

        # % — each month relative to deposit
        pivot_p = pivot_d.copy()
        for col in pivot_p.columns:
            pivot_p[col] = pivot_p[col] / deposit * 100
        s["monthly_table_pct"] = pivot_p

    return s


# ─────────────────────────────────────────────────────────────────────────────
# Chart helpers
# ─────────────────────────────────────────────────────────────────────────────
COLORS = ["#4C8EF5","#F5A623","#7ED321","#BD10E0",
          "#9B59B6","#1ABC9C","#E67E22","#FF6B9D"]


def _smooth(y: pd.Series, window: int) -> pd.Series:
    if window <= 1:
        return y
    return y.rolling(window=window, min_periods=1, center=True).mean()


def _add_stagnation_vrect(fig, df: pd.DataFrame, deposit: float):
    eq_ts = df[["close_time","net_profit"]].dropna().sort_values("close_time").copy()
    if eq_ts.empty:
        return
    eq_ts["cum"] = deposit + eq_ts["net_profit"].cumsum()
    peak = float(eq_ts["cum"].iloc[0]); stag_start = eq_ts["close_time"].iloc[0]
    max_days = 0; best_s = stag_start; best_e = stag_start
    for _, r in eq_ts.iterrows():
        if float(r["cum"]) > peak:
            days = (r["close_time"] - stag_start).days
            if days > max_days:
                max_days = days; best_s = stag_start; best_e = r["close_time"]
            peak = float(r["cum"]); stag_start = r["close_time"]
    if max_days > 0:
        fig.add_vrect(x0=best_s, x1=best_e,
            fillcolor="rgba(255,160,80,0.10)", line_width=0,
            annotation_text=f"Max stagnation: {max_days}d",
            annotation_position="top left",
            annotation_font_size=11, annotation_font_color="#FFB366",
            row=1, col=1)


def _build_equity_chart(
    df: pd.DataFrame, deposit: float,
    eff_dfs: dict, portfolios: dict, active_label: str,
    chart_view: str, smooth_window: int,
    show_stagnation: bool,
    date_from, date_to,
    selected_strategies: list,
    selected_portfolio: str = None,
):
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        row_heights=[0.62, 0.19, 0.19],
        vertical_spacing=0.02,
        subplot_titles=("", "Drawdown from Peak ($)", "Daily P&L ($)"),
    )
    all_dd_frames = []
    all_daily_frames = []

    # Compute the global time span across all series being plotted so every
    # line can be extended to span the full x-axis width.
    _all_times = []
    for _sdf in eff_dfs.values():
        if "close_time" in _sdf.columns:
            _all_times.append(pd.to_datetime(_sdf["close_time"]).dt.tz_localize(None).dropna())
    if not df.empty and "close_time" in df.columns:
        _all_times.append(pd.to_datetime(df["close_time"]).dt.tz_localize(None).dropna())
    _global_min = min(s.min() for s in _all_times) if _all_times else None
    _global_max = max(s.max() for s in _all_times) if _all_times else None
    # Clamp to date filter if set
    if date_from and _global_min is not None:
        _global_min = max(_global_min, pd.Timestamp(date_from))
    if date_to and _global_max is not None:
        _global_max = min(_global_max, pd.Timestamp(date_to) + pd.Timedelta(days=1))

    def _plot_series(times: pd.Series, profits: pd.Series,
                     name: str, color: str, width: float,
                     contribute_to_aggregates: bool = True):
        """Plot one equity line. Only contributes to row 2 (drawdown) and row 3
        (daily P&L) when contribute_to_aggregates=True. This prevents
        double-counting in modes that show both a combined line and individual
        strategy lines for the same underlying trades."""
        times   = times.reset_index(drop=True)
        profits = profits.reset_index(drop=True)
        eq_full = deposit + profits.cumsum()
        rm_full = eq_full.cummax()
        dd_full = eq_full - rm_full

        times_dt = pd.to_datetime(times).dt.tz_localize(None)
        mask = pd.Series([True] * len(times_dt), dtype=bool)
        if date_from:
            mask &= times_dt >= pd.Timestamp(date_from)
        if date_to:
            mask &= times_dt <= pd.Timestamp(date_to) + pd.Timedelta(days=1)

        times_f = times_dt[mask].reset_index(drop=True)
        eq_f    = eq_full[mask].reset_index(drop=True)
        dd_f    = dd_full[mask].reset_index(drop=True)
        if eq_f.empty:
            return

        if contribute_to_aggregates:
            all_dd_frames.append(pd.DataFrame({"t": times_f, "dd": dd_f}))

            # Daily P&L — sum net_profit per day within the filtered window
            profits_f = profits[mask].reset_index(drop=True)
            daily_pnl = (pd.DataFrame({"t": times_f, "pnl": profits_f})
                         .assign(date=lambda x: x["t"].dt.normalize())
                         .groupby("date")["pnl"].sum()
                         .reset_index()
                         .rename(columns={"date": "t"}))
            all_daily_frames.append(daily_pnl)

        eq_disp = _smooth(eq_f, smooth_window)

        # Extend line to global span so all series fill the full x-axis
        if _global_min is not None and len(times_f) > 0 and times_f.iloc[0] > _global_min:
            times_f = pd.concat([pd.Series([_global_min]), times_f], ignore_index=True)
            eq_disp = pd.concat([pd.Series([eq_disp.iloc[0]]), eq_disp], ignore_index=True)
        if _global_max is not None and len(times_f) > 0 and times_f.iloc[-1] < _global_max:
            times_f = pd.concat([times_f, pd.Series([_global_max])], ignore_index=True)
            eq_disp = pd.concat([eq_disp, pd.Series([eq_disp.iloc[-1]])], ignore_index=True)

        fig.add_trace(go.Scatter(
            x=times_f, y=eq_disp, name=name,
            line=dict(color=color, width=width), mode="lines",
            connectgaps=True,
            hovertemplate=f"<b>{name}</b><br>%{{x|%d %b %Y}}<br>${{y:,.2f}}<extra></extra>",
        ), row=1, col=1)

    if chart_view == "Portfolio":
        # Show the selected portfolio / all as one combined line
        if not df.empty and "close_time" in df.columns and "net_profit" in df.columns:
            _plot_series(df["close_time"], df["net_profit"], active_label, COLORS[0], 2.0,
                         contribute_to_aggregates=True)
            if show_stagnation:
                _add_stagnation_vrect(fig, df, deposit)

    elif chart_view == "Portfolio+Individual":
        # Combined line + each member underneath. Only the combined line
        # contributes to drawdown / daily-P&L subplots so we don't double-count.
        if not df.empty and "close_time" in df.columns and "net_profit" in df.columns:
            import os as _os, re as _re2
            _cfg = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".streamlit", "config.toml")
            _light = False
            if _os.path.isfile(_cfg):
                _m = _re2.search(r'base\s*=\s*"([^"]*)"', open(_cfg).read())
                if _m: _light = _m.group(1) == "light"
            _portfolio_color = "#1a3a5c" if _light else "#FFFFFF"
            _plot_series(df["close_time"], df["net_profit"], f"{active_label} (combined)",
                         _portfolio_color, 2.5, contribute_to_aggregates=True)
            if show_stagnation:
                _add_stagnation_vrect(fig, df, deposit)
        # Dedupe individual lines: skip any whose trade fingerprint matches another
        # already-plotted line (prevents EA/Strategy duplicates when each EA file
        # contains only one strategy comment).
        _seen_fingerprints = set()
        for i, label in enumerate(selected_strategies):
            if label not in eff_dfs:
                continue
            sdf = eff_dfs[label]
            if "close_time" not in sdf.columns or "net_profit" not in sdf.columns:
                continue
            sdf_s = sdf.sort_values("close_time")
            # Fingerprint: count + sum of net_profit + first/last close_time.
            # Two series with the same fingerprint are the same trades.
            try:
                fp = (
                    len(sdf_s),
                    round(float(sdf_s["net_profit"].sum()), 4),
                    str(pd.to_datetime(sdf_s["close_time"]).min()),
                    str(pd.to_datetime(sdf_s["close_time"]).max()),
                )
            except Exception:
                fp = (label,)
            if fp in _seen_fingerprints:
                continue
            _seen_fingerprints.add(fp)
            _plot_series(sdf_s["close_time"], sdf_s["net_profit"],
                         label, COLORS[i % len(COLORS)], 1.2,
                         contribute_to_aggregates=False)

    else:  # Individual
        # No combined line; each strategy's trades contribute to aggregates once.
        # Dedupe identical trade-sets (e.g. EA == Strategy when each file has
        # one strategy comment).
        _seen_fingerprints = set()
        for i, label in enumerate(selected_strategies):
            if label not in eff_dfs:
                continue
            sdf = eff_dfs[label]
            if "close_time" not in sdf.columns or "net_profit" not in sdf.columns:
                continue
            sdf_s = sdf.sort_values("close_time")
            try:
                fp = (
                    len(sdf_s),
                    round(float(sdf_s["net_profit"].sum()), 4),
                    str(pd.to_datetime(sdf_s["close_time"]).min()),
                    str(pd.to_datetime(sdf_s["close_time"]).max()),
                )
            except Exception:
                fp = (label,)
            if fp in _seen_fingerprints:
                continue
            _seen_fingerprints.add(fp)
            _plot_series(sdf_s["close_time"], sdf_s["net_profit"],
                         label, COLORS[i % len(COLORS)], 1.5,
                         contribute_to_aggregates=True)

    # Row 2 — cumulative drawdown from peak
    if all_dd_frames:
        dd_all = pd.concat(all_dd_frames).sort_values("t").reset_index(drop=True)
        dd_agg = dd_all.groupby("t")["dd"].min().reset_index()
        fig.add_trace(go.Scatter(
            x=dd_agg["t"], y=dd_agg["dd"],
            name="Peak DD", fill="tozeroy",
            fillcolor="rgba(220,50,50,0.30)",
            line=dict(color="rgba(220,50,50,0.75)", width=1),
            mode="lines", showlegend=False,
            hovertemplate="Peak DD: $%{y:,.2f}<extra></extra>",
        ), row=2, col=1)

    # Row 3 — daily P&L bars
    if all_daily_frames:
        daily_all = pd.concat(all_daily_frames).groupby("t")["pnl"].sum().reset_index()
        pos = daily_all["pnl"].clip(lower=0)
        neg = daily_all["pnl"].clip(upper=0)
        # Green bars for positive days
        fig.add_trace(go.Bar(
            x=daily_all["t"], y=pos,
            name="Daily gain",
            marker_color="rgba(52,194,122,0.70)",
            showlegend=False,
            hovertemplate="Daily P&L: $%{y:,.2f}<extra></extra>",
        ), row=3, col=1)
        # Red bars for negative days
        fig.add_trace(go.Bar(
            x=daily_all["t"], y=neg,
            name="Daily loss",
            marker_color="rgba(220,50,50,0.70)",
            showlegend=False,
            hovertemplate="Daily P&L: $%{y:,.2f}<extra></extra>",
        ), row=3, col=1)

    fig.update_layout(
        height=1240, margin=dict(l=60,r=20,t=24,b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="left", x=0, font=dict(size=11)),
        hovermode="x unified", bargap=0, barmode="overlay",
        hoverlabel=dict(namelength=-1, font=dict(size=11)),
    )
    # Force x-axis range to match the selected date window
    x_min = pd.Timestamp(date_from) if date_from else None
    x_max = pd.Timestamp(date_to) + pd.Timedelta(days=1) if date_to else None

    fig.update_xaxes(
        gridcolor="rgba(128,128,128,0.15)", zeroline=False,
        showspikes=True, spikecolor="#445", spikethickness=1,
        range=[x_min, x_max] if x_min and x_max else None,
    )
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.15)", zeroline=False)
    fig.update_yaxes(title_text="Equity ($)",    row=1, col=1, tickprefix="$")
    fig.update_yaxes(title_text="Peak DD ($)",   row=2, col=1, tickprefix="$")
    fig.update_yaxes(title_text="Daily P&L ($)", row=3, col=1, tickprefix="$")
    for ann in fig.layout.annotations:
        ann.font.size  = 11
        ann.font.color = "#6C7A8D"
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Monthly HTML table  (supports $ or %)
# ─────────────────────────────────────────────────────────────────────────────
def _monthly_html(pivot: pd.DataFrame, mode: str = "$") -> str:
    order = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec","YTD"]
    cols  = [c for c in order if c in pivot.columns]
    pivot = pivot[cols]
    rows  = []
    for year, row in pivot.iterrows():
        cells = [f"<td class='yc'>{year}</td>"]
        for col in cols:
            v = row.get(col, 0)
            if pd.isna(v) or v == 0:
                cls = "z"
                txt = "0"
            elif v > 0:
                cls = "p"
                txt = f"{v:,.2f}%" if mode == "%" else f"{v:,.2f}"
            else:
                cls = "n"
                txt = f"{v:,.2f}%" if mode == "%" else f"{v:,.2f}"
            cells.append(f"<td class='{cls}'>{txt}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    hdr = "<tr><th>Year</th>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>"
    return (
        "<style>.mt{width:100%;border-collapse:collapse;font-size:12px;"
        "font-family:'Courier New',monospace}"
        ".mt .p{color:#34C27A}.mt .n{color:#E05555}</style>"
        f"<table class='mt'><thead>{hdr}</thead><tbody>{''.join(rows)}</tbody></table>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Strategy comparison table
# ─────────────────────────────────────────────────────────────────────────────
def _strategy_table(eff_dfs: dict, deposit: float, lot_overrides: dict) -> pd.DataFrame:
    rows = []
    for label, df in eff_dfs.items():
        s   = _calc_stats(df, deposit)
        pf  = s.get("profit_factor", 0)
        mult = lot_overrides.get(label, 1.0)
        # Avg lot from original (unscaled) volume; if scaled show effective avg
        base_avg_lot = s.get("avg_lot", 0.0)
        rows.append({
            "Strategy":        label,
            "Lot ×":           round(mult, 2),
            "Avg Lot":         round(base_avg_lot, 4),
            "Trades":          s.get("num_trades", 0),
            "Net Profit ($)":  round(s.get("net_profit", 0), 2),
            "Win Rate (%)":    round(s.get("win_rate", 0), 2),
            "Profit Factor":   round(pf, 2) if pf != float("inf") else 999.0,
            "Max DD ($)":      round(s.get("max_dd", 0), 2),
            "Max DD (%)":      round(s.get("max_dd_pct", 0), 2),
            "Ret/DD":          round(s.get("ret_dd_ratio", 0), 2),
            "Avg Trade ($)":   round(s.get("avg_trade", 0), 2),
            "Stagnation (d)":  s.get("max_stagnation_days", 0),
            "Start":           str(s.get("start_date",""))[:10],
            "End":             str(s.get("end_date",""))[:10],
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────
def _init_state():
    for k, v in {
        "pb_uploaded_files": {},
        "pb_portfolios":     {},
        "pb_lot_overrides":  {},   # label → float multiplier
        "pb_deposit":        10000.0,
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────
def render():
    _init_state()

    # Card/stat styling injected globally by inject_theme_css() in app.py
    # Also expand multiselect tags so full strategy names are visible
    st.markdown("""<style>
    span[data-baseweb="tag"] { max-width: none !important; }
    span[data-baseweb="tag"] span { max-width: none !important; overflow: visible !important;
        white-space: normal !important; text-overflow: unset !important; }
    </style>""", unsafe_allow_html=True)

    st.markdown('<p class="pb-title">📊 Portfolio Builder</p>', unsafe_allow_html=True)
    st.markdown('<p class="pb-sub">Combine MT5 backtest reports into multi-strategy portfolios</p>',
                unsafe_allow_html=True)

    # ── Upload panel ─────────────────────────────────────────────────────────
    # [8] Show only htm/html/csv in the help text; type=None + manual filter
    #     because Streamlit on Windows sometimes chokes on type=["htm"]
    with st.expander("📂  Upload Strategy Reports",
                     expanded=not bool(st.session_state.pb_uploaded_files)):
        st.caption("Accepts: `.htm` · `.html` · `.csv`  —  other file types are ignored.")
        uploaded = st.file_uploader(
            "Select HTM or CSV files  (PNG and other files in the same folder are ignored)",
            type=None, accept_multiple_files=True, key="pb_uploader",
        )
        if uploaded:
            rejected = [f.name for f in uploaded
                        if not f.name.lower().endswith((".htm",".html",".csv"))]
            if rejected:
                st.warning(f"Ignored {len(rejected)} unsupported file(s): "
                           + ", ".join(rejected[:5])
                           + (" …" if len(rejected) > 5 else ""))
            uploaded = [f for f in uploaded
                        if f.name.lower().endswith((".htm",".html",".csv"))]
            for f in uploaded:
                stem = os.path.splitext(f.name)[0]
                if stem not in st.session_state.pb_uploaded_files:
                    df = _parse_uploaded(f)
                    if df is not None:
                        df = _ensure_columns(df, stem)
                        st.session_state.pb_uploaded_files[stem] = df
                        st.success(f"✅ **{stem}** — {len(df):,} trades")

        if st.session_state.pb_uploaded_files:
            st.markdown("**Loaded strategies:**")
            to_remove = []
            for label in list(st.session_state.pb_uploaded_files):
                c1, c2 = st.columns([6,1])
                c1.markdown(f"<span class='chip'>📈 {label}</span>",
                            unsafe_allow_html=True)
                if c2.button("✕", key=f"rm_{label}"):
                    to_remove.append(label)
            for k in to_remove:
                del st.session_state.pb_uploaded_files[k]
                st.session_state.pb_lot_overrides.pop(k, None)
                for pn in st.session_state.pb_portfolios:
                    if k in st.session_state.pb_portfolios[pn]:
                        st.session_state.pb_portfolios[pn].remove(k)
                st.rerun()

    strategy_dfs:  dict = st.session_state.pb_uploaded_files
    portfolios:    dict = st.session_state.pb_portfolios
    lot_overrides: dict = st.session_state.pb_lot_overrides

    if not strategy_dfs:
        st.info("Upload one or more strategy reports above to get started.")
        return

    # Effective DFs (with lot scaling applied)
    eff_dfs = _get_effective_dfs(strategy_dfs, lot_overrides)

    # ── View selector + deposit ───────────────────────────────────────────────
    view_options = (["Portfolio (all)"]
                    + [f"Portfolio: {p}" for p in portfolios]
                    + list(strategy_dfs.keys()))
    label_map = {"Portfolio (all)": "Portfolio (all)"}
    for p in portfolios:   label_map[f"Portfolio: {p}"] = p
    for s in strategy_dfs: label_map[s] = s

    sc1, sc2 = st.columns([4, 2])
    view_sel = sc1.selectbox("View", view_options, key="pb_view_sel")
    deposit  = sc2.number_input("Initial Deposit ($)", min_value=100.0,
                                 max_value=10_000_000.0,
                                 value=st.session_state.pb_deposit,
                                 step=1000.0, format="%.2f",
                                 key="pb_deposit_input")
    st.session_state.pb_deposit = deposit

    view_mode = label_map.get(view_sel, view_sel)
    df, active_label = _get_active_df(view_mode, eff_dfs, portfolios, deposit)

    # ── Shared date-range slider (used by Overview + Trades) ───────────────────────────
    import datetime as _dt
    _has_dates = not df.empty and "close_time" in df.columns
    if _has_dates:
        _all_dates = pd.to_datetime(df["close_time"]).dt.tz_localize(None).dropna()
        _gmin = _all_dates.min().date()
        _gmax = _all_dates.max().date()
        _total_days = (_gmax - _gmin).days
        _step = max(1, _total_days // 500)
        _date_options = [_gmin + _dt.timedelta(days=i)
                         for i in range(0, _total_days + 1, _step)]
        if _date_options[-1] != _gmax:
            _date_options.append(_gmax)
    else:
        _gmin = _gmax = None
        _date_options = []

    def _date_slider(key_prefix):
        if not _has_dates or _gmin == _gmax or len(_date_options) < 2:
            return _gmin, _gmax
        skey = f"{key_prefix}_dslider"
        # Use existing session state value if valid, otherwise default to full range
        existing = st.session_state.get(skey)
        if (existing and isinstance(existing, (list, tuple)) and len(existing) == 2
                and existing[0] in _date_options and existing[1] in _date_options):
            default_val = (existing[0], existing[1])
        else:
            default_val = (_gmin, _gmax)
        sel = st.select_slider(
            "Date range",
            options=_date_options,
            value=default_val,
            format_func=lambda d: d.strftime("%d %b %Y"),
            key=skey,
        )
        return sel[0], sel[1]


    # ── Overview filters (only shown for multi-strategy views) ───────────────
    # Determine if this is a portfolio/all view with multiple strategies
    _is_multi = view_mode in ("Portfolio (all)",) or view_mode in portfolios
    ov_df = df  # default — filtered below if _is_multi

    if not df.empty:
        ov_date_from, ov_date_to = _date_slider("pb_ov")

        strat_labels = sorted(df["_strategy"].dropna().unique().tolist()) \
            if "_strategy" in df.columns else []
        sym_labels = sorted(df["symbol"].dropna().unique().tolist()) \
            if "symbol" in df.columns else []

        if _is_multi:
            # EA filter (file level)
            ea_labels = sorted(df["_ea"].dropna().unique().tolist()) \
                if "_ea" in df.columns else strat_labels
            fc1, fc2, fc3 = st.columns(3)
            sel_eas = fc1.multiselect(
                "Filter EA",
                ea_labels, default=ea_labels, key="pb_ov_ea",
                help="Filter by uploaded file (EA)",
            )
            # Strategy filter — cascades from EA selection
            if "_ea" in df.columns and sel_eas:
                strat_labels_filtered = sorted(
                    df[df["_ea"].isin(sel_eas)]["strategy"].dropna().unique().tolist()
                ) if "strategy" in df.columns else strat_labels
            else:
                strat_labels_filtered = strat_labels
            sel_strats_raw = fc2.multiselect(
                "Filter Strategy",
                strat_labels_filtered, default=strat_labels_filtered, key="pb_ov_strat",
                help="Filter by strategy (comment) within selected EAs",
            )
            sel_syms = fc3.multiselect(
                "Filter symbols",
                sym_labels, default=sym_labels, key="pb_ov_sym",
            )
        else:
            sel_strats_raw = strat_labels
            sel_syms = sym_labels

        ov_df = df.copy()
        if "close_time" in ov_df.columns and ov_date_from and ov_date_to:
            ct = pd.to_datetime(ov_df["close_time"]).dt.tz_localize(None)
            ov_df = ov_df[
                (ct >= pd.Timestamp(ov_date_from)) &
                (ct <= pd.Timestamp(ov_date_to) + pd.Timedelta(days=1))
            ]
        if sel_strats_raw and "strategy" in ov_df.columns:
            ov_df = ov_df[ov_df["strategy"].isin(sel_strats_raw)]
        if sel_syms and "symbol" in ov_df.columns:
            ov_df = ov_df[ov_df["symbol"].isin(sel_syms)]

    stats = _calc_stats(ov_df, deposit)

    # ── Tabs ─────────────────────────────────────────────────────────────────
    tab_ov, tab_tr, tab_eq, tab_st, tab_wi, tab_pf = st.tabs([
        "📋 Overview", "📜 Trades", "📈 Equity Chart",
        "📊 Strategies", "🔧 What-If", "🗂 Portfolios",
    ])

    # ═════════════════════════════════════════════════════════════════════════
    # OVERVIEW
    # ═════════════════════════════════════════════════════════════════════════
    with tab_ov:
        if not stats:
            st.warning("No data for selected view.")
        else:
            np_ = stats.get("net_profit", 0)
            nc  = "pos" if np_ >= 0 else "neg"

            def card(label, val, sub, cls="neutral"):
                st.markdown(
                    f'<div class="stat-card"><div class="stat-label">{label}</div>'
                    f'<div class="stat-value {cls}">{val}</div>'
                    f'<div class="stat-sub">{sub}</div></div>',
                    unsafe_allow_html=True)

            c1,c2,c3,c4,c5 = st.columns(5)
            with c1: card("Total Profit",  f"${np_:,.2f}",
                          f"{stats.get('num_trades',0):,} trades", nc)
            with c2: card("Win Rate",      f"{stats.get('win_rate',0):.2f}%",
                          f"{stats.get('win_count',0)}W / {stats.get('loss_count',0)}L")
            with c3:
                pf = stats.get("profit_factor", 0)
                card("Profit Factor", f"{pf:.2f}" if pf != float("inf") else "∞",
                     "Gross P / Gross L")
            with c4: card("Max Drawdown",  f"${stats.get('max_dd',0):,.2f}",
                          f"{stats.get('max_dd_pct',0):.2f}%", "neg")
            with c5: card("Return / DD",   f"{stats.get('ret_dd_ratio',0):.2f}",
                          "Net profit / max DD")

            st.markdown("<br>", unsafe_allow_html=True)
            r1,r2,r3,r4,r5 = st.columns(5)
            with r1: card("Yearly Avg",    f"${stats.get('yearly_avg_profit',0):,.2f}", "Annual")
            with r2: card("Monthly Avg",   f"${stats.get('monthly_avg_profit',0):,.2f}", "Per month")
            with r3: card("CAGR",          f"{stats.get('cagr',0)*100:.2f}%", "Compound annual")
            with r4:
                at = stats.get("avg_trade",0)
                card("Avg Trade", f"${at:,.2f}", "Per trade", "pos" if at>=0 else "neg")
            with r5: card("Max Stagnation",f"{stats.get('max_stagnation_days',0)}d",
                          "Days w/o new high")

            st.markdown("<br>", unsafe_allow_html=True)
            lc, rc = st.columns(2)

            def kv(key, val, cls=""):
                st.markdown(
                    f'<div class="kv-row"><span class="kk">{key}</span>'
                    f'<span class="kv-v {cls}">{val}</span></div>',
                    unsafe_allow_html=True)

            with lc:
                st.markdown('<div class="sh">Strategy</div>', unsafe_allow_html=True)
                kv("# Trades",     f"{stats.get('num_trades',0):,}")
                kv("Gross Profit", f"${stats.get('gross_profit',0):,.2f}", "pos")
                kv("Gross Loss",   f"${stats.get('gross_loss',0):,.2f}",   "neg")
                kv("Avg Win",      f"${stats.get('avg_win',0):,.2f}",      "pos")
                kv("Avg Loss",     f"${stats.get('avg_loss',0):,.2f}",     "neg")
                kv("W/L Ratio",    f"{stats.get('win_count',0)} / {stats.get('loss_count',0)}")
            with rc:
                st.markdown('<div class="sh">Risk</div>', unsafe_allow_html=True)
                kv("Max Consec Wins",   str(stats.get("max_consec_wins",0)),   "pos")
                kv("Max Consec Losses", str(stats.get("max_consec_losses",0)), "neg")
                kv("Max DD $",     f"${stats.get('max_dd',0):,.2f}",           "neg")
                kv("Max DD %",     f"{stats.get('max_dd_pct',0):.2f}%",        "neg")
                kv("Start Date",   str(stats.get("start_date","—"))[:10])
                kv("End Date",     str(stats.get("end_date","—"))[:10])

            # [1] Monthly table toggle $ / %
            if "monthly_table_dollar" in stats:
                st.markdown("<br>", unsafe_allow_html=True)
                mt_col1, mt_col2 = st.columns([3, 1])
                mt_col1.markdown('<div class="sh">Monthly Performance</div>',
                                 unsafe_allow_html=True)
                mt_mode = mt_col2.radio("Unit", ["$", "%"],
                                        horizontal=True, key="pb_mt_mode")
                tbl_key = "monthly_table_dollar" if mt_mode == "$" else "monthly_table_pct"
                st.markdown(_monthly_html(stats[tbl_key], mt_mode),
                            unsafe_allow_html=True)

    # ═════════════════════════════════════════════════════════════════════════
    # TRADES
    # ═════════════════════════════════════════════════════════════════════════
    with tab_tr:
        if df.empty:
            st.info("No trades in selected view.")
        else:
            tr_date_from, tr_date_to = _date_slider("pb_tr")

            fc1, fc2, fc3, fc4, fc5 = st.columns(5)
            all_eas    = sorted(df["_ea"].dropna().unique().tolist())       if "_ea"       in df.columns else []
            all_syms   = sorted(df["symbol"].dropna().unique().tolist())    if "symbol"    in df.columns else []
            all_types  = sorted(df["type"].dropna().unique().tolist())      if "type"      in df.columns else []
            filt_ea    = fc1.multiselect("EA",        all_eas,   default=all_eas,   key="pb_tea")
            # Cascade strategies from EA filter
            if filt_ea and "_ea" in df.columns:
                avail_strats = sorted(df[df["_ea"].isin(filt_ea)]["strategy"].dropna().unique().tolist()) \
                    if "strategy" in df.columns else []
            else:
                avail_strats = sorted(df["strategy"].dropna().unique().tolist()) \
                    if "strategy" in df.columns else []
            filt_strat = fc2.multiselect("Strategy",  avail_strats, default=avail_strats, key="pb_tst")
            filt_sym   = fc3.multiselect("Symbol",    all_syms,   default=all_syms,   key="pb_ts")
            filt_type  = fc4.multiselect("Direction", all_types,  default=all_types,  key="pb_tt")
            result_f   = fc5.selectbox("Result", ["All","Wins only","Losses only"], key="pb_tr")

            view = df.copy()
            if "close_time" in view.columns and tr_date_from and tr_date_to:
                ct = pd.to_datetime(view["close_time"]).dt.tz_localize(None)
                view = view[
                    (ct >= pd.Timestamp(tr_date_from)) &
                    (ct <= pd.Timestamp(tr_date_to) + pd.Timedelta(days=1))
                ]
            if filt_ea    and "_ea"      in view.columns: view = view[view["_ea"].isin(filt_ea)]
            if filt_strat and "strategy" in view.columns: view = view[view["strategy"].isin(filt_strat)]
            if filt_sym   and "symbol"   in view.columns: view = view[view["symbol"].isin(filt_sym)]
            if filt_type  and "type"     in view.columns: view = view[view["type"].isin(filt_type)]
            if result_f == "Wins only":    view = view[view["net_profit"] > 0]
            elif result_f == "Losses only": view = view[view["net_profit"] <= 0]

            keep   = [c for c in ["_strategy","symbol","type","open_time","open_price",
                                   "close_time","close_price","volume","net_profit","comment"]
                      if c in view.columns]
            rename = {"_strategy":"Strategy","symbol":"Symbol","type":"Type",
                      "open_time":"Open Time","open_price":"Open Price",
                      "close_time":"Close Time","close_price":"Close Price",
                      "volume":"Volume","net_profit":"P/L ($)","comment":"Comment"}
            ddf = view[keep].rename(columns=rename).copy()
            for dc in ["Open Time","Close Time"]:
                if dc in ddf.columns:
                    ddf[dc] = pd.to_datetime(ddf[dc], errors="coerce").dt.strftime("%d.%m.%Y %H:%M")

            # [6] Format all numeric columns to 2dp
            num_cols = ddf.select_dtypes(include="number").columns.tolist()
            fmt_dict = {c: "{:.2f}" for c in num_cols}

            # Compact stats bar above trade list
            tr_stats = _calc_stats(view, deposit)
            if tr_stats:
                st.caption("Stats reflect the filtered trade list below — does not affect Overview, Equity Chart or Strategies tabs.")
                ts1,ts2,ts3,ts4,ts5,ts6 = st.columns(6)
                _np = tr_stats.get("net_profit",0)
                ts1.metric("Trades",        f"{tr_stats.get('num_trades',0):,}")
                ts2.metric("Net Profit",    f"${_np:,.2f}")
                ts3.metric("Win Rate",      f"{tr_stats.get('win_rate',0):.2f}%")
                ts4.metric("Profit Factor", f"{tr_stats.get('profit_factor',0):.2f}"
                           if tr_stats.get('profit_factor',0) != float('inf') else "∞")
                ts5.metric("Max DD",        f"${tr_stats.get('max_dd',0):,.2f}")
                ts6.metric("Avg Trade",     f"${tr_stats.get('avg_trade',0):.2f}")
                st.markdown("")

            st.caption(f"{len(ddf):,} trades  ·  use ⛶ to expand full screen")

            def _hl(val):
                if not isinstance(val, (int,float)): return ""
                import os, re as _re
                _cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "config.toml")
                _light = False
                if os.path.isfile(_cfg):
                    _m = _re.search(r'base\s*=\s*"([^"]*)"', open(_cfg).read())
                    if _m: _light = _m.group(1) == "light"
                if _light:
                    if val > 0: return "background-color:rgba(52,194,122,0.15)"
                    if val < 0: return "background-color:rgba(220,50,50,0.12)"
                else:
                    if val > 0: return "background-color:#1A3A26"
                    if val < 0: return "background-color:#3A1A1A"
                return ""

            # No fixed height — Streamlit will size to content on the page
            # and properly fill the screen when the fullscreen icon is clicked.
            st.dataframe(
                ddf.style
                   .format(fmt_dict)
                   .map(_hl, subset=["P/L ($)"] if "P/L ($)" in ddf.columns else []),
                use_container_width=True,
            )
            buf = io.StringIO()
            ddf.to_csv(buf, index=False)
            st.download_button("⬇️ Export CSV", buf.getvalue(),
                               file_name=f"trades_{active_label}.csv", mime="text/csv")

    # ═════════════════════════════════════════════════════════════════════════
    # EQUITY CHART
    # ═════════════════════════════════════════════════════════════════════════
    with tab_eq:
        if df.empty or "close_time" not in df.columns:
            st.info("No time-series data available.")
        else:
            # [8] Line mode options
            ctl1, ctl2, ctl3 = st.columns([3, 2, 2])
            chart_view = ctl1.radio(
                "Lines",
                ["Portfolio", "By EA", "By Strategy", "EA + Strategy"],
                horizontal=True, key="pb_cv",
            )
            smooth_window = ctl2.slider("Smoothing", 1, 50, 1, key="pb_sm",
                                        help="Rolling-average window (trades). 1 = raw.")
            show_stag = ctl3.checkbox("Show stagnation band", value=True, key="pb_stag")

            # Date range slider
            date_from, date_to = _date_slider("pb_eq")

            # EA filter + cascading strategy filter
            all_eas_eq = sorted(df["_ea"].dropna().unique().tolist()) if "_ea" in df.columns else list(eff_dfs.keys())
            sel_eas_eq = st.multiselect("Filter EA", all_eas_eq, default=all_eas_eq, key="pb_eq_ea")

            if chart_view in ("By Strategy", "EA + Strategy"):
                if sel_eas_eq and "_ea" in df.columns and "strategy" in df.columns:
                    avail_strats_eq = sorted(df[df["_ea"].isin(sel_eas_eq)]["strategy"].dropna().unique().tolist())
                else:
                    avail_strats_eq = sorted(df["strategy"].dropna().unique().tolist()) if "strategy" in df.columns else []
                sel_strats_eq = st.multiselect("Filter Strategy", avail_strats_eq, default=avail_strats_eq, key="pb_eq_strat")
            else:
                sel_strats_eq = []

            # Build per-EA and per-strategy dfs for charting
            # Per-EA: combine all trades for that EA filename
            ea_dfs = {}
            for ea in all_eas_eq:
                if ea not in sel_eas_eq:
                    continue
                ea_trades = df[df["_ea"] == ea] if "_ea" in df.columns else pd.DataFrame()
                if not ea_trades.empty:
                    ea_trades = ea_trades.sort_values("close_time").reset_index(drop=True)
                    ea_dfs[ea] = ea_trades

            # Per-strategy: combine all trades sharing the same strategy comment
            strat_dfs = {}
            if "strategy" in df.columns:
                for strat in (sel_strats_eq if sel_strats_eq else df["strategy"].dropna().unique()):
                    mask = df["strategy"] == strat
                    if sel_eas_eq and "_ea" in df.columns:
                        mask &= df["_ea"].isin(sel_eas_eq)
                    s_trades = df[mask]
                    if not s_trades.empty:
                        s_trades = s_trades.sort_values("close_time").reset_index(drop=True)
                        strat_dfs[strat] = s_trades

            # Determine what to pass to the chart builder
            if chart_view == "By EA":
                chart_eff_dfs  = ea_dfs
                sel_strats     = list(ea_dfs.keys())
                chart_cv       = "Individual"
            elif chart_view == "By Strategy":
                chart_eff_dfs  = strat_dfs
                sel_strats     = list(strat_dfs.keys())
                chart_cv       = "Individual"
            elif chart_view == "EA + Strategy":
                chart_eff_dfs  = {**ea_dfs, **strat_dfs}
                sel_strats     = list(ea_dfs.keys()) + list(strat_dfs.keys())
                chart_cv       = "Portfolio+Individual"
            else:  # Portfolio
                chart_eff_dfs  = eff_dfs
                sel_strats     = list(eff_dfs.keys())
                chart_cv       = "Portfolio"

            # Combined df for portfolio line
            chart_df = df
            if chart_view == "EA + Strategy" and ea_dfs:
                chart_df = _combine(ea_dfs, deposit)

            cv_map = {
                "Portfolio":    "Portfolio",
                "By EA":        "Individual",
                "By Strategy":  "Individual",
                "EA + Strategy":"Portfolio+Individual",
            }
            fig = _build_equity_chart(
                chart_df, deposit, chart_eff_dfs, portfolios, active_label,
                chart_view=cv_map[chart_view],
                smooth_window=smooth_window,
                show_stagnation=show_stag,
                date_from=date_from, date_to=date_to,
                selected_strategies=sel_strats,
            )
            st.plotly_chart(fig, use_container_width=True)

    # ═════════════════════════════════════════════════════════════════════════
    # STRATEGIES TABLE  [2] lot size, no CAGR  [3] smoothing  [4] taller chart
    # ═════════════════════════════════════════════════════════════════════════
    with tab_st:
        st.markdown("##### All Strategies — Performance Summary")
        # Apply same date range as Overview
        eff_dfs_filtered = {}
        for _lbl, _sdf in eff_dfs.items():
            if "close_time" in _sdf.columns and ov_date_from and ov_date_to:
                _ct = pd.to_datetime(_sdf["close_time"]).dt.tz_localize(None)
                eff_dfs_filtered[_lbl] = _sdf[
                    (_ct >= pd.Timestamp(ov_date_from)) &
                    (_ct <= pd.Timestamp(ov_date_to) + pd.Timedelta(days=1))
                ]
            else:
                eff_dfs_filtered[_lbl] = _sdf
        tdf = _strategy_table(eff_dfs_filtered, deposit, lot_overrides)
        if tdf.empty:
            st.info("No strategies loaded.")
        else:
            def _cc(val, low=0):
                if not isinstance(val, (int,float)): return ""
                return "color:#34C27A" if val > low else "color:#E05555" if val < low else ""

            # [6] 2dp formatting for all numeric columns
            num_cols_t = tdf.select_dtypes(include="number").columns.tolist()
            fmt_t = {c: "{:.2f}" for c in num_cols_t if c != "Trades"}
            fmt_t["Trades"] = "{:.0f}"

            styled = (
                tdf.style
                .format(fmt_t)
                .map(_cc,                     subset=["Net Profit ($)","Avg Trade ($)"])
                .map(lambda v: _cc(v, 1.0),   subset=["Profit Factor"])
                .map(lambda v: "color:#E05555"
                     if isinstance(v,(int,float)) and v < 0 else "",
                     subset=["Max DD ($)","Max DD (%)"])
            )
            st.dataframe(styled, use_container_width=True, hide_index=True)

            # Controls row
            st.markdown("##### Equity Curves")
            ctl1, ctl2, ctl3, ctl4 = st.columns([2, 2, 2, 2])
            sc_smooth    = ctl1.slider("Curve smoothing", 1, 50, 1, key="pb_st_smooth",
                                       help="Rolling-average window (trades).")
            st_curve_grp = ctl2.radio("Group by", ["EA", "Strategy"], horizontal=True, key="pb_st_grp",
                                      help="EA = one line per file · Strategy = one line per comment")
            show_st_stag = ctl3.toggle("Show stagnation bands", value=False,
                                       key="pb_st_show_stag",
                                       help="Highlight max stagnation period per strategy in matching colour")

            # Build the series to plot
            if st_curve_grp == "Strategy" and "strategy" in df.columns:
                # One series per unique strategy comment across all loaded files
                _st_series = {}
                for _strat in sorted(df["strategy"].dropna().unique()):
                    _s_df = df[df["strategy"] == _strat].copy()
                    if ov_date_from and ov_date_to and "close_time" in _s_df.columns:
                        _ct = pd.to_datetime(_s_df["close_time"]).dt.tz_localize(None)
                        _s_df = _s_df[(_ct >= pd.Timestamp(ov_date_from)) &
                                      (_ct <= pd.Timestamp(ov_date_to) + pd.Timedelta(days=1))]
                    if not _s_df.empty:
                        _st_series[_strat] = _s_df.sort_values("close_time").reset_index(drop=True)
            else:
                _st_series = eff_dfs_filtered  # one series per uploaded file

            sf = go.Figure()
            sf.update_layout(
                height=500,
                margin=dict(l=40, r=20, t=40, b=10),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                legend=dict(orientation="h", y=1.08, font=dict(size=10)),
                hovermode="x unified",
                hoverlabel=dict(namelength=-1, font=dict(size=12)),
            )
            sf.update_xaxes(gridcolor="rgba(128,128,128,0.15)", zeroline=False)
            sf.update_yaxes(gridcolor="rgba(128,128,128,0.15)", zeroline=False, tickprefix="$")

            for i, (lbl, sdf) in enumerate(_st_series.items()):
                if "close_time" not in sdf.columns or "net_profit" not in sdf.columns:
                    continue
                color = COLORS[i % len(COLORS)]
                sdf_s = sdf.sort_values("close_time")
                eq    = deposit + sdf_s["net_profit"].cumsum()
                eq_s  = _smooth(eq.reset_index(drop=True), sc_smooth)
                sf.add_trace(go.Scatter(
                    x=sdf_s["close_time"].values, y=eq_s,
                    name=lbl, mode="lines",
                    line=dict(color=color, width=1.5),
                    hovertemplate=f"<b>{lbl}</b><br>%{{x|%d %b %Y}}: $%{{y:,.2f}}<extra></extra>",
                ))

                # Stagnation band per strategy in matching colour
                if show_st_stag:
                    eq_ts = sdf_s[["close_time","net_profit"]].dropna().copy()
                    eq_ts["cum"] = deposit + eq_ts["net_profit"].cumsum()
                    if not eq_ts.empty:
                        peak = float(eq_ts["cum"].iloc[0])
                        stag_start = eq_ts["close_time"].iloc[0]
                        max_days = 0
                        best_s = stag_start
                        best_e = stag_start
                        for _, r in eq_ts.iterrows():
                            if float(r["cum"]) > peak:
                                days = (r["close_time"] - stag_start).days
                                if days > max_days:
                                    max_days = days
                                    best_s = stag_start
                                    best_e = r["close_time"]
                                peak = float(r["cum"])
                                stag_start = r["close_time"]
                        if max_days > 0:
                            # Convert hex to rgba with low opacity
                            hex_c = color.lstrip("#")
                            if len(hex_c) == 6:
                                r_c = int(hex_c[0:2], 16)
                                g_c = int(hex_c[2:4], 16)
                                b_c = int(hex_c[4:6], 16)
                                fill_color = f"rgba({r_c},{g_c},{b_c},0.12)"
                                ann_color  = color
                            else:
                                fill_color = "rgba(255,160,80,0.12)"
                                ann_color  = color
                            sf.add_vrect(
                                x0=best_s, x1=best_e,
                                fillcolor=fill_color, line_width=1,
                                line_color=f"rgba({r_c},{g_c},{b_c},0.3)" if len(hex_c)==6 else color,
                                annotation_text=f"{lbl.split()[0]}… {max_days}d",
                                annotation_position="top left",
                                annotation_font_size=9,
                                annotation_font_color=ann_color,
                            )

            st.plotly_chart(sf, use_container_width=True)

    # ═════════════════════════════════════════════════════════════════════════
    # WHAT-IF  [5] lot-size scaling per strategy
    # ═════════════════════════════════════════════════════════════════════════
    with tab_wi:
        st.markdown("##### What-If: Lot Size Adjustment")
        st.caption(
            "Enter a target lot size for each strategy. The tool calculates the "
            "multiplier automatically from the strategy's average lot size. "
            "Changes apply everywhere (Overview, Equity Chart, Strategies table)."
        )

        changed = False
        for label in list(strategy_dfs.keys()):
            orig_df    = strategy_dfs[label]
            orig_stats = _calc_stats(orig_df, deposit)
            orig_dd    = orig_stats.get("max_dd", 0)
            orig_avg_lot = orig_stats.get("avg_lot", 0.0)
            current_mult = lot_overrides.get(label, 1.0)

            # Derive the currently displayed lot size from the multiplier
            # (avg_lot is from original unscaled data, so effective = orig * mult)
            current_lot = round(orig_avg_lot * current_mult, 4) if orig_avg_lot else current_mult

            wi_c1, wi_c2, wi_c3, wi_c4 = st.columns([3, 1, 1, 1])
            wi_c1.markdown(f"**{label}**")
            wi_c2.markdown(
                f"<small style='color:#6C7A8D'>Avg lot<br>"
                f"<b style='color:#CDD6F4'>{orig_avg_lot:.4f}</b></small>",
                unsafe_allow_html=True)
            wi_c3.markdown(
                f"<small style='color:#6C7A8D'>Current ×<br>"
                f"<b style='color:#CDD6F4'>{current_mult:.4f}</b></small>",
                unsafe_allow_html=True)

            new_lot = wi_c4.number_input(
                "Target lot size", min_value=0.0001, max_value=9999.0,
                value=float(current_lot) if current_lot > 0 else float(orig_avg_lot) if orig_avg_lot else 0.01,
                step=0.01, format="%.4f",
                key=f"pb_wi_{label}",
                label_visibility="collapsed",
                help="Enter the lot size you want for this strategy",
            )

            # Compute new multiplier from target lot / original avg lot
            if orig_avg_lot and orig_avg_lot > 0:
                new_mult = new_lot / orig_avg_lot
            else:
                new_mult = new_lot  # fallback: treat as direct multiplier

            new_mult = round(new_mult, 6)
            if abs(new_mult - current_mult) > 1e-9:
                st.session_state.pb_lot_overrides[label] = new_mult
                changed = True

            # Before / after metrics
            scaled_df    = _scale_df(orig_df, new_mult)
            scaled_stats = _calc_stats(scaled_df, deposit)
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("Net Profit",    f"${scaled_stats.get('net_profit',0):,.2f}",
                      delta=f"{scaled_stats.get('net_profit',0)-orig_stats.get('net_profit',0):+.2f}")
            s2.metric("Max DD",        f"${scaled_stats.get('max_dd',0):,.2f}",
                      delta=f"{scaled_stats.get('max_dd',0)-orig_dd:+.2f}")
            s3.metric("Profit Factor", f"{scaled_stats.get('profit_factor',0):.2f}")
            s4.metric("Win Rate",      f"{scaled_stats.get('win_rate',0):.2f}%")
            st.markdown("---")

        if changed:
            st.rerun()

        if any(v != 1.0 for v in lot_overrides.values()):
            if st.button("Reset all lot sizes to original", key="pb_wi_reset"):
                st.session_state.pb_lot_overrides = {}
                st.rerun()

    # ═════════════════════════════════════════════════════════════════════════
    # PORTFOLIOS MANAGER
    # ═════════════════════════════════════════════════════════════════════════
    with tab_pf:
        st.markdown("##### Custom Portfolios")
        st.caption("Named portfolios appear in the View dropdown. "
                   "Lot-size overrides from the What-If tab are reflected in portfolio stats.")

        with st.expander("➕  Create new portfolio",
                         expanded=not bool(portfolios)):
            pname   = st.text_input("Portfolio name", key="pb_pname",
                                    placeholder="e.g.  Gold Strategies")
            members = st.multiselect("Strategies to include",
                                     list(strategy_dfs.keys()), key="pb_pmembers")
            if st.button("Save portfolio", key="pb_psave"):
                if not pname.strip():
                    st.warning("Enter a portfolio name.")
                elif not members:
                    st.warning("Select at least one strategy.")
                else:
                    st.session_state.pb_portfolios[pname.strip()] = list(members)
                    st.success(f"✅ **{pname.strip()}** saved.")
                    st.rerun()

        if not portfolios:
            st.info("No custom portfolios yet.")
        else:
            for pname, members in list(portfolios.items()):
                with st.expander(f"📁  {pname}  ({len(members)} strategies)"):
                    for m in members:
                        mult = lot_overrides.get(m, 1.0)
                        tag  = "✅" if m in strategy_dfs else "⚠️ not loaded"
                        mult_str = f"  ×{mult:.2f}" if mult != 1.0 else ""
                        st.markdown(f"- {m}  {tag}{mult_str}")

                    new_members = st.multiselect(
                        "Edit members", list(strategy_dfs.keys()),
                        default=[m for m in members if m in strategy_dfs],
                        key=f"pb_edit_{pname}",
                    )
                    ec1, ec2 = st.columns(2)
                    if ec1.button("Update", key=f"pb_upd_{pname}"):
                        st.session_state.pb_portfolios[pname] = list(new_members)
                        st.success("Updated.")
                        st.rerun()
                    if ec2.button("🗑 Delete", key=f"pb_del_{pname}"):
                        del st.session_state.pb_portfolios[pname]
                        st.rerun()

                    pm_dfs = {k: eff_dfs[k] for k in members if k in eff_dfs}
                    if pm_dfs:
                        pdf   = _combine(pm_dfs, deposit)
                        pstat = _calc_stats(pdf, deposit)
                        mc1,mc2,mc3,mc4 = st.columns(4)
                        mc1.metric("Net Profit",    f"${pstat.get('net_profit',0):,.2f}")
                        mc2.metric("Win Rate",      f"{pstat.get('win_rate',0):.2f}%")
                        mc3.metric("Profit Factor", f"{pstat.get('profit_factor',0):.2f}")
                        mc4.metric("Max DD",        f"${pstat.get('max_dd',0):,.2f}")