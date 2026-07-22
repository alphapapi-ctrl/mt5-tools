"""
trade_stats.py — canonical trade statistics for MT5Tools.
Single source of truth for definitions shared across pages
(Portfolio Builder, Portfolio Master, Trade Analysis, Prop Planner).

Conventions:
- Drawdown is balance-based: closed-trade PnL cumsum from a deposit,
  in close_time order.
- Max DD % follows MT5's "Balance Drawdown Maximal": the DD at its deepest
  point relative to the peak balance at that point — NOT the initial deposit.
- Stagnation is measured on the daily-last balance curve.
- Stability / growth quality regress the DAILY balance curve against calendar
  days, so bursts of trade density don't distort R².
"""

import numpy as np
import pandas as pd


def ordered_profits(df: pd.DataFrame) -> pd.Series:
    """net_profit in close_time order (falls back to row order)."""
    if df.empty or "net_profit" not in df.columns:
        return pd.Series(dtype=float)
    if "close_time" in df.columns:
        d = df.sort_values("close_time")
    else:
        d = df
    return pd.to_numeric(d["net_profit"], errors="coerce").fillna(0).reset_index(drop=True)


def drawdown_stats(profits: pd.Series, deposit: float) -> dict:
    """Balance-based drawdown, MT5 convention. `profits` must be in trade order.
    Returns max_dd (negative $), max_dd_pct (negative %, relative to the peak
    balance at the deepest point) and peak_equity."""
    profits = pd.to_numeric(profits, errors="coerce").fillna(0)
    if profits.empty:
        return {"max_dd": 0.0, "max_dd_pct": 0.0, "peak_equity": round(float(deposit), 2)}
    balance = deposit + profits.cumsum()
    peak    = balance.cummax()
    dd      = balance - peak
    i       = dd.idxmin()
    max_dd  = float(dd.min())
    peak_at = float(peak.loc[i])
    pct     = max_dd / peak_at * 100 if peak_at else 0.0
    return {"max_dd": round(max_dd, 2), "max_dd_pct": round(pct, 2),
            "peak_equity": round(float(peak.max()), 2)}


def consec_streaks(profits: pd.Series) -> dict:
    """Max consecutive wins/losses. `profits` must be in trade order."""
    mcw = mcl = cw = cl = 0
    for p in profits:
        if p > 0:
            cw += 1; cl = 0
        else:
            cl += 1; cw = 0
        mcw = max(mcw, cw); mcl = max(mcl, cl)
    return {"max_consec_wins": mcw, "max_consec_losses": mcl}


def basic_stats(profits: pd.Series) -> dict:
    """Order-independent aggregates: counts, win rate, PF, averages."""
    profits = pd.to_numeric(profits, errors="coerce").fillna(0)
    n     = len(profits)
    wins  = profits[profits > 0]
    losses = profits[profits < 0]
    gp = float(wins.sum())
    gl = float(losses.sum())
    return {
        "num_trades":    n,
        "gross_profit":  round(gp, 2),
        "gross_loss":    round(gl, 2),
        "net_profit":    round(float(profits.sum()), 2),
        "win_count":     int((profits > 0).sum()),
        "loss_count":    int((profits <= 0).sum()),
        "win_rate":      round((profits > 0).sum() / n * 100, 2) if n else 0.0,
        "avg_win":       round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss":      round(float(losses.mean()), 2) if len(losses) else 0.0,
        "avg_trade":     round(float(profits.mean()), 2) if n else 0.0,
        "profit_factor": round(gp / abs(gl), 2) if gl else float("inf"),
    }


def daily_balance(df: pd.DataFrame, deposit: float) -> pd.Series:
    """Daily-last balance curve indexed by normalized date."""
    if df.empty or "close_time" not in df.columns or "net_profit" not in df.columns:
        return pd.Series(dtype=float)
    t = df[["close_time", "net_profit"]].dropna().sort_values("close_time")
    if t.empty:
        return pd.Series(dtype=float)
    bal = deposit + pd.to_numeric(t["net_profit"], errors="coerce").fillna(0).cumsum()
    return bal.groupby(pd.to_datetime(t["close_time"]).dt.normalize()).last()


def max_stagnation_days(df: pd.DataFrame, deposit: float) -> int:
    """Longest run of calendar days without a new daily-balance high."""
    dly = daily_balance(df, deposit)
    if dly.empty:
        return 0
    peak = dly.iloc[0]
    start = dly.index[0]
    max_stag = 0
    for date, v in dly.items():
        if v > peak:
            peak = v
            start = date
        else:
            max_stag = max(max_stag, (date - start).days)
    return int(max_stag)


def equity_regression(df: pd.DataFrame, deposit: float) -> dict:
    """Linear regression of the daily balance curve on calendar days.
    stability      = R² × 100 (0–100)
    growth_quality = annualised return (% of deposit per year) × R²,
                     i.e. ~30 for a 40%-a-year curve at R² 0.75."""
    dly = daily_balance(df, deposit)
    if len(dly) < 3:
        return {"stability": 0, "growth_quality": 0}
    x = np.asarray((dly.index - dly.index[0]).days, dtype=float)
    y = dly.values.astype(float)
    if x[-1] <= 0 or np.allclose(y, y[0]):
        return {"stability": 0, "growth_quality": 0}
    slope, _ = np.polyfit(x, y, 1)
    corr = np.corrcoef(x, y)[0, 1]
    r2 = float(corr ** 2) if np.isfinite(corr) else 0.0
    annual_ret_pct = slope * 365.25 / deposit * 100 if deposit else 0.0
    return {"stability": int(round(r2 * 100)),
            "growth_quality": int(round(annual_ret_pct * r2))}
