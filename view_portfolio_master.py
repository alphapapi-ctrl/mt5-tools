"""
view_portfolio_master.py — Portfolio Master
Automated portfolio construction with:
  - Composite weighted scoring (Ret/DD, Stability, Stagnation, Win Rate, Growth Quality)
  - Three search modes: Exhaustive | Greedy | Monte Carlo
  - Combination count estimate + runtime warning before run
  - Diversity bonus for multi-symbol / multi-session portfolios
  - Average portfolio correlation output metric
  - Conditional correlation (drawdown periods only)
  - Equity curve growth quality = slope × stability
  - Per-result correlation heatmap in detail expander
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from scipy import stats as scipy_stats
import io, importlib, sys, os, itertools, random, time
from datetime import timedelta

# ─────────────────────────────────────────────────────────────────────────────
# Parser
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
    df = df.copy()  # ensure we never mutate the original
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
# Full per-strategy statistics
# ─────────────────────────────────────────────────────────────────────────────
def _full_stats(df: pd.DataFrame, deposit: float, idx: int, custom_name: str) -> dict:
    s = {}
    if df.empty or "net_profit" not in df.columns:
        return s

    label   = df["_strategy"].iloc[0] if "_strategy" in df.columns else f"#{idx}"
    symbol  = df["symbol"].iloc[0] if "symbol" in df.columns else ""
    profits = df["net_profit"].fillna(0)

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

    if "commission" in df.columns:
        s["Commissions ($)"] = round(float(pd.to_numeric(df["commission"], errors="coerce").fillna(0).sum()), 2)
    else:
        s["Commissions ($)"] = 0.0

    eq = deposit + profits.cumsum()
    rm = eq.cummax()
    dd = eq - rm
    s["Max DD ($)"]   = round(float(dd.min()), 2)
    s["Max DD (%)"]   = round(float(dd.min() / deposit * 100), 2)
    s["Ret/DD"]       = round(s["Net Profit ($)"] / abs(s["Max DD ($)"]), 2) if s["Max DD ($)"] else 0.0

    if "close_time" in df.columns and "open_time" in df.columns:
        vc = df["close_time"].dropna(); vo = df["open_time"].dropna()
        if not vc.empty:
            start = vo.min() if not vo.empty else vc.min()
            end   = vc.max()
            days  = max((end - start).days, 1)
            yrs   = days / 365.25
            s["Annual Profit ($)"] = round(s["Net Profit ($)"] / yrs, 2)
            s["Annual Profit (%)"] = round(s["Net Profit ($)"] / deposit / yrs * 100, 2)
        else:
            s["Annual Profit ($)"] = s["Annual Profit (%)"] = 0.0
    else:
        s["Annual Profit ($)"] = s["Annual Profit (%)"] = 0.0



    if "close_time" in df.columns:
        eq_ts = df[["close_time","net_profit"]].dropna().sort_values("close_time").copy()
        if not eq_ts.empty:
            eq_ts["cum"]  = deposit + eq_ts["net_profit"].cumsum()
            eq_ts["date"] = eq_ts["close_time"].dt.date
            dly = eq_ts.groupby("date")["cum"].last().reset_index()
            total_days = max((dly["date"].iloc[-1] - dly["date"].iloc[0]).days, 1)
            peak = float(dly["cum"].iloc[0]); stag_start = dly["date"].iloc[0]; max_stag = 0
            for _, r in dly.iterrows():
                if float(r["cum"]) > peak: peak = float(r["cum"]); stag_start = r["date"]
                else: max_stag = max(max_stag, (r["date"] - stag_start).days)
            s["Stagnation (days)"] = max_stag
            s["Stagnation (%)"]    = round(max_stag / total_days * 100, 2)
        else:
            s["Stagnation (days)"] = 0; s["Stagnation (%)"] = 0.0
    else:
        s["Stagnation (days)"] = 0; s["Stagnation (%)"] = 0.0

    # Stability (R²) and Growth Quality (slope × R²)
    if len(eq) > 2:
        x = np.arange(len(eq))
        slope, intercept, r, p, se = scipy_stats.linregress(x, eq.values)
        r2 = float(r ** 2)
        s["Stability"]      = int(round(r2 * 100))          # 0-100
        # Normalise slope to per-trade return as % of deposit, then multiply by R²
        norm_slope = float(slope) / deposit * 100
        s["Growth Quality"] = int(round(norm_slope * r2 * 10000))  # whole number
    else:
        s["Stability"] = 0; s["Growth Quality"] = 0

    return s


# ─────────────────────────────────────────────────────────────────────────────
# Daily P&L and correlation helpers
# ─────────────────────────────────────────────────────────────────────────────
def _daily_pnl(df: pd.DataFrame) -> pd.Series:
    if df.empty or "close_time" not in df.columns or "net_profit" not in df.columns:
        return pd.Series(dtype=float)
    tmp = df[["close_time","net_profit"]].dropna().copy()
    tmp["date"] = pd.to_datetime(tmp["close_time"]).dt.tz_localize(None).dt.normalize()
    return tmp.groupby("date")["net_profit"].sum()


def _correlation_matrix(dfs: dict) -> pd.DataFrame:
    series  = {lbl: _daily_pnl(df) for lbl, df in dfs.items()}
    aligned = pd.DataFrame(series).fillna(0)
    return aligned.corr()


def _conditional_correlation(dfs: dict, deposit: float) -> pd.DataFrame:
    """Correlation computed only on days where the combined portfolio is in drawdown."""
    series  = {lbl: _daily_pnl(df) for lbl, df in dfs.items()}
    aligned = pd.DataFrame(series).fillna(0)
    combined_daily = aligned.sum(axis=1)
    cum = deposit + combined_daily.cumsum()
    in_dd = cum < cum.cummax()
    dd_days = aligned[in_dd]
    if len(dd_days) < 5:
        return aligned.corr()   # fallback if not enough drawdown days
    return dd_days.corr()


def _portfolio_exceeds_corr(members: list, corr_matrix: pd.DataFrame, max_corr: float) -> bool:
    for a, b in itertools.combinations(members, 2):
        if a in corr_matrix.index and b in corr_matrix.columns:
            if abs(corr_matrix.loc[a, b]) > max_corr:
                return True
    return False


def _avg_correlation(members: list, corr_matrix: pd.DataFrame) -> float:
    """Average pairwise correlation across all member pairs."""
    pairs = list(itertools.combinations(members, 2))
    if not pairs:
        return 0.0
    vals = []
    for a, b in pairs:
        if a in corr_matrix.index and b in corr_matrix.columns:
            vals.append(abs(corr_matrix.loc[a, b]))
    return round(float(np.mean(vals)), 4) if vals else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Diversity bonus
# ─────────────────────────────────────────────────────────────────────────────
def _diversity_bonus(members: list, dfs: dict) -> float:
    """
    Returns a bonus score 0.0–1.0 based on:
      - Symbol diversity (unique symbols / n_members)
      - Session diversity (strategies trading at different hours)
    """
    if len(members) < 2:
        return 0.0

    symbols = []
    hour_sets = []
    for m in members:
        df = dfs.get(m)
        if df is None: continue
        # Symbol
        if "symbol" in df.columns:
            sym = str(df["symbol"].iloc[0]).split(".")[0].upper()
            symbols.append(sym)
        # Trading hours — get modal hour of closes
        if "close_time" in df.columns:
            hrs = pd.to_datetime(df["close_time"], errors="coerce").dt.hour.dropna()
            if not hrs.empty:
                hour_sets.append(set(hrs.value_counts().head(6).index.tolist()))

    sym_score = len(set(symbols)) / len(members) if symbols else 0.0

    session_score = 0.0
    if len(hour_sets) >= 2:
        overlaps = []
        for h1, h2 in itertools.combinations(hour_sets, 2):
            if h1 | h2:
                overlaps.append(len(h1 & h2) / len(h1 | h2))
        session_score = 1.0 - (sum(overlaps) / len(overlaps)) if overlaps else 0.0

    return int(round((sym_score * 0.6 + session_score * 0.4) * 100))  # 0-100


# ─────────────────────────────────────────────────────────────────────────────
# Composite scoring
# ─────────────────────────────────────────────────────────────────────────────
def _composite_score(full: dict, weights: dict, diversity: float, deposit: float) -> float:
    """
    Weighted composite score. Each metric is normalised before weighting.
    weights keys: ret_dd, stability, stagnation, win_rate, growth_quality, diversity
    """
    def _norm(val, low, high):
        if high == low: return 0.5
        return max(0.0, min(1.0, (val - low) / (high - low)))

    ret_dd   = full.get("Ret/DD", 0.0)
    stab     = full.get("Stability", 0.0)
    stag     = full.get("Stagnation (%)", 100.0)
    wr       = full.get("% Wins", 0.0)
    gq       = full.get("Growth Quality", 0.0)

    # Normalise each component (rough reasonable ranges)
    n_ret_dd = _norm(ret_dd,    0, 10)
    n_stab   = _norm(stab,      0, 1)
    n_stag   = _norm(100-stag,  0, 100)   # inverted: lower stagnation = higher score
    n_wr     = _norm(wr,        40, 90)
    n_gq     = _norm(gq,        0, 0.05)
    n_div    = _norm(diversity,  0, 1)

    score = (
        weights.get("ret_dd",       0.35) * n_ret_dd   +
        weights.get("stability",    0.25) * n_stab     +
        weights.get("stagnation",   0.20) * n_stag     +
        weights.get("win_rate",     0.10) * n_wr       +
        weights.get("growth_quality",0.05)* n_gq       +
        weights.get("diversity",    0.05) * n_div
    )
    return int(round(float(score) * 1000))


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio evaluation (single combo)
# ─────────────────────────────────────────────────────────────────────────────
def _evaluate_combo(members: list, dfs: dict, deposit: float,
                    weights: dict, corr_matrix: pd.DataFrame,
                    cond_corr_matrix: pd.DataFrame) -> dict:
    frames = [dfs[m].copy() for m in members if m in dfs]
    if not frames: return {}
    combined = pd.concat(frames, ignore_index=True)
    if "close_time" in combined.columns:
        combined = combined.sort_values("close_time").reset_index(drop=True)
    combined["_strategy"] = " + ".join(members)

    full      = _full_stats(combined, deposit, 0, " + ".join(members))
    diversity = _diversity_bonus(members, dfs)
    score     = _composite_score(full, weights, diversity, deposit)
    avg_corr  = _avg_correlation(members, corr_matrix)
    avg_cond  = _avg_correlation(members, cond_corr_matrix)

    return {
        "members":       members,
        "score":         score,
        "net_profit":    full.get("Net Profit ($)", 0.0),
        "max_dd":        full.get("Max DD ($)", 0.0),
        "ret_dd":        full.get("Ret/DD", 0.0),
        "stag_pct":      full.get("Stagnation (%)", 0.0),
        "stability":     full.get("Stability", 0.0),
        "growth_quality":full.get("Growth Quality", 0.0),
        "diversity":     diversity,
        "avg_corr":      avg_corr,
        "avg_cond_corr": avg_cond,
        "full_stats":    full,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Search modes
# ─────────────────────────────────────────────────────────────────────────────
def _search_exhaustive(labels, dfs, deposit, weights, min_s, max_s,
                       use_corr, corr_limit, corr_matrix, cond_corr_matrix,
                       max_results, prog_cb, cancelled=None):
    results = []
    total = sum(
        sum(1 for _ in itertools.combinations(labels, r))
        for r in range(min_s, max_s + 1)
    )
    done = 0
    for size in range(min_s, max_s + 1):
        for combo in itertools.combinations(labels, size):
            combo = list(combo)
            done += 1
            if done % 100 == 0:
                prog_cb(done, total, f"Exhaustive: {done:,} / {total:,}")
            if use_corr and corr_matrix is not None:
                if _portfolio_exceeds_corr(combo, corr_matrix, corr_limit):
                    continue
            if cancelled and cancelled(): break
            r = _evaluate_combo(combo, dfs, deposit, weights, corr_matrix, cond_corr_matrix)
            if r: results.append(r)
        if cancelled and cancelled(): break
    prog_cb(total, total, "Cancelled." if (cancelled and cancelled()) else "Done.")
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:max_results]


def _search_greedy(labels, dfs, deposit, weights, min_s, max_s,
                   use_corr, corr_limit, corr_matrix, cond_corr_matrix,
                   max_results, prog_cb, cancelled=None):
    """
    Greedy incremental build: start with best single strategy,
    repeatedly add the strategy that most improves the composite score.
    Runs once per starting strategy to explore diverse starting points.
    """
    results = []
    n = len(labels)
    total_starts = n
    for start_idx, seed in enumerate(labels):
        prog_cb(start_idx, total_starts, f"Greedy: seed {start_idx+1}/{total_starts}")
        current = [seed]
        # Grow until max_s
        while len(current) < max_s:
            best_score = -999
            best_add   = None
            for candidate in labels:
                if candidate in current: continue
                trial = current + [candidate]
                if use_corr and corr_matrix is not None:
                    if _portfolio_exceeds_corr(trial, corr_matrix, corr_limit):
                        continue
                r = _evaluate_combo(trial, dfs, deposit, weights, corr_matrix, cond_corr_matrix)
                if r and r["score"] > best_score:
                    best_score = r["score"]
                    best_add   = candidate
            if best_add is None: break
            current.append(best_add)
            # Record each size if >= min_s
            if len(current) >= min_s:
                r = _evaluate_combo(current[:], dfs, deposit, weights, corr_matrix, cond_corr_matrix)
                if r: results.append(r)

        if cancelled and cancelled(): break
    prog_cb(total_starts, total_starts, "Cancelled." if (cancelled and cancelled()) else "Done.")
    # Deduplicate by member set
    seen = set(); unique = []
    for r in sorted(results, key=lambda x: x["score"], reverse=True):
        key = frozenset(r["members"])
        if key not in seen:
            seen.add(key); unique.append(r)
    return unique[:max_results]


def _search_montecarlo(labels, dfs, deposit, weights, min_s, max_s,
                       use_corr, corr_limit, corr_matrix, cond_corr_matrix,
                       max_results, n_samples, prog_cb, cancelled=None):
    results = []; seen = set()
    for i in range(n_samples):
        if i % 100 == 0:
            prog_cb(i, n_samples, f"Monte Carlo: {i:,} / {n_samples:,} samples")
        size  = random.randint(min_s, min(max_s, len(labels)))
        combo = sorted(random.sample(labels, size))
        key   = frozenset(combo)
        if key in seen: continue
        seen.add(key)
        if use_corr and corr_matrix is not None:
            if _portfolio_exceeds_corr(combo, corr_matrix, corr_limit):
                continue
        r = _evaluate_combo(combo, dfs, deposit, weights, corr_matrix, cond_corr_matrix)
        if r: results.append(r)
        if cancelled and cancelled(): break
    prog_cb(n_samples, n_samples, "Cancelled." if (cancelled and cancelled()) else "Done.")
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:max_results]


# ─────────────────────────────────────────────────────────────────────────────
# Combination count estimate
# ─────────────────────────────────────────────────────────────────────────────
def _combo_estimate(n: int, min_s: int, max_s: int) -> int:
    from math import comb
    return sum(comb(n, r) for r in range(min_s, max_s + 1))


def _time_estimate(n_combos: int) -> str:
    # Rough: ~0.5ms per combo for small DFs, slower for large ones
    secs = n_combos * 0.0008
    if secs < 60:    return f"~{secs:.0f}s"
    if secs < 3600:  return f"~{secs/60:.0f} min"
    return f"~{secs/3600:.1f} hrs"


# ─────────────────────────────────────────────────────────────────────────────
# Correlation heatmap figure (reused in strategies tab and result expanders)
# ─────────────────────────────────────────────────────────────────────────────
def _corr_fig(corr: pd.DataFrame, title: str = "", height: int = 300) -> go.Figure:
    labels = list(corr.columns)
    fig = go.Figure(go.Heatmap(
        z=corr.values, x=labels, y=labels,
        colorscale=[
            [0.00,"#2166AC"],[0.25,"#92C5DE"],[0.50,"#E8E8E8"],
            [0.75,"#F4A582"],[1.00,"#B2182B"],
        ],
        zmid=0, zmin=-1, zmax=1,
        text=np.round(corr.values, 2),
        texttemplate="%{text}",
        textfont=dict(size=10, color="#1a1a2e"),
        hovertemplate="%{x} / %{y}: %{z:.3f}<extra></extra>",
    ))
    fig.update_layout(
        title=dict(text=title, font=dict(size=11, color="#6C7A8D")) if title else {},
        height=height,
        margin=dict(l=20, r=60, t=30 if title else 10, b=10),
        paper_bgcolor="#F0F2F6", plot_bgcolor="#F0F2F6",
        xaxis=dict(tickfont=dict(size=9, color="#333"), tickangle=-30),
        yaxis=dict(tickfont=dict(size=9, color="#333")),
    )
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────
def _init_state():
    for k, v in {
        "pm_files":        {},
        "pm_custom_names": {},
        "pm_results":      [],
        "pm_deposit":      10000.0,
        "pm_running":      False,
        "pm_cancel":       False,
        "pm_thread_results": None,
        "pm_progress_q":   None,
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
    .warn-box{background:#2A1A00;border:1px solid #7A4A00;border-radius:6px;
              padding:10px 14px;font-size:13px;color:#FFB347;margin:8px 0}
    </style>""", unsafe_allow_html=True)

    st.markdown('<p class="pm-title">🏆 Portfolio Master</p>', unsafe_allow_html=True)
    st.markdown('<p class="pm-sub">Automated portfolio construction — composite scoring, greedy & Monte Carlo search</p>',
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
                        df = _normalise(df.copy(), stem)
                        st.session_state.pm_files[stem] = df
                        st.success(f"✅ **{stem}** — {len(df):,} trades")

        if st.session_state.pm_files:
            # Clear all button
            if st.button("🗑 Clear All Files", key="pm_clear_all"):
                st.session_state.pm_files = {}
                st.session_state.pm_custom_names = {}
                st.session_state.pm_results = []
                st.rerun()

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
    tab_config, tab_strategies, tab_results = st.tabs([
        "⚙️ Configure & Run", "📊 Strategy Stats", "🏆 Results",
    ])

    # ═════════════════════════════════════════════════════════════════════════
    # CONFIGURE & RUN
    # ═════════════════════════════════════════════════════════════════════════
    with tab_config:

        # ── Capital ──────────────────────────────────────────────────────────
        st.markdown('<div class="sh">Capital</div>', unsafe_allow_html=True)
        deposit = st.number_input("Initial Deposit ($)", min_value=100.0,
                                   max_value=10_000_000.0,
                                   value=st.session_state.pm_deposit,
                                   step=1000.0, format="%.2f", key="pm_deposit")

        # ── Composite score weights ───────────────────────────────────────────
        st.markdown('<div class="sh">Composite Score Weights</div>', unsafe_allow_html=True)
        st.caption("Weights are normalised automatically — they don't need to sum to 1.")

        wc1, wc2, wc3 = st.columns(3)
        w_retdd  = wc1.slider("Ret/DD",          0, 100, 35, key="pm_w_retdd")
        w_stab   = wc1.slider("Stability (R²)",  0, 100, 25, key="pm_w_stab")
        w_stag   = wc2.slider("Stagnation % ↓",  0, 100, 20, key="pm_w_stag",
                               help="Lower stagnation = higher score")
        w_wr     = wc2.slider("Win Rate",         0, 100, 10, key="pm_w_wr")
        w_gq     = wc3.slider("Growth Quality",   0, 100,  5, key="pm_w_gq",
                               help="Slope × R² — rewards a rising, stable equity curve")
        w_div    = wc3.slider("Diversity Bonus",  0, 100,  5, key="pm_w_div",
                               help="Rewards portfolios trading different symbols / sessions")

        total_w = w_retdd + w_stab + w_stag + w_wr + w_gq + w_div or 1
        weights = {
            "ret_dd":         w_retdd  / total_w,
            "stability":      w_stab   / total_w,
            "stagnation":     w_stag   / total_w,
            "win_rate":       w_wr     / total_w,
            "growth_quality": w_gq     / total_w,
            "diversity":      w_div    / total_w,
        }
        st.caption(f"Normalised: Ret/DD {weights['ret_dd']:.0%}  "
                   f"Stability {weights['stability']:.0%}  "
                   f"Stagnation {weights['stagnation']:.0%}  "
                   f"Win Rate {weights['win_rate']:.0%}  "
                   f"Growth Quality {weights['growth_quality']:.0%}  "
                   f"Diversity {weights['diversity']:.0%}")

        # ── Portfolio size ────────────────────────────────────────────────────
        st.markdown('<div class="sh">Portfolio Size</div>', unsafe_allow_html=True)
        sz1, sz2, sz3 = st.columns(3)
        min_strats  = sz1.number_input("Min strategies", min_value=1, max_value=len(labels),
                                        value=2, step=1, key="pm_min")
        max_strats  = sz2.number_input("Max strategies", min_value=1, max_value=len(labels),
                                        value=min(5, len(labels)), step=1, key="pm_max")
        max_results = sz3.number_input("Max portfolios to store", min_value=1, max_value=500,
                                        value=50, step=10, key="pm_maxres")

        # ── Search mode ───────────────────────────────────────────────────────
        st.markdown('<div class="sh">Search Mode</div>', unsafe_allow_html=True)
        search_mode = st.radio(
            "Algorithm",
            ["Exhaustive", "Greedy (fast)", "Monte Carlo", "Greedy + Monte Carlo"],
            horizontal=True, key="pm_search_mode",
            help="Exhaustive: every combination. Greedy: incremental build from each seed. "
                 "Monte Carlo: random sampling. Combined: greedy first then MC to fill gaps.",
        )

        mc_samples = 1000
        if "Monte Carlo" in search_mode:
            mc_samples = st.number_input("Monte Carlo samples", min_value=100,
                                          max_value=100_000, value=5000,
                                          step=500, key="pm_mc_samples")

        # ── Combination count estimate + warning ─────────────────────────────
        sel_labels = st.multiselect("Strategies to include", labels,
                                     default=labels, key="pm_sel_labels")
        n_sel = len(sel_labels)

        if n_sel >= int(min_strats):
            n_combos   = _combo_estimate(n_sel, int(min_strats), int(max_strats))
            t_estimate = _time_estimate(n_combos)

            if search_mode == "Exhaustive":
                col_est1, col_est2 = st.columns(2)
                col_est1.metric("Combinations to evaluate", f"{n_combos:,}")
                col_est2.metric("Estimated run time", t_estimate)
                if n_combos > 50_000:
                    st.markdown(
                        f'<div class="warn-box">⚠️ <b>{n_combos:,} combinations</b> — '
                        f'estimated {t_estimate}. Consider switching to Greedy or Monte Carlo '
                        f'for faster results, or reduce Max strategies / Strategy count.</div>',
                        unsafe_allow_html=True)
                elif n_combos > 5_000:
                    st.info(f"ℹ️ {n_combos:,} combinations — estimated {t_estimate}. "
                            f"This may take a moment.")
            elif "Greedy" in search_mode:
                st.metric("Greedy seeds (one per strategy)", n_sel)
            else:
                st.metric("Monte Carlo samples", f"{mc_samples:,}")

        # ── Correlation ───────────────────────────────────────────────────────
        st.markdown('<div class="sh">Correlation Filter</div>', unsafe_allow_html=True)
        cc1, cc2 = st.columns(2)
        use_corr   = cc1.checkbox("Enable pairwise correlation filter", value=True, key="pm_use_corr")
        use_cond   = cc2.checkbox("Also compute conditional correlation (drawdown periods)",
                                   value=True, key="pm_use_cond",
                                   help="Shown in results but not used for filtering — "
                                        "useful to see how strategies co-move during losses.")
        corr_limit = st.slider("Max allowed pairwise correlation",
                                min_value=0.10, max_value=0.70, value=0.50,
                                step=0.05, key="pm_corr", disabled=not use_corr,
                                help="Portfolios with any pair exceeding this are excluded.")

        # ── Date range ────────────────────────────────────────────────────────
        st.markdown('<div class="sh">Date Range Filter</div>', unsafe_allow_html=True)
        all_dates = [pd.to_datetime(df["close_time"]).dt.tz_localize(None).dropna()
                     for df in strategy_dfs.values() if "close_time" in df.columns]
        date_from = date_to = None
        if all_dates:
            g_min = min(s.min().date() for s in all_dates)
            g_max = max(s.max().date() for s in all_dates)
            use_date = st.checkbox("Filter by date range", value=False, key="pm_use_date")
            if use_date and g_min != g_max:
                import datetime as _dt
                total_days = (g_max - g_min).days
                step = max(1, total_days // 500)
                date_opts = [g_min + _dt.timedelta(days=i) for i in range(0, total_days+1, step)]
                if date_opts[-1] != g_max: date_opts.append(g_max)
                date_sel  = st.select_slider("Date range", options=date_opts,
                                              value=(g_min, g_max),
                                              format_func=lambda d: d.strftime("%d %b %Y"),
                                              key="pm_daterange")
                date_from, date_to = date_sel

        # ── Run ───────────────────────────────────────────────────────────────
        st.markdown("---")
        rb1, rb2 = st.columns([3, 1])
        run_btn    = rb1.button("🚀  Run Portfolio Search", type="primary",
                                key="pm_run",
                                disabled=st.session_state.pm_running)
        cancel_btn = rb2.button("⛔  Cancel", key="pm_cancel_btn",
                                disabled=not st.session_state.pm_running)

        if cancel_btn:
            st.session_state.pm_cancel = True
            # Signal the thread-safe event so the worker stops without session_state access
            ev = st.session_state.get("pm_cancel_event")
            if ev is not None:
                ev.set()

        # Poll for thread completion
        if st.session_state.pm_running:
            q = st.session_state.pm_progress_q
            if q is not None:
                import queue as _queue
                try:
                    msg = q.get_nowait()
                    if msg.get("status") == "done":
                        st.session_state.pm_running = False
                        st.session_state.pm_cancel  = False
                        results = msg.get("results", [])
                        st.session_state.pm_results = results
                        cancelled = msg.get("cancelled", False)
                        if cancelled:
                            st.warning(f"Search cancelled — {len(results)} portfolios found so far.")
                        else:
                            st.success(f"Found **{len(results)}** portfolios.")
                    elif msg.get("status") == "progress":
                        st.progress(msg["pct"], text=msg["text"])
                except _queue.Empty:
                    pass

            st.info("⏳ Search running…  Results will appear when complete or cancelled.")
            time.sleep(1)
            st.rerun()

        if run_btn:
            if len(sel_labels) < int(min_strats):
                st.error(f"Need at least {int(min_strats)} strategies selected.")
            else:
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
                    import queue as _queue, threading as _threading
                    q            = _queue.Queue()
                    cancel_event = _threading.Event()   # thread-safe cancel flag
                    st.session_state.pm_progress_q  = q
                    st.session_state.pm_cancel_event = cancel_event
                    st.session_state.pm_running      = True
                    st.session_state.pm_cancel       = False

                    # Capture all search params for the thread
                    _mode      = search_mode
                    _labels    = list(filtered_dfs.keys())
                    _fdfs      = filtered_dfs
                    _dep       = deposit
                    _wts       = dict(weights)
                    _min_s     = int(min_strats)
                    _max_s     = int(max_strats)
                    _use_corr  = use_corr
                    _corr_lim  = corr_limit
                    _use_cond  = use_cond
                    _max_res   = int(max_results)
                    _mc_samp   = int(mc_samples)

                    def _run_thread():
                        try:
                            cm  = _correlation_matrix(_fdfs)
                            ccm = (_conditional_correlation(_fdfs, _dep) if _use_cond else cm)

                            def _prog(done, total, msg):
                                pct = min(done / max(total, 1), 1.0)
                                q.put({"status": "progress", "pct": pct, "text": msg})

                            def _is_cancelled():
                                return cancel_event.is_set()   # no Streamlit context needed

                            if _mode == "Exhaustive":
                                res = _search_exhaustive(
                                    _labels, _fdfs, _dep, _wts, _min_s, _max_s,
                                    _use_corr, _corr_lim, cm, ccm, _max_res, _prog, _is_cancelled)
                            elif _mode == "Greedy (fast)":
                                res = _search_greedy(
                                    _labels, _fdfs, _dep, _wts, _min_s, _max_s,
                                    _use_corr, _corr_lim, cm, ccm, _max_res, _prog, _is_cancelled)
                            elif _mode == "Monte Carlo":
                                res = _search_montecarlo(
                                    _labels, _fdfs, _dep, _wts, _min_s, _max_s,
                                    _use_corr, _corr_lim, cm, ccm, _max_res, _mc_samp,
                                    _prog, _is_cancelled)
                            else:  # Greedy + Monte Carlo
                                gr = _search_greedy(
                                    _labels, _fdfs, _dep, _wts, _min_s, _max_s,
                                    _use_corr, _corr_lim, cm, ccm, _max_res, _prog, _is_cancelled)
                                mc = _search_montecarlo(
                                    _labels, _fdfs, _dep, _wts, _min_s, _max_s,
                                    _use_corr, _corr_lim, cm, ccm, _max_res, _mc_samp,
                                    _prog, _is_cancelled)
                                seen = set(); combined = []
                                for r in sorted(gr + mc, key=lambda x: x["score"], reverse=True):
                                    k = frozenset(r["members"])
                                    if k not in seen: seen.add(k); combined.append(r)
                                res = combined[:_max_res]

                            q.put({"status": "done", "results": res,
                                   "cancelled": _is_cancelled()})
                        except Exception as e:
                            q.put({"status": "done", "results": [],
                                   "cancelled": False, "error": str(e)})

                    t = _threading.Thread(target=_run_thread, daemon=True)
                    t.start()
                    st.rerun()

    # ═════════════════════════════════════════════════════════════════════════
    # STRATEGY STATS
    # ═════════════════════════════════════════════════════════════════════════
    with tab_strategies:
        st.markdown("##### Individual Strategy Statistics")
        st.caption("Edit Strategy Name to assign custom names — these carry through to Results.")

        dep_s = st.session_state.pm_deposit
        rows  = []
        for i, label in enumerate(labels):
            custom = st.session_state.pm_custom_names.get(label, "")
            row = _full_stats(strategy_dfs[label], dep_s, i+1, custom)
            if row: rows.append(row)

        if rows:
            col_order = ["#","Strategy Name","Symbol","# Trades",
                         "Net Profit ($)","Max DD ($)","Max DD (%)",
                         "Annual Profit ($)","Annual Profit (%)",
                         "Avg Win ($)","Avg Loss ($)","% Wins",
                         "Commissions ($)",
                         "Stagnation (%)","Stagnation (days)","Profit Factor",
                         "Ret/DD","Stability","Growth Quality"]
            stats_df = pd.DataFrame(rows)
            stats_df = stats_df[[c for c in col_order if c in stats_df.columns]]

            edited = st.data_editor(
                stats_df, use_container_width=True, hide_index=True,
                column_config={
                    "#":                   st.column_config.NumberColumn("#", disabled=True, width="small"),
                    "Strategy Name":       st.column_config.TextColumn("Strategy Name", width="medium"),
                    "Symbol":              st.column_config.TextColumn("Symbol", disabled=True),
                    "# Trades":            st.column_config.NumberColumn("# Trades", disabled=True, format="%d"),
                    "Net Profit ($)":      st.column_config.NumberColumn("Net Profit ($)", disabled=True, format="%.2f"),
                    "Max DD ($)":          st.column_config.NumberColumn("Max DD ($)", disabled=True, format="%.2f"),
                    "Max DD (%)":          st.column_config.NumberColumn("Max DD (%)", disabled=True, format="%.2f"),
                    "Annual Profit ($)":   st.column_config.NumberColumn("Annual Profit ($)", disabled=True, format="%.2f"),
                    "Annual Profit (%)":   st.column_config.NumberColumn("Annual Profit (%)", disabled=True, format="%.2f"),
                    "Avg Win ($)":         st.column_config.NumberColumn("Avg Win ($)", disabled=True, format="%.2f"),
                    "Avg Loss ($)":        st.column_config.NumberColumn("Avg Loss ($)", disabled=True, format="%.2f"),
                    "% Wins":              st.column_config.NumberColumn("% Wins", disabled=True, format="%.2f"),
                    "Commissions ($)":     st.column_config.NumberColumn("Commissions ($)", disabled=True, format="%.2f"),
                    "Stagnation (%)":      st.column_config.NumberColumn("Stagnation (%)", disabled=True, format="%.2f"),
                    "Stagnation (days)":   st.column_config.NumberColumn("Stagnation (d)", disabled=True, format="%d"),
                    "Profit Factor":       st.column_config.NumberColumn("PF", disabled=True, format="%.2f"),
                    "Ret/DD":              st.column_config.NumberColumn("Ret/DD", disabled=True, format="%.2f"),
                    "Stability":           st.column_config.NumberColumn("Stability", disabled=True, format="%d",
                                           help="R² of equity curve linear regression. 1.0 = perfectly straight."),
                    "Growth Quality":      st.column_config.NumberColumn("Growth Quality", disabled=True, format="%d",
                                           help="Normalised slope × R² — rewards a consistently rising equity curve."),
                },
                key="pm_stats_editor",
            )
            for _, row in edited.iterrows():
                orig = labels[int(row["#"]) - 1]
                name = str(row["Strategy Name"]).strip()
                if name and name != orig: st.session_state.pm_custom_names[orig] = name
                else: st.session_state.pm_custom_names.pop(orig, None)

            # Overall correlation heatmap
            if len(labels) > 1:
                hc1, hc2 = st.columns(2)
                with hc1:
                    st.markdown("##### Pairwise Correlation (all days)")
                    corr = _correlation_matrix(strategy_dfs)
                    disp_labels = [st.session_state.pm_custom_names.get(l,l) for l in corr.columns]
                    corr.index = corr.columns = disp_labels
                    st.plotly_chart(_corr_fig(corr, height=max(300, len(labels)*55)),
                                    use_container_width=True, key=f"pm_corr_all_{len(labels)}")
                with hc2:
                    st.markdown("##### Conditional Correlation (drawdown days only)")
                    dep_s2 = st.session_state.pm_deposit
                    cond   = _conditional_correlation(strategy_dfs, dep_s2)
                    cond.index = cond.columns = disp_labels
                    st.plotly_chart(_corr_fig(cond, height=max(300, len(labels)*55)),
                                    use_container_width=True, key=f"pm_corr_cond_{len(labels)}")

    # ═════════════════════════════════════════════════════════════════════════
    # RESULTS
    # ═════════════════════════════════════════════════════════════════════════
    with tab_results:
        results = st.session_state.pm_results
        if not results:
            st.info("Run the portfolio search on the Configure tab first.")
        else:
            def _name(lbl):
                return st.session_state.pm_custom_names.get(lbl, lbl)

            # ── Summary table ─────────────────────────────────────────────────
            st.markdown(f"##### Top {len(results)} Portfolios")
            st.markdown("""
<div style="background:#131720;border:1px solid #1E2535;border-radius:8px;padding:12px 16px;font-size:12px;color:#8899AA;margin-bottom:12px;line-height:1.7">
<b style="color:#CDD6F4">Score</b> — Composite ranking (0–1000). Higher is better. Weighted blend of the metrics below based on your sliders.<br>
<b style="color:#CDD6F4">Stability</b> — How straight the equity curve is (0–100). 100 = perfectly straight rising line. Computed as R² of linear regression on the equity curve.<br>
<b style="color:#CDD6F4">Growth Quality</b> — Combines curve straightness with upward slope. Rewards portfolios that rise consistently, not just ones that are flat and stable.<br>
<b style="color:#CDD6F4">Diversity</b> — How different the strategies are from each other (0–100), based on symbol variety and trading session overlap. 100 = completely different symbols and hours.<br>
<b style="color:#CDD6F4">Avg Corr</b> — Average pairwise correlation of daily P&L across all strategy pairs. Lower is better — strategies that don't move together reduce portfolio drawdown.<br>
<b style="color:#CDD6F4">Avg Cond Corr</b> — Same correlation computed only on days when the portfolio is in drawdown. Strategies that decorrelate during losses are more valuable than those that only decorrelate on good days.
</div>
""", unsafe_allow_html=True)

            col_order = [
                "Rank", "Score", "Strategies", "# Strategies",
                "Avg Corr", "Avg Cond Corr", "Diversity",
                "# Trades", "Net Profit ($)", "Max DD ($)", "Max DD (%)",
                "Annual Profit ($)", "Annual Profit (%)",
                "Avg Win ($)", "Avg Loss ($)", "% Wins",
                "Commissions ($)",
                "Stagnation (%)", "Stagnation (days)", "Profit Factor",
                "Ret/DD", "Stability", "Growth Quality",
            ]
            rows_r = []
            for i, r in enumerate(results):
                member_names = " + ".join(_name(m) for m in r["members"])
                fs = r.get("full_stats", {})
                row = {
                    "Rank":         i + 1,
                    "Score":        round(r.get("score", 0), 4),
                    "Strategies":   member_names,
                    "# Strategies": len(r["members"]),
                    "Avg Corr":     round(r.get("avg_corr", 0), 4),
                    "Avg Cond Corr":round(r.get("avg_cond_corr", 0), 4),
                    "Diversity":    round(r.get("diversity", 0), 4),
                }
                for col in col_order[7:]:
                    row[col] = fs.get(col, 0)
                rows_r.append(row)

            res_df = pd.DataFrame(rows_r)
            res_df = res_df[[c for c in col_order if c in res_df.columns]]

            def _cc(val, low=0):
                if not isinstance(val,(int,float)): return ""
                return "color:#34C27A" if val > low else "color:#E05555" if val < low else ""

            int_cols = {"Rank","# Strategies","# Trades","Stagnation (days)"}
            nc       = res_df.select_dtypes(include="number").columns.tolist()
            fmt      = {c: ("{:.0f}" if c in int_cols or c in
                            ("Score","Stability","Growth Quality","Diversity")
                            else "{:.3f}" if c in ("Avg Corr","Avg Cond Corr")
                            else "{:.2f}") for c in nc}

            pos_cols = [c for c in ["Net Profit ($)","Annual Profit ($)","Annual Profit (%)","Avg Win ($)","Score"]
                        if c in res_df.columns]
            neg_cols = [c for c in ["Max DD ($)","Max DD (%)","Avg Loss ($)","Avg Corr","Avg Cond Corr"]
                        if c in res_df.columns]

            styled = (
                res_df.style.format(fmt)
                .map(_cc,                           subset=pos_cols if pos_cols else [])
                .map(lambda v: "color:#E05555" if isinstance(v,(int,float)) and v < 0 else "",
                     subset=neg_cols if neg_cols else [])
                .map(lambda v: _cc(v, 1.0),
                     subset=["Profit Factor"] if "Profit Factor" in res_df.columns else [])
            )
            st.dataframe(styled, use_container_width=True, hide_index=True)

            buf = io.StringIO(); res_df.to_csv(buf, index=False)
            st.download_button("⬇️ Export Results CSV", buf.getvalue(),
                               file_name="portfolio_master_results.csv", mime="text/csv")

            # ── Detail expanders ──────────────────────────────────────────────
            st.markdown("##### Portfolio Detail")
            show_top = st.slider("Show detail for top N", 1, min(10, len(results)),
                                  min(5, len(results)), key="pm_show_top")

            for i, r in enumerate(results[:show_top]):
                member_names = " + ".join(_name(m) for m in r["members"])
                with st.expander(
                    f"#{i+1}  {member_names}  "
                    f"| Score {r['score']:.4f}  "
                    f"| Ret/DD {r['ret_dd']:.2f}  "
                    f"| Net ${r['net_profit']:,.2f}  "
                    f"| DD ${r['max_dd']:,.2f}"
                ):
                    # Key metrics row
                    mc1,mc2,mc3,mc4,mc5,mc6 = st.columns(6)
                    mc1.metric("Score",          f"{r['score']:.4f}")
                    mc2.metric("Ret/DD",         f"{r['ret_dd']:.2f}")
                    mc3.metric("Stability",      f"{r['stability']}")
                    mc4.metric("Growth Quality", f"{r['growth_quality']}")
                    mc5.metric("Avg Correlation",f"{r['avg_corr']:.3f}")
                    mc6.metric("Avg Cond Corr",  f"{r['avg_cond_corr']:.3f}",
                               help="Correlation during drawdown days only")

                    dc1, dc2 = st.columns(2)

                    # Mini equity chart
                    with dc1:
                        frames = [strategy_dfs[m].copy() for m in r["members"] if m in strategy_dfs]
                        if frames:
                            combined = pd.concat(frames, ignore_index=True)
                            if "close_time" in combined.columns:
                                combined = combined.sort_values("close_time").reset_index(drop=True)
                            eq   = st.session_state.pm_deposit + combined["net_profit"].cumsum()
                            rm   = eq.cummax(); dd_c = eq - rm
                            pfig = go.Figure()
                            pfig.add_trace(go.Scatter(x=combined["close_time"], y=eq,
                                name="Equity", line=dict(color="#4C8EF5", width=2), mode="lines"))
                            pfig.add_trace(go.Scatter(x=combined["close_time"], y=dd_c,
                                name="DD", fill="tozeroy",
                                fillcolor="rgba(220,50,50,0.25)",
                                line=dict(color="rgba(220,50,50,0.6)", width=1),
                                mode="lines", yaxis="y2"))
                            pfig.update_layout(
                                height=200, margin=dict(l=40,r=40,t=10,b=10),
                                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#0E1117",
                                hovermode="x unified",
                                legend=dict(orientation="h", y=1.1, font=dict(size=9)),
                                yaxis=dict(gridcolor="#1E2130", tickprefix="$"),
                                yaxis2=dict(overlaying="y", side="right",
                                            gridcolor="#1E2130", tickprefix="$", showgrid=False),
                            )
                            # Add invisible annotation to ensure figure hash is unique per portfolio
                            pfig.add_annotation(text=str(i), x=0, y=0, opacity=0,
                                                showarrow=False, xref="paper", yref="paper")
                            st.plotly_chart(pfig, use_container_width=True, key=f"pm_pfig_{i}")

                    # Per-result correlation heatmap
                    with dc2:
                        if len(r["members"]) > 1:
                            member_dfs = {m: strategy_dfs[m] for m in r["members"] if m in strategy_dfs}
                            if len(member_dfs) > 1:
                                r_corr = _correlation_matrix(member_dfs)
                                r_cond = _conditional_correlation(member_dfs, st.session_state.pm_deposit)
                                disp   = [_name(m) for m in r_corr.columns]
                                r_corr.index = r_corr.columns = disp
                                r_cond.index = r_cond.columns = disp
                                st.plotly_chart(
                                    _corr_fig(r_corr, title="Correlation (all days)", height=180),
                                    use_container_width=True, key=f"pm_rcorr_{i}")
                                st.plotly_chart(
                                    _corr_fig(r_cond, title="Conditional (DD days)", height=180),
                                    use_container_width=True, key=f"pm_rcond_{i}")

                    # Member stats table
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
                                "Growth Quality": s.get("Growth Quality",0),
                            })
                    if m_rows:
                        mdf = pd.DataFrame(m_rows)
                        st.dataframe(
                            mdf.style.format({c:"{:.0f}" if c in ("Stability","Growth Quality")
                                              else "{:.2f}"
                                              for c in mdf.select_dtypes("number").columns}),
                            use_container_width=True, hide_index=True)