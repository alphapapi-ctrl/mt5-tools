"""
view_prop_planner.py — Prop Planner page for MT5 Tools
Simulate prop firm accounts (FTMO / Blue Guardian / Custom) trade-by-trade
from MT5 backtest HTML reports, with lot-step position sizing derived from
each report's Equity Drawdown Maximal.

Sizing rule (set-file method):
  balance_per_step = (equity_dd_max / backtest_volume * lot_step) / budget_dd_pct
  lots             = floor(basis_balance / balance_per_step) * lot_step
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import importlib, sys, re, os, glob, json

from trade_stats import drawdown_stats, ordered_profits

COLOURS = ["#4a90d9", "#26a69a", "#ff9800", "#ab47bc"]

# Symbols treated as indices (lot step 0.10). Matched on symbol_base.
_INDEX_PATTERNS = [
    r'^(US|SPX|NAS|USTEC|DJ|DOW)\d*$', r'^US\d{2,3}$', r'^(GER|DE|DAX)\d{2}$',
    r'^(UK|FTSE)\d{2,3}$', r'^(JP|JPN|NIK)\d{2,3}$', r'^(AUS|AU)\d{2,3}$',
    r'^(HK|HSI)\d{2}$', r'^(EU|STOXX)\d{2}$', r'^(FRA|CAC)\d{2}$', r'^(ES|IBEX)\d{2}$',
]


def _get_parser():
    if "mt5_parser" in sys.modules:
        return importlib.reload(sys.modules["mt5_parser"])
    import mt5_parser
    return mt5_parser


def _is_index_symbol(symbol_base: str) -> bool:
    s = (symbol_base or "").upper()
    return any(re.match(p, s) for p in _INDEX_PATTERNS)


# ─────────────────────────────────────────────────────────────────────────────
# Prop firm presets (ported from School Run App Prop Planner)
# ─────────────────────────────────────────────────────────────────────────────
PROP_FIRM_PRESETS = {
    "FTMO": {
        "drawdown_type": "static",
        "max_loss_pct": 10.0,
        "daily_loss_type": "pct",
        "daily_loss_pct": 5.0,
        "daily_loss_fixed": 0.0,
        "payout_threshold_pct": 10.0,
        "payout_amount": 5000.0,
        "trader_split_pct": 90.0,
        "max_equity": 130000.0,
        "cap_buffer": 5000.0,
        "withdrawal_buffer_pct": 0.0,
        "account_size": 100000.0,
        # Available accounts (2-step challenge, fees in EUR, 2026 pricing)
        "fee_currency": "EUR",
        "accounts": [
            {"size": 10000.0,  "fee": 155.0},
            {"size": 25000.0,  "fee": 250.0},
            {"size": 50000.0,  "fee": 345.0},
            {"size": 100000.0, "fee": 540.0},
            {"size": 200000.0, "fee": 1080.0},
        ],
    },
    "Blue Guardian": {
        "drawdown_type": "trailing_lock",
        "max_loss_pct": 6.0,
        "daily_loss_type": "fixed",
        "daily_loss_pct": 0.0,
        "daily_loss_fixed": 3000.0,
        "payout_threshold_pct": 5.0,
        "payout_amount": 5000.0,
        "trader_split_pct": 80.0,
        "max_equity": 200000.0,
        "cap_buffer": 5000.0,
        "withdrawal_buffer_pct": 0.5,
        "account_size": 100000.0,
        # Available accounts (2-step challenge, fees in USD, 2026 pricing)
        "fee_currency": "USD",
        "accounts": [
            {"size": 5000.0,   "fee": 49.0},
            {"size": 10000.0,  "fee": 112.0},
            {"size": 25000.0,  "fee": 229.0},
            {"size": 50000.0,  "fee": 297.0},
            {"size": 100000.0, "fee": 447.0},
            {"size": 200000.0, "fee": 797.0},
        ],
    },
    "Custom": {
        "drawdown_type": "static",
        "max_loss_pct": 10.0,
        "daily_loss_type": "pct",
        "daily_loss_pct": 5.0,
        "daily_loss_fixed": 0.0,
        "payout_threshold_pct": 10.0,
        "payout_amount": 5000.0,
        "trader_split_pct": 90.0,
        "max_equity": 130000.0,
        "cap_buffer": 5000.0,
        "withdrawal_buffer_pct": 0.0,
        "account_size": 100000.0,
    },
    # Own capital, self-imposed rules — selected via the Personal firm type,
    # not the Classic template dropdown. No split, no equity cap.
    "Personal": {
        "drawdown_type": "trailing",
        "max_loss_pct": 10.0,
        "daily_loss_type": "pct",
        "daily_loss_pct": 5.0,
        "daily_loss_fixed": 0.0,
        "payout_threshold_pct": 10.0,
        "payout_amount": 5000.0,
        "trader_split_pct": 100.0,
        "max_equity": 1e15,
        "cap_buffer": 0.0,
        "withdrawal_buffer_pct": 0.0,
        "account_size": 10000.0,
    },
}

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _payout_heatmap(all_events, acct_ccy, key, level_dates=None):
    """Year x month calendar heatmap of net payouts. Optionally annotates
    level-up counts (▲n) when level_dates is given."""
    rows = [{"date": pd.Timestamp(ev["date"]), "net": ev["net"]}
            for evs in all_events.values() for ev in evs]
    if not rows:
        return
    df = pd.DataFrame(rows)
    df["year"], df["month"] = df["date"].dt.year, df["date"].dt.month
    pivot = (df.pivot_table(index="year", columns="month", values="net", aggfunc="sum")
               .reindex(columns=range(1, 13)))

    lvl_pivot = None
    if level_dates:
        ldf = pd.DataFrame({"date": pd.to_datetime(level_dates)})
        ldf["year"], ldf["month"] = ldf["date"].dt.year, ldf["date"].dt.month
        lvl_pivot = (ldf.pivot_table(index="year", columns="month", values="date",
                                     aggfunc="count")
                        .reindex(index=pivot.index, columns=range(1, 13)))

    text = []
    for year in pivot.index:
        trow = []
        for m in range(1, 13):
            v = pivot.loc[year, m]
            if pd.isna(v):
                trow.append("")
                continue
            t = f"{v:,.0f}"
            if lvl_pivot is not None:
                n = lvl_pivot.loc[year, m]
                if not pd.isna(n) and n > 0:
                    t += f"<br>▲{int(n)}"
            trow.append(t)
        text.append(trow)

    fig = go.Figure(go.Heatmap(
        z=pivot.values,
        x=_MONTHS,
        y=[str(y) for y in pivot.index],
        colorscale=[[0, "#152238"], [1, "#26a69a"]],
        text=text, texttemplate="%{text}", textfont=dict(size=10),
        hoverongaps=False, xgap=2, ygap=2,
        colorbar=dict(title=f"Net {acct_ccy}", tickformat=",.0f"),
    ))
    fig.update_layout(
        height=max(240, 80 + 36 * len(pivot.index)),
        margin=dict(l=0, r=0, t=10, b=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#ccc"),
        yaxis=dict(autorange="reversed"),
    )
    st.plotly_chart(fig, width="stretch", key=key)


def _bootstrap_blocks(mc_trades: pd.DataFrame, block_mode: str) -> list:
    """Contiguous trade-index blocks for block bootstrap (per trade / weekly /
    monthly). Partial edge blocks under half the median size are dropped."""
    n = len(mc_trades)
    if block_mode == "Per trade":
        return [[j] for j in range(n)]
    dates = pd.to_datetime(mc_trades["close_time"])
    keys = (dates.dt.strftime("%G-W%V") if block_mode == "Weekly"
            else dates.dt.to_period("M").astype(str)).values
    blocks, prev = [], None
    for idx_, key in enumerate(keys):
        if key != prev:
            blocks.append([idx_])
            prev = key
        else:
            blocks[-1].append(idx_)
    med = np.median([len(b) for b in blocks])
    return [b for b in blocks if len(b) >= med * 0.5]


_DD_TYPE_OPTIONS = ["static", "trailing", "trailing_lock"]
_DD_TYPE_LABELS = {
    "static":        "Static (from initial balance)",
    "trailing":      "Trailing (follows equity HWM)",
    "trailing_lock": "Trailing → Lock (locks at initial balance)",
}


# ─────────────────────────────────────────────────────────────────────────────
# Concept Trading (TCT) leveling accounts
# ─────────────────────────────────────────────────────────────────────────────
_CONCEPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "concept_accounts.json")


def _load_concept():
    """Load the scraped TCT payout matrices. Returns {program: {tier: spec}}.
    Keys starting with '_' are metadata (e.g. _max_accounts) at either depth."""
    with open(_CONCEPT_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _concept_tiers(prog_dict):
    return {k: v for k, v in prog_dict.items() if not k.startswith("_")}


def _level_label(lv):
    return f"{lv['level']} (${lv['se']:,.0f})"


def simulate_concept_account(stream, cfg, strat_cfgs):
    """
    Walk the merged trade stream through a TCT leveling account.

    Mechanics: each level is a fresh account at that level's starting equity
    (SE). Static DD floor = SE x (1 - dd%). Profit target is judged on the
    equity HWM; on hitting it the trader is paid the matrix payout
    (SE x PT% x PO%) regardless of current balance, and the account jumps to
    the next level's SE. Lots are fixed within a level, derived from SE.
    On the final (or locked) level there is no next level: profit above SE is
    withdrawn at each target hit, at that level's split.

    If cfg["reset_on_breach"] is set, a breach burns the account and a fresh
    one is started from the configured start level (another assessment fee),
    so the whole backtest period is assessed — like the classic account-loss
    simulation. Otherwise the walk stops at the first breach.

    cfg: {levels, dd_pct, pt_pct, start_idx, lock_idx (or None), reset_on_breach}
    Returns (rows DataFrame, payout_events, level_log, diagnostics).
    """
    levels   = cfg["levels"]
    dd_pct   = cfg["dd_pct"] / 100
    pt_pct   = cfg["pt_pct"] / 100
    start_idx = cfg["start_idx"]
    idx      = start_idx
    lock_idx = cfg.get("lock_idx")
    reset_on_breach = bool(cfg.get("reset_on_breach"))

    def _is_terminal(i, locked):
        return locked or bool(levels[i].get("final"))

    locked  = False
    se      = float(levels[idx]["se"])
    equity  = se
    hwm     = se
    floor   = se * (1 - dd_pct)
    target  = se * (1 + pt_pct)
    lots_by_strat = {i: _lots_for(se, sc) for i, sc in enumerate(strat_cfgs)}

    rows, payout_events, level_log = [], [], []
    forced_min = 0
    breaches   = 0
    attempt    = 1
    best_idx   = idx
    level_start_date = None
    level_trades = 0

    for t in stream.itertuples():
        if level_start_date is None:
            level_start_date = t.date

        lots = lots_by_strat[t.strat]
        if _is_forced_min(se, strat_cfgs[t.strat]):
            forced_min += 1

        trade_pnl = t.pnl_per_lot * lots
        equity   += trade_pnl
        level_trades += 1
        if equity > hwm:
            hwm = equity

        # ── DD breach (static per level)
        if equity <= floor:
            breaches += 1
            rows.append({
                "date": t.date, "close_time": t.close_time, "equity": equity,
                "trade_pnl": trade_pnl, "lots": lots, "level": levels[idx]["level"],
                "se": se, "attempt": attempt,
                "status": f"BREACHED (level {levels[idx]['level']}, account #{attempt})",
                "payout": 0, "net_payout": 0,
                "total_paid": payout_events[-1]["cumulative"] if payout_events else 0,
                "breached": True, "hwm": hwm,
            })
            level_log.append({
                "attempt": attempt, "level": levels[idx]["level"], "se": se,
                "start": level_start_date, "end": t.date,
                "trades": level_trades, "outcome": "breached", "net_payout": 0.0,
            })
            level_start_date = None   # level is closed either way
            if not reset_on_breach:
                break
            # Fresh account: back to the start level, new fee, clean state
            attempt += 1
            idx    = start_idx
            locked = False
            se     = float(levels[idx]["se"])
            equity = se
            hwm    = se
            floor  = se * (1 - dd_pct)
            target = se * (1 + pt_pct)
            lots_by_strat = {i: _lots_for(se, sc) for i, sc in enumerate(strat_cfgs)}
            level_start_date = None
            level_trades = 0
            continue

        payout = net_payout = 0.0
        terminal = _is_terminal(idx, locked)

        # ── Target hit (on HWM, regardless of balance)
        if hwm >= target:
            po_pct = 90.0 if locked else float(levels[idx].get("po_pct", 50))
            if terminal:
                # Withdraw profit above SE at the terminal split, stay at level
                if equity > se:
                    payout     = equity - se
                    net_payout = payout * (po_pct / 100)
                    equity     = se
                hwm = equity
            else:
                # Matrix payout on level-up
                payout     = se * pt_pct
                net_payout = payout * (po_pct / 100)
                level_log.append({
                    "attempt": attempt, "level": levels[idx]["level"], "se": se,
                    "start": level_start_date, "end": t.date,
                    "trades": level_trades, "outcome": "passed",
                    "net_payout": net_payout,
                })
                if lock_idx is not None and idx == lock_idx:
                    locked = True         # stay at this level, 90% from now on
                else:
                    idx += 1
                best_idx = max(best_idx, idx)
                se     = float(levels[idx]["se"])
                equity = se
                hwm    = se
                floor  = se * (1 - dd_pct)
                target = se * (1 + pt_pct)
                lots_by_strat = {i: _lots_for(se, sc) for i, sc in enumerate(strat_cfgs)}
                level_start_date = None
                level_trades = 0

            if net_payout > 0:
                payout_events.append({
                    "date": t.date, "gross": payout, "net": net_payout,
                    "equity": equity,
                    "cumulative": (payout_events[-1]["cumulative"] if payout_events else 0)
                                  + net_payout,
                })

        rows.append({
            "date": t.date, "close_time": t.close_time, "equity": equity,
            "trade_pnl": trade_pnl, "lots": lots, "level": levels[idx]["level"],
            "se": se, "attempt": attempt,
            "status": "locked" if locked else "ok",
            "payout": payout, "net_payout": net_payout,
            "total_paid": payout_events[-1]["cumulative"] if payout_events else 0,
            "breached": False, "hwm": hwm,
        })

    # Open (unfinished) level of the current account
    if rows and level_start_date is not None:
        _end = rows[-1]["date"] if level_trades > 0 else level_start_date
        level_log.append({
            "attempt": attempt, "level": levels[idx]["level"], "se": se,
            "start": level_start_date, "end": _end,
            "trades": level_trades, "outcome": "in progress",
            "net_payout": 0.0,
        })

    diagnostics = {"forced_min_trades": forced_min, "final_idx": idx,
                   "best_idx": best_idx, "locked": locked,
                   "breached": breaches > 0, "breaches": breaches,
                   "attempts": attempt}
    return pd.DataFrame(rows), payout_events, level_log, diagnostics


# ─────────────────────────────────────────────────────────────────────────────
# Simulation engine — lot-step sizing
# ─────────────────────────────────────────────────────────────────────────────
def _lots_for(basis_balance, strat):
    """Lot size for a strategy given the sizing basis balance.
    Never returns 0: an account too small for the budget-derived size still
    trades the minimum lot step and simply wears the oversized risk — by
    design, so undersized accounts fail visibly instead of sitting idle."""
    bps  = strat["bal_per_step"]
    step = strat["lot_step"]
    if bps <= 0:
        return step
    return max(1, int(basis_balance // bps)) * step


def _is_forced_min(basis_balance, strat):
    """True when the budget-derived size would be 0 and the min step is forced."""
    return strat["bal_per_step"] > 0 and basis_balance < strat["bal_per_step"]


def simulate_account(stream, cfg, strat_cfgs, gcfg):
    """
    Walk the merged trade stream through one prop account.
    stream: DataFrame with close_time, date, strat (index into strat_cfgs),
            pnl_per_lot (account currency).
    Returns (rows DataFrame, payout_events, diagnostics).
    """
    acc_size = cfg["account_size"]
    max_loss = cfg["max_loss_pct"] / 100
    dd_type  = cfg["drawdown_type"]
    dl_type  = cfg["daily_loss_type"]
    daily_loss_pct   = cfg["daily_loss_pct"] / 100
    daily_loss_fixed = cfg["daily_loss_fixed"]
    pay_thr  = cfg["payout_threshold_pct"] / 100
    pay_amt  = cfg["payout_amount"]
    split    = cfg["trader_split_pct"] / 100
    max_eq   = cfg["max_equity"]
    cap_buf  = cfg["cap_buffer"]
    wd_buf   = cfg["withdrawal_buffer_pct"] / 100
    compound = gcfg["compound"]

    reset_on_breach = bool(cfg.get("reset_on_breach"))
    # Personal accounts: the daily limit is a circuit breaker — skip the rest
    # of the day's trades and continue tomorrow, instead of killing the account.
    daily_stop_only = bool(cfg.get("daily_stop_only"))

    equity      = acc_size
    hwm         = acc_size
    payout_base = acc_size

    trailing_floor = acc_size * (1 - max_loss)
    floor_locked   = False

    daily_start_eq = acc_size
    current_day    = None
    forced_min     = 0
    breaches       = 0
    attempt        = 1
    halt_day       = None
    cb_days        = 0
    cb_skipped     = 0

    rows, payout_events = [], []

    def _breach_row(t, pnl, lots, label):
        return {
            "date": t.date, "close_time": t.close_time, "equity": equity,
            "trade_pnl": pnl, "lots": lots, "attempt": attempt, "status": label,
            "payout": 0, "net_payout": 0,
            "total_paid": payout_events[-1]["cumulative"] if payout_events else 0,
            "breached": True, "hwm": hwm,
        }

    for t in stream.itertuples():
        # Circuit breaker active: no more trades until the next day
        if halt_day is not None:
            if t.date == halt_day:
                cb_skipped += 1
                continue
            halt_day = None

        if t.date != current_day:
            daily_start_eq = equity
            current_day = t.date

        strat = strat_cfgs[t.strat]
        basis = equity if compound else acc_size
        lots  = _lots_for(basis, strat)
        if _is_forced_min(basis, strat):
            forced_min += 1

        trade_pnl = t.pnl_per_lot * lots
        equity   += trade_pnl

        if equity > hwm:
            hwm = equity

        if dd_type in ("trailing", "trailing_lock") and not floor_locked:
            trailing_floor = max(trailing_floor, equity * (1 - max_loss))
            if dd_type == "trailing_lock" and trailing_floor >= acc_size:
                trailing_floor = acc_size
                floor_locked = True

        # ── Breach checks (overall then daily)
        breach_label = None
        breach_daily = False
        if dd_type == "static":
            if (acc_size - equity) / acc_size >= max_loss:
                breach_label = "overall DD"
        elif equity <= trailing_floor:
            breach_label = "trailing DD" + (" [locked]" if floor_locked else "")
        if breach_label is None:
            if dl_type == "fixed" and daily_loss_fixed > 0 \
                    and daily_start_eq - equity >= daily_loss_fixed:
                breach_label = f"daily DD ${daily_loss_fixed:,.0f}"
                breach_daily = True
            elif dl_type == "pct" and daily_loss_pct > 0:
                daily_dd = (daily_start_eq - equity) / daily_start_eq if daily_start_eq > 0 else 0
                if daily_dd >= daily_loss_pct:
                    breach_label = "daily DD"
                    breach_daily = True

        if breach_label is not None and breach_daily and daily_stop_only:
            # Circuit breaker: keep the account, stop trading for the day
            cb_days += 1
            halt_day = t.date
            rows.append({
                "date": t.date, "close_time": t.close_time, "equity": equity,
                "trade_pnl": trade_pnl, "lots": lots, "attempt": attempt,
                "status": f"daily stop ({breach_label}) — rest of day skipped",
                "payout": 0, "net_payout": 0,
                "total_paid": payout_events[-1]["cumulative"] if payout_events else 0,
                "breached": False, "hwm": hwm,
            })
            continue

        if breach_label is not None:
            breaches += 1
            rows.append(_breach_row(t, trade_pnl, lots,
                                    f"BREACHED ({breach_label}, account #{attempt})"))
            if not reset_on_breach:
                break
            # Buy a new account: fresh balance, floors and payout base
            attempt += 1
            equity      = acc_size
            hwm         = acc_size
            payout_base = acc_size
            trailing_floor = acc_size * (1 - max_loss)
            floor_locked   = False
            daily_start_eq = acc_size
            continue

        # ── Payout
        payout = net_payout = 0.0
        if equity >= payout_base * (1 + pay_thr):
            payout = min(pay_amt, equity - payout_base)
            if dd_type != "static":
                min_eq = trailing_floor + acc_size * wd_buf
            else:
                min_eq = acc_size * (1 - max_loss) + acc_size * wd_buf
            payout = max(0, min(payout, equity - min_eq))
            if payout > 0:
                equity     -= payout
                # Withdrawals are not trading losses — keep the daily-loss
                # check measuring trade PnL only
                daily_start_eq -= payout
                net_payout  = payout * split
                payout_base = equity
                hwm = equity
                payout_events.append({
                    "date": t.date, "gross": payout, "net": net_payout,
                    "equity": equity,
                    "cumulative": sum(e["net"] for e in payout_events) + net_payout,
                })

        # ── Equity cap
        if equity > max_eq:
            excess      = equity - (max_eq - cap_buf)
            payout     += excess
            net_payout += excess * split
            equity     -= excess
            daily_start_eq -= excess   # cap sweeps aren't trading losses either
            payout_base = equity
            hwm = equity
            if excess > 0:
                payout_events.append({
                    "date": t.date, "gross": excess, "net": excess * split,
                    "equity": equity,
                    "cumulative": sum(e["net"] for e in payout_events),
                })

        rows.append({
            "date": t.date, "close_time": t.close_time, "equity": equity,
            "trade_pnl": trade_pnl, "lots": lots, "attempt": attempt, "status": "ok",
            "payout": payout, "net_payout": net_payout,
            "total_paid": sum(e["net"] for e in payout_events),
            "breached": False, "hwm": hwm,
        })

    diagnostics = {"forced_min_trades": forced_min, "breaches": breaches,
                   "attempts": attempt, "breached": breaches > 0,
                   "circuit_breaker_days": cb_days, "cb_skipped_trades": cb_skipped}
    return pd.DataFrame(rows), payout_events, diagnostics


# ─────────────────────────────────────────────────────────────────────────────
# Concept Trading page section
# ─────────────────────────────────────────────────────────────────────────────
def _render_concept(stream, strat_cfgs, active, acct_ccy, run_start, run_end):
    try:
        concept = _load_concept()
    except Exception as e:
        st.error(f"Could not load concept_accounts.json: {e}")
        return

    if acct_ccy != "AUD":
        st.info("TCT accounts are AUD-denominated — the level balances below are AUD. "
                "Set **Account currency = AUD** above so report PnL is converted to match.",
                icon="💱")

    # ── Account configuration ─────────────────────────────────────────────
    with st.expander("🏦 Concept Account Configuration", expanded=True):
        st.caption("Each level is a fresh account at that level's starting equity. The profit "
                   "target is judged on the equity high-water mark; on level-up you are paid the "
                   "matrix payout regardless of balance. Lots are fixed within a level, derived "
                   "from the level's starting equity. Account limits: 4 Premier, 2 Empire.")
        n_accounts = st.slider("Number of accounts", 1, 6, 1, key="tct_n_acc")
        reset_on_breach = st.checkbox(
            "Reset to a new account on breach",
            value=True, key="tct_reset",
            help="A breach burns the account; the sim buys a fresh one at the start level "
                 "(paying the fee again) and keeps walking, so the whole backtest period is "
                 "assessed. Untick to stop at the first breach.")
        account_configs = []
        cols = st.columns(n_accounts)
        for i, col in enumerate(cols):
            with col:
                st.markdown(f"**Account {i+1}**")
                enabled = st.checkbox("Enabled", value=True, key=f"tct_en_{i}")
                program = st.selectbox("Program", list(concept.keys()), key=f"tct_prog_{i}")
                tier    = st.selectbox("Account type",
                                       list(_concept_tiers(concept[program]).keys()),
                                       key=f"tct_tier_{i}")
                spec    = concept[program][tier]
                levels  = spec["levels"]
                _k = f"{i}_{program}_{tier}"
                label = st.text_input("Label", value=f"{program} {tier} #{i+1}",
                                      key=f"tct_lbl_{_k}")
                start_idx = st.selectbox("Start level", list(range(len(levels) - 1)),
                                         format_func=lambda j, _lv=levels: _level_label(_lv[j]),
                                         key=f"tct_start_{_k}")
                lock_idx = st.selectbox("90% Lock Rule", [None] + list(range(1, len(levels) - 1)),
                                        format_func=lambda j, _lv=levels:
                                            "No lock — scale to max" if j is None
                                            else f"Lock at {_level_label(_lv[j])}",
                                        key=f"tct_lock_{_k}",
                                        help="Complete the chosen level's target once at the "
                                             "standard split, then stay at that level with a "
                                             "90% split thereafter.")
                start_date = st.date_input("Start date", value=run_start,
                                           min_value=run_start, max_value=run_end,
                                           key=f"tct_sd_{i}")
                # Instant Funded variants skip Intern and start at L1 for a higher fee
                if start_idx == 1 and spec.get("instant_fee"):
                    fee = spec["instant_fee"]
                    _fee_note = "Instant Funded"
                else:
                    fee = spec["assessment_fee"]
                    _fee_note = "assessment"
                st.caption(f"Target {spec['pt_pct']:g}% · Max DD {spec['dd_pct']:g}% static per "
                           f"level · Fee ${fee:,} AUD ({_fee_note})")
                account_configs.append({
                    "enabled": enabled, "label": label, "program": program, "tier": tier,
                    "levels": levels, "dd_pct": spec["dd_pct"], "pt_pct": spec["pt_pct"],
                    "start_idx": start_idx, "lock_idx": lock_idx,
                    "reset_on_breach": reset_on_breach,
                    "assessment_fee": fee, "start": start_date,
                })

        # Per-program account limits
        _prog_counts = {}
        for acc in account_configs:
            if acc["enabled"]:
                _prog_counts[acc["program"]] = _prog_counts.get(acc["program"], 0) + 1
        for prog, cnt in _prog_counts.items():
            limit = concept[prog].get("_max_accounts")
            if limit and cnt > limit:
                st.error(f"{cnt} {prog} accounts enabled — TCT allows a maximum of "
                         f"{limit} active {prog} accounts.")

    # Budget sanity: combined strategy budgets vs each account's DD limit
    _total_budget = sum(strat_cfgs[si]["budget_pct"] for si in active)
    for acc in account_configs:
        if acc["enabled"] and _total_budget > acc["dd_pct"]:
            st.warning(f"**{acc['label']}**: combined strategy DD budgets "
                       f"({_total_budget:g}%) exceed this account's {acc['dd_pct']:g}% max "
                       f"drawdown — if the strategies hit their historical DDs together, the "
                       f"account breaches. Reduce budget DD% or run fewer strategies.",
                       icon="⚠️")

    # ── Run simulations ───────────────────────────────────────────────────
    results, all_events, all_logs, all_diags = {}, {}, {}, {}
    for i, acc in enumerate(account_configs):
        if not acc["enabled"]:
            continue
        acc_stream = stream[stream["date"] >= acc["start"]]
        sim, events, log, diag = simulate_concept_account(acc_stream, acc, strat_cfgs)
        if not sim.empty:
            results[i], all_events[i] = sim, events
            all_logs[i], all_diags[i] = log, diag

    if not results:
        st.warning("No simulation results — check account configuration.")
        return

    # ── Summary metrics ───────────────────────────────────────────────────
    st.markdown("---")
    total_net  = sum(sum(e["net"] for e in ev) for ev in all_events.values())
    # Every breached-and-reset account re-pays the assessment fee
    total_fees = sum(account_configs[i]["assessment_fee"] * all_diags[i].get("attempts", 1)
                     for i in results)
    total_lost = sum(all_diags[i].get("breaches", 0) for i in results)
    all_dates = pd.to_datetime(pd.concat([pd.Series(r["date"]) for r in results.values()],
                                         ignore_index=True))
    n_months = max(1, (all_dates.max() - all_dates.min()).days / 30.44)

    def _top_level(i):
        acc = account_configs[i]
        _bi = all_diags[i].get("best_idx", all_diags[i]["final_idx"])
        return str(acc["levels"][_bi]["level"])

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Active Accounts", len(results))
    m2.metric("Best Level Reached", " / ".join(_top_level(i) for i in results))
    m3.metric(f"Total Net {acct_ccy}", f"{total_net:,.0f}")
    m4.metric("Net After Fees", f"{total_net - total_fees:,.0f}",
              help=f"Total fees {total_fees:,.0f} AUD across all accounts incl. re-buys "
                   f"after breaches — match the account currency for a fair figure.")
    m5.metric("Avg Monthly Net", f"{total_net / n_months:,.0f}")
    m6.metric("Accounts Lost", total_lost)

    _forced_total = sum(d.get("forced_min_trades", 0) for d in all_diags.values())
    if _forced_total:
        st.warning(f"{_forced_total} trades were taken at the **forced minimum lot** — the "
                   f"level's starting equity was below the balance-per-step for that strategy, "
                   f"so those trades ran oversized relative to the DD budget. The account "
                   f"trades anyway and fails if it fails: consider dropping or de-risking "
                   f"those strategies. See the Level Detail tab (⚠ marks forced levels).",
                   icon="⚠️")

    # ── Tabs ──────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "📈 Equity & Levels", "💰 Payout Events", "📅 Monthly Income",
        "🪜 Level Detail", "🎲 Monte Carlo", "🏆 Tier Comparison",
    ])

    # ── TAB 1 — Equity & levels
    with tab1:
        y_mode = st.radio("Y axis", ["% of level equity", f"Equity ({acct_ccy}, log)"],
                          horizontal=True, key="tct_ymode",
                          help="% of level equity normalises each level to its own starting "
                               "equity — every level plays out between the same target and "
                               "breach lines, so risk consistency across the scaling is "
                               "visible at a glance.")
        pct_mode = y_mode.startswith("%")
        fig = go.Figure()
        for i, sim in results.items():
            acc = account_configs[i]
            col = COLOURS[i % len(COLOURS)]
            if pct_mode:
                y = (sim["equity"] - sim["se"]) / sim["se"] * 100
                fig.add_trace(go.Scatter(x=sim["close_time"], y=y,
                                         name=acc["label"], line=dict(color=col, width=1.5)))
                # Target / breach lines for this account's rule set
                fig.add_hline(y=acc["pt_pct"], line_dash="dash", line_color=col,
                              line_width=1, opacity=0.5,
                              annotation_text=f"target +{acc['pt_pct']:g}%",
                              annotation_font_color=col, annotation_font_size=10)
                fig.add_hline(y=-acc["dd_pct"], line_dash="solid", line_color="#ef5350",
                              line_width=1, opacity=0.6,
                              annotation_text=f"breach −{acc['dd_pct']:g}%",
                              annotation_font_color="#ef5350", annotation_font_size=10)
                for ev in all_events.get(i, []):
                    fig.add_trace(go.Scatter(
                        x=[pd.Timestamp(ev["date"])], y=[0], mode="markers",
                        marker=dict(symbol="diamond", size=8, color=col,
                                    line=dict(color="white", width=1)),
                        showlegend=False))
            else:
                fig.add_trace(go.Scatter(x=sim["close_time"], y=sim["equity"],
                                         name=acc["label"], line=dict(color=col, width=2)))
                fig.add_trace(go.Scatter(x=sim["close_time"], y=sim["se"],
                                         name=f"{acc['label']} level SE",
                                         line=dict(color=col, width=1, dash="dot"),
                                         opacity=0.6))
                for ev in all_events.get(i, []):
                    fig.add_trace(go.Scatter(
                        x=[pd.Timestamp(ev["date"])], y=[max(ev["equity"], 1)], mode="markers",
                        marker=dict(symbol="diamond", size=9, color=col,
                                    line=dict(color="white", width=1)),
                        showlegend=False))
            _br = sim[sim["breached"]]
            if not _br.empty:
                _br_y = ((_br["equity"] - _br["se"]) / _br["se"] * 100) if pct_mode \
                        else _br["equity"]
                fig.add_trace(go.Scatter(
                    x=_br["close_time"], y=_br_y, mode="markers",
                    marker=dict(symbol="x", size=10, color="#ef5350"),
                    name=f"{acc['label']} breach", showlegend=False))
        if pct_mode:
            _yaxis = dict(gridcolor="#2d3250", title="Distance from level start (%)",
                          ticksuffix="%", zeroline=True, zerolinecolor="#555")
        else:
            _yaxis = dict(gridcolor="#2d3250", title=f"Equity ({acct_ccy})",
                          tickformat=",.0f", type="log")
        fig.update_layout(
            height=480, margin=dict(l=0, r=0, t=30, b=0),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#ccc"),
            xaxis=dict(gridcolor="#2d3250"),
            yaxis=_yaxis,
            legend=dict(orientation="h", y=-0.12),
        )
        st.plotly_chart(fig, width="stretch", key="tct_equity")
        if pct_mode:
            st.markdown('<small>◆ = payout / level-up (resets to 0%) · ✕ = breach (account '
                        'lost, restarts at the start level when reset is on) · every level '
                        'plays out between the same target and breach lines regardless of '
                        'account size — if the line behaves differently at later levels, risk '
                        'is not scaling consistently</small>', unsafe_allow_html=True)
        else:
            st.markdown('<small>◆ = payout · ✕ = breach · dotted = level starting equity</small>',
                        unsafe_allow_html=True)

    # ── TAB 2 — Payout events
    with tab2:
        rows_ev = []
        for i, events in all_events.items():
            acc = account_configs[i]
            for ev in events:
                rows_ev.append({
                    "Account": acc["label"], "Date": str(ev["date"]),
                    "Gross": f"{ev['gross']:,.0f}", "Net": f"{ev['net']:,.0f}",
                    "Cumulative Net": f"{ev['cumulative']:,.0f}",
                })
        if rows_ev:
            st.dataframe(pd.DataFrame(rows_ev), width="stretch", hide_index=True)
        else:
            st.info("No payouts yet — no level target has been hit.")

    # ── TAB 3 — Monthly income
    with tab3:
        monthly_rows = [{"date": pd.Timestamp(ev["date"]), "net": ev["net"]}
                        for events in all_events.values() for ev in events]
        if monthly_rows:
            ev_monthly = pd.DataFrame(monthly_rows)
            ev_monthly["label"] = ev_monthly["date"].dt.to_period("M")
            monthly_agg = ev_monthly.groupby("label")["net"].sum().reset_index()
            monthly_agg["label"] = monthly_agg["label"].dt.strftime("%b %Y")
            fig2 = go.Figure(go.Bar(
                x=monthly_agg["label"], y=monthly_agg["net"],
                marker_color="#26a69a",
                text=[f"{v:,.0f}" for v in monthly_agg["net"]], textposition="outside"))
            fig2.update_layout(
                height=350, margin=dict(l=0, r=0, t=30, b=60),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#ccc"),
                xaxis=dict(gridcolor="#2d3250", tickangle=-45),
                yaxis=dict(gridcolor="#2d3250", title=f"Net {acct_ccy}", tickformat=",.0f"),
                title="Monthly Net Payouts (all accounts)")
            st.plotly_chart(fig2, width="stretch", key="tct_monthly")
            ms1, ms2, ms3 = st.columns(3)
            ms1.metric("Months with payout", len(monthly_agg))
            ms2.metric("Avg monthly", f"{monthly_agg['net'].mean():,.0f}")
            ms3.metric("Best month", f"{monthly_agg['net'].max():,.0f}")

            st.markdown("#### Calendar Heatmap")
            st.caption("Net payouts per calendar month · ▲n = level-ups that month")
            _lvl_dates = [l["end"] for log in all_logs.values()
                          for l in log if l["outcome"] == "passed"]
            _payout_heatmap(all_events, acct_ccy, "tct_heatmap", level_dates=_lvl_dates)
        else:
            st.info("No payouts to display.")

    # ── TAB 4 — Level detail
    with tab4:
        for i, sim in results.items():
            acc = account_configs[i]
            st.markdown(f"#### {acc['label']}")

            log  = all_logs[i]
            diag = all_diags[i]
            if log:
                log_rows = []
                for lv in log:
                    days = (pd.Timestamp(lv["end"]) - pd.Timestamp(lv["start"])).days if lv["start"] else 0
                    log_rows.append({
                        "Acct #": lv.get("attempt", 1),
                        "Level": lv["level"], "SE": f"{lv['se']:,.0f}",
                        "From": str(lv["start"]), "To": str(lv["end"]),
                        "Trades": lv["trades"], "Days": days,
                        "Months": f"{days / 30.44:.1f}",
                        "Outcome": lv["outcome"],
                        "Net Payout": f"{lv['net_payout']:,.0f}",
                    })
                st.dataframe(pd.DataFrame(log_rows), width="stretch", hide_index=True)

                passed = [l for l in log if l["outcome"] == "passed"]
                _fees  = acc["assessment_fee"] * diag.get("attempts", 1)
                p1, p2, p3, p4, p5 = st.columns(5)
                p1.metric("Levels passed", len(passed))
                p2.metric("Accounts lost", diag.get("breaches", 0))
                p3.metric("Fees paid (AUD)", f"{_fees:,.0f}",
                          help="Assessment fee × accounts bought (initial + re-buys)")
                if passed:
                    _avg_days = np.mean([
                        (pd.Timestamp(l["end"]) - pd.Timestamp(l["start"])).days
                        for l in passed if l["start"]])
                    _best = diag.get("best_idx", diag["final_idx"])
                    _remaining = len(acc["levels"]) - 1 - _best
                    p4.metric("Avg months / level", f"{_avg_days / 30.44:.1f}")
                    if _remaining > 0:
                        p5.metric("Est. months to top",
                                  f"{_remaining * _avg_days / 30.44:.0f}",
                                  help="Remaining levels (from best reached) x avg months per "
                                       "level — assumes history-like performance continues.")

            # Lot fit per level
            st.markdown("##### Lots per level")
            st.caption("⚠ = forced minimum lot — the level is too small for this strategy's "
                       "budget-derived size, so it runs oversized risk.")
            fit_rows = []
            for lv in acc["levels"]:
                row = {"Level": lv["level"], "SE": f"{lv['se']:,.0f}"}
                for si in active:
                    sc = strat_cfgs[si]
                    lots = _lots_for(float(lv["se"]), sc)
                    row[sc["name"]] = f"{lots:g}" + (" ⚠" if _is_forced_min(float(lv["se"]), sc) else "")
                fit_rows.append(row)
            st.dataframe(pd.DataFrame(fit_rows), width="stretch", hide_index=True)
            st.markdown("---")

    # ── TAB 5 — Monte Carlo
    with tab5:
        st.markdown("*Reshuffle the trade sequence to estimate breach risk and the "
                    "distribution of levels reached.*")
        mc1, mc2, mc3 = st.columns(3)
        with mc1:
            mc_sims = st.slider("Simulations", 100, 2000, 300, step=100, key="tct_mc_sims")
        with mc2:
            mc_acc_idx = st.selectbox("Account", list(results.keys()),
                                      format_func=lambda i: account_configs[i]["label"],
                                      key="tct_mc_acc")
        with mc3:
            mc_block = st.radio("Block size", ["Per trade", "Weekly", "Monthly"],
                                key="tct_mc_block", horizontal=True)

        if st.button("Run Monte Carlo", key="tct_mc_run", type="primary"):
            acc = account_configs[mc_acc_idx]
            mc_trades = stream[stream["date"] >= acc["start"]].reset_index(drop=True)
            n = len(mc_trades)
            if n < 10:
                st.warning("Need at least 10 trades for Monte Carlo.")
                return

            rng = np.random.default_rng(42)
            block_indices = _bootstrap_blocks(mc_trades, mc_block)
            n_blocks = len(block_indices)
            if n_blocks < 3:
                st.warning(f"Only {n_blocks} complete blocks — need at least 3.")
                return

            mc_breached = 0
            mc_net   = np.zeros(mc_sims)
            mc_lost  = np.zeros(mc_sims, dtype=int)
            mc_level = np.zeros(mc_sims, dtype=int)
            orig_ct = mc_trades["close_time"].values
            orig_dt = mc_trades["date"].values

            progress = st.progress(0, text="Running simulations...")
            for k in range(mc_sims):
                pieces, total = [], 0
                while total < n:
                    b = block_indices[rng.integers(0, n_blocks)]
                    pieces.extend(b)
                    total += len(b)
                shuffled = mc_trades.iloc[pieces[:n]].reset_index(drop=True)
                shuffled["close_time"] = orig_ct
                shuffled["date"] = orig_dt

                sim_df, sim_events, _, diag = simulate_concept_account(shuffled, acc, strat_cfgs)
                if diag["breached"]:
                    mc_breached += 1
                mc_net[k]   = sum(e["net"] for e in sim_events)
                mc_lost[k]  = diag.get("breaches", 0)
                mc_level[k] = diag.get("best_idx", diag["final_idx"])
                if k % max(1, mc_sims // 20) == 0:
                    progress.progress((k + 1) / mc_sims, text=f"Simulation {k+1}/{mc_sims}")
            progress.empty()

            levels = acc["levels"]
            st.markdown("#### Results")
            r1, r2, r3, r4, r5 = st.columns(5)
            r1.metric("Paths with a Breach", f"{mc_breached / mc_sims * 100:.1f}%")
            r2.metric("Avg Accounts Lost", f"{mc_lost.mean():.1f}")
            r3.metric("Median Best Level", str(levels[int(np.median(mc_level))]["level"]))
            r4.metric("Median Net Payout", f"{np.median(mc_net):,.0f}")
            r5.metric("95th %ile Payout", f"{np.percentile(mc_net, 95):,.0f}")

            # Level distribution
            lvl_counts = pd.Series(mc_level).value_counts().sort_index()
            fig_lvl = go.Figure(go.Bar(
                x=[str(levels[j]["level"]) for j in lvl_counts.index],
                y=lvl_counts.values, marker_color="#7c6af7", opacity=0.8,
                text=[f"{v / mc_sims * 100:.0f}%" for v in lvl_counts.values],
                textposition="outside"))
            fig_lvl.update_layout(
                title="Best level reached across simulations",
                height=320, margin=dict(l=0, r=0, t=40, b=0),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#ccc"),
                xaxis=dict(gridcolor="#2d3250", title="Level"),
                yaxis=dict(gridcolor="#2d3250", title="Simulations"))
            st.plotly_chart(fig_lvl, width="stretch", key="tct_mc_levels")

            fig_pay = go.Figure(go.Histogram(x=mc_net, nbinsx=50,
                                             marker_color="#26a69a", opacity=0.7))
            fig_pay.update_layout(
                height=300, margin=dict(l=0, r=0, t=20, b=0),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#ccc"),
                xaxis=dict(gridcolor="#2d3250", title=f"Total Net Payout ({acct_ccy})",
                           tickformat=",.0f"),
                yaxis=dict(gridcolor="#2d3250", title="Count"))
            st.plotly_chart(fig_pay, width="stretch", key="tct_mc_pay")

        # ── Budget sweep ──────────────────────────────────────────────────
        st.markdown("---")
        st.markdown("##### Budget Sweep")
        st.caption("Re-runs the Monte Carlo at several budget DD% levels (replacing every "
                   "included strategy's budget with the sweep value) to find the knee where "
                   "breach risk collapses while the climb only slows modestly. Uses the "
                   "account and block size selected above; identical bootstrap paths per "
                   "budget for a fair comparison.")
        sw1, sw2 = st.columns(2)
        _sweep_txt  = sw1.text_input("Budget DD% levels (comma-separated)",
                                     value="1, 2, 3, 4", key="tct_sweep_budgets")
        _sweep_sims = sw2.slider("Sims per budget", 50, 1000, 200, step=50,
                                 key="tct_sweep_sims")

        if st.button("Run budget sweep", key="tct_sweep_run", type="primary"):
            try:
                budgets = sorted({round(float(x), 2) for x in
                                  _sweep_txt.replace(";", ",").split(",") if x.strip()})
                budgets = [b for b in budgets if b > 0]
            except ValueError:
                budgets = []
            if not budgets:
                st.warning("Enter budgets like: 1, 2, 3, 4")
            else:
                acc = account_configs[mc_acc_idx]
                mc_trades = stream[stream["date"] >= acc["start"]].reset_index(drop=True)
                n = len(mc_trades)
                block_indices = _bootstrap_blocks(mc_trades, mc_block)
                n_blocks = len(block_indices)
                if n < 10 or n_blocks < 3:
                    st.warning("Not enough trades / complete blocks for the sweep.")
                else:
                    orig_ct = mc_trades["close_time"].values
                    orig_dt = mc_trades["date"].values
                    rows_sw = []
                    total_runs = len(budgets) * _sweep_sims
                    done = 0
                    progress = st.progress(0, text="Sweeping budgets...")
                    for b in budgets:
                        scs_b = [dict(sc, bal_per_step=sc["bal_per_step"]
                                      * sc["budget_pct"] / b)
                                 for sc in strat_cfgs]
                        rng = np.random.default_rng(42)   # same paths for every budget
                        s_net  = np.zeros(_sweep_sims)
                        s_lost = np.zeros(_sweep_sims, dtype=int)
                        s_lvl  = np.zeros(_sweep_sims, dtype=int)
                        s_br   = 0
                        for k in range(_sweep_sims):
                            pieces, total = [], 0
                            while total < n:
                                blk = block_indices[rng.integers(0, n_blocks)]
                                pieces.extend(blk)
                                total += len(blk)
                            shuffled = mc_trades.iloc[pieces[:n]].reset_index(drop=True)
                            shuffled["close_time"] = orig_ct
                            shuffled["date"] = orig_dt
                            _, ev_s, _, dg = simulate_concept_account(shuffled, acc, scs_b)
                            if dg["breached"]:
                                s_br += 1
                            s_net[k]  = sum(e["net"] for e in ev_s)
                            s_lost[k] = dg.get("breaches", 0)
                            s_lvl[k]  = dg.get("best_idx", dg["final_idx"])
                            done += 1
                            if done % max(1, total_runs // 25) == 0:
                                progress.progress(done / total_runs,
                                                  text=f"Budget {b:g}% — {done}/{total_runs}")
                        _med_ix = int(np.median(s_lvl))
                        rows_sw.append({
                            "Budget %": b,
                            "Paths Breached %": round(s_br / _sweep_sims * 100, 1),
                            "Avg Accounts Lost": round(float(s_lost.mean()), 2),
                            "Median Best Level": str(acc["levels"][_med_ix]["level"]),
                            "_lvl_ix": _med_ix,
                            "Median Net": round(float(np.median(s_net))),
                            "95th %ile Net": round(float(np.percentile(s_net, 95))),
                        })
                    progress.empty()
                    st.session_state["tct_sweep_result"] = {
                        "df": pd.DataFrame(rows_sw), "account": acc["label"],
                    }

        _sw = st.session_state.get("tct_sweep_result")
        if _sw:
            _swdf = _sw["df"]
            st.markdown(f"**{_sw['account']}** — breach risk vs climb speed by budget")
            st.dataframe(
                _swdf.drop(columns=["_lvl_ix"]).style.format(
                    {"Budget %": "{:g}", "Median Net": "{:,.0f}", "95th %ile Net": "{:,.0f}"}),
                width="stretch", hide_index=True)

            _x = [f"{v:g}%" for v in _swdf["Budget %"]]
            fig_sw = go.Figure()
            fig_sw.add_trace(go.Bar(
                x=_x, y=_swdf["Paths Breached %"],
                name="Paths breached %", marker_color="#ef5350", opacity=0.75))
            fig_sw.add_trace(go.Scatter(
                x=_x, y=_swdf["_lvl_ix"],
                name="Median best level", mode="lines+markers+text",
                text=_swdf["Median Best Level"], textposition="top center",
                textfont=dict(color="#26a69a"),
                line=dict(color="#26a69a", width=2), yaxis="y2"))
            fig_sw.update_layout(
                height=380, margin=dict(l=0, r=0, t=30, b=0),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#ccc"),
                xaxis=dict(gridcolor="#2d3250", title="Budget DD% per strategy"),
                yaxis=dict(gridcolor="#2d3250", title="Paths breached (%)"),
                yaxis2=dict(overlaying="y", side="right", showgrid=False,
                            title="Median best level (index)"),
                legend=dict(orientation="h", y=-0.25),
            )
            st.plotly_chart(fig_sw, width="stretch", key="tct_sweep_chart")
            st.caption("Look for the knee: the largest budget where breach risk is still "
                       "near zero — beyond it you're trading survival for climb speed.")

    # ── TAB 6 — Tier comparison
    with tab6:
        st.markdown("*The same strategy portfolio run through **every** TCT tier from Intern "
                    "(no lock, full history) — which program should the next account be?*")
        if st.button("Run 17-tier comparison", key="tct_cmp_run", type="primary"):
            combos = [(p, t) for p, pdict in concept.items()
                      for t in _concept_tiers(pdict)]
            cmp_rows = []
            progress = st.progress(0, text="Simulating tiers...")
            for k, (prog, tier) in enumerate(combos):
                spec = concept[prog][tier]
                cfg = {"levels": spec["levels"], "dd_pct": spec["dd_pct"],
                       "pt_pct": spec["pt_pct"], "start_idx": 0, "lock_idx": None,
                       "reset_on_breach": reset_on_breach}
                sim, ev, log, diag = simulate_concept_account(stream, cfg, strat_cfgs)
                net = sum(e["net"] for e in ev)
                fees = spec["assessment_fee"] * diag.get("attempts", 1)
                passed = [l for l in log if l["outcome"] == "passed"]
                if passed:
                    _avg_mo = np.mean([
                        (pd.Timestamp(l["end"]) - pd.Timestamp(l["start"])).days
                        for l in passed if l["start"]]) / 30.44
                else:
                    _avg_mo = None
                _first_pay_mo = ((pd.Timestamp(ev[0]["date"]) -
                                  pd.Timestamp(stream["date"].iloc[0])).days / 30.44
                                 if ev else None)
                best_lv = spec["levels"][diag.get("best_idx", diag["final_idx"])]
                cmp_rows.append({
                    "Program": prog, "Account": tier,
                    "Fees": fees,
                    "PT/DD": f"{spec['pt_pct']:g}% / {spec['dd_pct']:g}%",
                    "Best Level": f"{best_lv['level']} / {spec['levels'][-1]['level']}",
                    "Top?": "🏆" if best_lv.get("final") else "",
                    "Accounts Lost": diag.get("breaches", 0),
                    "Levels Passed": len(passed),
                    "Payouts": len(ev),
                    "Net": round(net),
                    "Net After Fees": round(net - fees),
                    "Avg mo/level": round(_avg_mo, 1) if _avg_mo is not None else None,
                    "1st Payout (mo)": round(_first_pay_mo, 1) if _first_pay_mo is not None else None,
                    "Forced-min Trades": diag.get("forced_min_trades", 0),
                })
                progress.progress((k + 1) / len(combos),
                                  text=f"Simulating {prog} {tier} ({k+1}/{len(combos)})")
            progress.empty()
            st.session_state["tct_cmp_result"] = pd.DataFrame(cmp_rows)

        _cmp = st.session_state.get("tct_cmp_result")
        if _cmp is not None and "Net After Fees" in _cmp.columns:
            _sort = st.checkbox("Sort by Net After Fees", value=True, key="tct_cmp_sort")
            _disp = _cmp.sort_values("Net After Fees", ascending=False) if _sort else _cmp
            st.dataframe(
                _disp.style.format({"Fees": "{:,.0f}", "Net": "{:,.0f}",
                                    "Net After Fees": "{:,.0f}"}),
                width="stretch", hide_index=True)
            st.caption(f"All figures {acct_ccy}. Fees include re-buys after breaches. "
                       f"Forced-min trades ran oversized vs the DD budget (level too small "
                       f"for the strategy's balance-per-step) — a high count means the "
                       f"result leans on oversized risk, not the plan.")

            fig_cmp = go.Figure(go.Bar(
                x=[f"{r.Program} {r.Account}" for r in _disp.itertuples()],
                y=_disp["Net After Fees"],
                marker_color=["#26a69a" if v >= 0 else "#ef5350"
                              for v in _disp["Net After Fees"]],
            ))
            fig_cmp.update_layout(
                title=f"Net after fees by tier ({acct_ccy})",
                height=380, margin=dict(l=0, r=0, t=40, b=90),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#ccc"),
                xaxis=dict(gridcolor="#2d3250", tickangle=-45),
                yaxis=dict(gridcolor="#2d3250", tickformat=",.0f"))
            st.plotly_chart(fig_cmp, width="stretch", key="tct_cmp_chart")
        else:
            st.info("Click **Run 17-tier comparison** to simulate every tier with the current "
                    "strategy portfolio and sizing settings.")


# ─────────────────────────────────────────────────────────────────────────────
# Page
# ─────────────────────────────────────────────────────────────────────────────
def render():
    st.title("🏦 Prop Planner")
    st.markdown(
        '<div class="info-card">Simulate prop firm accounts trade-by-trade from MT5 backtest '
        'reports. Position size uses the set-file lot-step method: each report\'s '
        '<b>Equity Drawdown Maximal</b> ÷ budget DD% gives the balance required per lot step.</div>',
        unsafe_allow_html=True)

    parser = _get_parser()

    up_col, dir_col = st.columns([3, 2])
    with up_col:
        uploads = st.file_uploader(
            "Upload MT5 backtest HTML reports (fixed-lot backtests)",
            type=["htm", "html"], accept_multiple_files=True, key="prop_uploads")
    with dir_col:
        folder = st.text_input("…or load all reports from a folder",
                               value="", key="prop_folder",
                               placeholder=r"C:\path\to\reports")
        if folder and not os.path.isdir(folder):
            st.warning("Folder not found.")

    class _DiskFile:
        """Minimal shim so folder-loaded files share the upload code path."""
        def __init__(self, path):
            self._path = path
            self.name = os.path.basename(path)
        def read(self):
            with open(self._path, "rb") as fh:
                return fh.read()
        def seek(self, *_):
            pass

    sources = list(uploads or [])
    if folder and os.path.isdir(folder):
        disk_paths = sorted(glob.glob(os.path.join(folder, "*.htm")) +
                            glob.glob(os.path.join(folder, "*.html")))
        sources += [_DiskFile(p) for p in disk_paths]
        st.caption(f"Loaded {len(disk_paths)} report(s) from folder.")

    if not sources:
        st.info("Upload one or more MT5 Strategy Tester HTML reports to begin.")
        return

    # ── Parse uploads ─────────────────────────────────────────────────────
    strategies = []   # per-strategy dicts: df, summary, config
    for f in sources:
        raw = f.read()
        f.seek(0)
        try:
            df, fmt = parser.detect_and_parse(raw, f.name)
        except Exception as e:
            st.error(f"Failed to parse **{f.name}**: {e}")
            continue
        if df is None or len(df) == 0:
            st.error(f"No trades found in **{f.name}**")
            continue
        if fmt != "MT5 Backtest Report":
            st.warning(f"**{f.name}** parsed as *{fmt}* — this page expects Strategy Tester "
                       f"backtest reports. Skipping.")
            continue
        summary = parser.parse_backtest_summary(raw) or {}
        _stem = os.path.splitext(f.name)[0]
        # Multi-strategy files are labelled by filename; single-strategy by comment
        _n_str = df["strategy"].nunique() if "strategy" in df.columns else 1
        _label = _stem if _n_str > 1 else (df["strategy"].iloc[0] or _stem)
        strategies.append({"name": _label,
                           "file": f.name, "df": df, "summary": summary})

    if not strategies:
        return

    # ── Global settings ───────────────────────────────────────────────────
    with st.expander("⚙️ Sizing & Currency (Global)", expanded=True):
        g1, g2, g3 = st.columns(3)
        with g1:
            acct_ccy = st.radio("Account currency", ["USD", "AUD"], horizontal=True,
                                key="prop_acct_ccy",
                                help="Reports are converted into this currency before simulation.")
        with g2:
            audusd = st.number_input("AUDUSD rate", value=0.6550, step=0.005, format="%.4f",
                                     key="prop_audusd",
                                     help="Used only when report and account currencies differ.")
        with g3:
            sizing_basis = st.radio("Sizing basis", ["Current equity (compound)", "Initial balance (fixed)"],
                                    key="prop_basis",
                                    help="Compound recalculates lots from live equity each trade; "
                                         "fixed keeps the lots implied by the starting balance.")
        compound = sizing_basis.startswith("Current")

    def _ccy_factor(report_ccy):
        """Multiply report-currency amounts by this to get account currency."""
        rc = (report_ccy or "USD").upper()
        if rc == acct_ccy:
            return 1.0
        if rc == "USD" and acct_ccy == "AUD":
            return 1.0 / audusd if audusd > 0 else 1.0
        if rc == "AUD" and acct_ccy == "USD":
            return audusd
        return 1.0

    # ── Per-strategy sizing config ────────────────────────────────────────
    with st.expander("🎯 Strategy Sizing", expanded=True):
        st.caption("Max DD defaults to the report's Equity Drawdown Maximal at the backtest "
                   "lot size. Budget DD% is how much of the account you allocate to that "
                   "strategy's historical max DD. Reports that tag multiple strategies in "
                   "the comment field can be split for EA-style per-strategy sizing.")
        strat_cfgs = []   # each entry carries its own trade subset in "_df"
        for si, s in enumerate(strategies):
            summ = s["summary"]
            df   = s["df"]
            vols = df["volume"].dropna()
            bt_vol = float(vols.mode().iloc[0]) if len(vols) else 0.01
            fixed_vol = bool(len(vols) and vols.nunique() == 1)
            sym_base  = str(df["symbol_base"].iloc[0]) if "symbol_base" in df.columns else ""
            report_ccy = summ.get("currency", "USD")
            dd_detected = summ.get("equity_dd_max")
            fx = _ccy_factor(report_ccy)
            strats_in_file = sorted(df["strategy"].dropna().unique().tolist()) \
                if "strategy" in df.columns else []

            c0, c1, c2, c3, c4 = st.columns([2.2, 1.2, 1.2, 1.2, 1.6])
            with c0:
                include = st.checkbox(f"**{s['name']}**", value=True, key=f"prop_inc_{si}")
                st.caption(f"{sym_base} · {summ.get('period','?')} · {len(df)} trades · "
                           f"vol {bt_vol:g}{'' if fixed_vol else ' ⚠️ variable'} · {report_ccy}")
                if not fixed_vol:
                    st.warning("Variable lot sizes detected — PnL is normalised per lot per "
                               "trade, but the report's DD figure assumes the EA's own sizing. "
                               "Prefer a fixed-lot backtest for this strategy.", icon="⚠️")
                split = False
                if len(strats_in_file) > 1:
                    split = st.checkbox(f"Split into {len(strats_in_file)} comment strategies",
                                        value=False, key=f"prop_split_{si}",
                                        help="EA-style internal balancing: each comment-tagged "
                                             "strategy gets its own Max DD and Risk %.")
            with c3:
                step_default = 0.1 if _is_index_symbol(sym_base) else 0.01
                lot_step = st.selectbox("Lot step", [0.01, 0.1],
                                        index=1 if step_default == 0.1 else 0,
                                        key=f"prop_step_{si}")

            if not split:
                with c1:
                    max_dd = st.number_input("Max DD (report ccy)",
                                             value=float(dd_detected) if dd_detected else 100.0,
                                             min_value=0.01, step=1.0, key=f"prop_dd_{si}",
                                             help="Equity Drawdown Maximal at the backtest lot "
                                                  "size. Override if you have a better figure.")
                with c2:
                    budget = st.number_input("Budget DD %", value=2.0, min_value=0.1,
                                             max_value=100.0, step=0.5, key=f"prop_bud_{si}",
                                             help="e.g. 2% budget on a 5% max-DD account "
                                                  "leaves wiggle room.")
                dd_per_step  = (max_dd / bt_vol) * fx * lot_step if bt_vol > 0 else 0.0
                bal_per_step = dd_per_step / (budget / 100) if budget > 0 else 0.0
                with c4:
                    st.metric("Balance / step", f"{bal_per_step:,.0f} {acct_ccy}",
                              help=f"DD per {lot_step:g} lot = {dd_per_step:,.2f} {acct_ccy} "
                                   f"÷ {budget:g}% budget")
                strat_cfgs.append({
                    "name": s["name"], "include": include, "symbol": sym_base,
                    "max_dd": max_dd, "budget_pct": budget, "lot_step": lot_step,
                    "bal_per_step": bal_per_step, "fx": fx, "bt_vol": bt_vol,
                    "_df": df,
                })
            else:
                with c2:
                    budget = st.number_input("Default risk %", value=2.0, min_value=0.1,
                                             max_value=100.0, step=0.5, key=f"prop_bud_{si}",
                                             help="Pre-fills the Risk % column — edit per "
                                                  "strategy in the table.")
                with c4:
                    st.metric("Strategies", len(strats_in_file))
                _rows = []
                for _sn in strats_in_file:
                    _g = df[df["strategy"] == _sn]
                    _dd_s = abs(drawdown_stats(ordered_profits(_g), 0.0)["max_dd"]) or 1.0
                    _rows.append({"Strategy": _sn, "Trades": len(_g),
                                  "Max DD": round(_dd_s, 2), "Risk %": budget})
                st.caption("Max DD defaults to each strategy's balance DD from its own "
                           "trades (report ccy, backtest lots) — override with the EA's "
                           "backtest figures if you have them.")
                _ed = st.data_editor(
                    pd.DataFrame(_rows), hide_index=True, width="stretch",
                    column_config={
                        "Strategy": st.column_config.TextColumn(disabled=True),
                        "Trades":   st.column_config.NumberColumn(disabled=True, format="%d"),
                        "Max DD":   st.column_config.NumberColumn(format="%.2f"),
                        "Risk %":   st.column_config.NumberColumn(format="%.2f"),
                    },
                    key=f"prop_ddtable_{si}")
                for _, r_ in _ed.iterrows():
                    _dd_v = max(float(r_["Max DD"]), 0.01)
                    _rk   = max(float(r_["Risk %"]), 0.01)
                    _bps  = (_dd_v / bt_vol) * fx * lot_step / (_rk / 100) if bt_vol > 0 else 0.0
                    strat_cfgs.append({
                        "name": f"{s['name']} — {r_['Strategy']}", "include": include,
                        "symbol": sym_base, "max_dd": _dd_v, "budget_pct": _rk,
                        "lot_step": lot_step, "bal_per_step": _bps, "fx": fx,
                        "bt_vol": bt_vol,
                        "_df": df[df["strategy"] == r_["Strategy"]],
                    })

    active = [i for i, sc in enumerate(strat_cfgs) if sc["include"]]
    if not active:
        st.warning("No strategies included.")
        return

    # ── Build merged trade stream ─────────────────────────────────────────
    frames = []
    for i in active:
        sc = strat_cfgs[i]
        df = sc["_df"]
        d = pd.DataFrame({
            "close_time":  df["close_time"],
            "pnl_per_lot": df["net_profit"] / df["volume"].replace(0, np.nan) * sc["fx"],
            "strat":       i,
        }).dropna(subset=["close_time", "pnl_per_lot"])
        frames.append(d)

    stream = pd.concat(frames).sort_values("close_time").reset_index(drop=True)
    stream["date"] = stream["close_time"].dt.date
    run_start, run_end = stream["date"].min(), stream["date"].max()
    st.caption(f"Merged stream: {len(stream)} trades · {run_start} → {run_end}")

    # ── Firm type ─────────────────────────────────────────────────────────
    firm_mode = st.radio("Firm type",
                         ["Classic (FTMO / Blue Guardian / Custom)",
                          "Concept Trading (leveling accounts)",
                          "Personal capital (own rules)"],
                         horizontal=True, key="prop_firm_mode")
    if firm_mode.startswith("Concept"):
        _render_concept(stream, strat_cfgs, active, acct_ccy, run_start, run_end)
        return
    personal = firm_mode.startswith("Personal")

    # ── Account configuration ─────────────────────────────────────────────
    if personal:
        selected_firm = "Personal"
        st.caption("Your own capital with the same discipline applied — max drawdown, max "
                   "daily loss, and a withdrawal rule. Withdrawals are 100% yours; there is "
                   "no equity cap. The daily loss limit acts as a **circuit breaker**: "
                   "trading stops for the rest of that day and resumes the next — only the "
                   "max drawdown limit ends the account.")
    else:
        _firm_col, _ = st.columns([2, 3])
        with _firm_col:
            selected_firm = st.selectbox("Prop Firm Template",
                                         [k for k in PROP_FIRM_PRESETS if k != "Personal"],
                                         key="prop_firm")
    firm = PROP_FIRM_PRESETS[selected_firm]
    _fk = selected_firm[:3].lower()   # key prefix so widget defaults reset on firm switch

    with st.expander("🏦 Account Configuration", expanded=True):
        n_accounts = st.slider("Number of accounts", 1, 4, 1, key="prop_n_acc")
        if not personal:
            reset_classic = st.checkbox(
                "Reset to a new account on breach",
                value=True, key="prop_reset",
                help="A breach burns the account; the sim buys a fresh one (fee re-paid) and "
                     "keeps walking, so the whole backtest period is assessed. Untick to stop "
                     "at the first breach.")
        else:
            reset_classic = False
        account_configs = []
        _catalog = firm.get("accounts")
        _fee_ccy = firm.get("fee_currency", acct_ccy)
        cols = st.columns(n_accounts)
        for i, col in enumerate(cols):
            with col:
                st.markdown(f"**Account {i+1}**")
                enabled = st.checkbox("Enabled", value=True, key=f"prop_acc_en_{i}")
                label   = st.text_input("Label", value=f"{selected_firm} #{i+1}",
                                        key=f"{_fk}_prop_acc_lbl_{i}")
                start   = st.date_input("Start date", value=run_start,
                                        min_value=run_start, max_value=run_end,
                                        key=f"prop_acc_start_{i}")
                if personal or not _catalog:
                    account_size = st.number_input(f"Account size ({acct_ccy})",
                                                   value=firm["account_size"], step=1000.0,
                                                   key=f"{_fk}_prop_acc_size_{i}")
                    account_fee = 0.0
                else:
                    _opts = [f"{a['size']:,.0f} — {_fee_ccy} {a['fee']:,.0f}"
                             for a in _catalog] + ["Custom size"]
                    _def_ix = next((ix for ix, a in enumerate(_catalog)
                                    if a["size"] == firm["account_size"]), 0)
                    _sel = st.selectbox("Available accounts", _opts, index=_def_ix,
                                        key=f"{_fk}_prop_acc_cat_{i}")
                    if _sel == "Custom size":
                        account_size = st.number_input(f"Account size ({acct_ccy})",
                                                       value=firm["account_size"], step=1000.0,
                                                       key=f"{_fk}_prop_acc_size_{i}")
                        _def_fee = 500.0
                    else:
                        _cat = _catalog[_opts.index(_sel)]
                        account_size = float(_cat["size"])
                        _def_fee = float(_cat["fee"])
                    account_fee = st.number_input(f"Account fee ({_fee_ccy})",
                                                  value=_def_fee, step=10.0,
                                                  key=f"{_fk}_prop_acc_fee_{i}_{_sel}",
                                                  help="Charged once per account bought — "
                                                       "initial plus each re-buy after a "
                                                       "breach when reset is on.")

                st.markdown("*Drawdown Rules*")
                drawdown_type = st.selectbox("Drawdown type", _DD_TYPE_OPTIONS,
                                             index=_DD_TYPE_OPTIONS.index(firm["drawdown_type"]),
                                             format_func=lambda x: _DD_TYPE_LABELS[x],
                                             key=f"{_fk}_prop_acc_ddt_{i}")
                max_loss_pct = st.number_input("Max loss %", value=firm["max_loss_pct"],
                                               step=0.5, key=f"{_fk}_prop_acc_ml_{i}")
                daily_loss_type = st.radio("Daily loss limit", ["Percentage", "Fixed $"],
                                           index=0 if firm["daily_loss_type"] == "pct" else 1,
                                           key=f"{_fk}_prop_acc_dlt_{i}", horizontal=True)
                if daily_loss_type == "Percentage":
                    daily_loss_pct   = st.number_input("Daily loss %", value=firm["daily_loss_pct"],
                                                       step=0.5, key=f"{_fk}_prop_acc_dl_{i}")
                    daily_loss_fixed = 0.0
                else:
                    daily_loss_pct   = 0.0
                    daily_loss_fixed = st.number_input("Daily loss limit $",
                                                       value=firm["daily_loss_fixed"],
                                                       step=100.0, key=f"{_fk}_prop_acc_dlf_{i}")

                st.markdown("*Withdrawal Rules*" if personal else "*Payout Rules*")
                payout_threshold_pct = st.number_input(
                    "Withdraw at profit %" if personal else "Payout threshold %",
                    value=firm["payout_threshold_pct"],
                    step=1.0, key=f"{_fk}_prop_acc_pt_{i}")
                payout_amount = st.number_input(
                    "Withdrawal amount $" if personal else "Payout amount $",
                    value=firm["payout_amount"],
                    step=500.0, key=f"{_fk}_prop_acc_pa_{i}")
                if personal:
                    trader_split_pct = 100.0
                else:
                    trader_split_pct = st.number_input("Trader split %",
                                                       value=firm["trader_split_pct"],
                                                       step=1.0, key=f"{_fk}_prop_acc_sp_{i}")
                withdrawal_buffer_pct = st.number_input("Withdrawal buffer %",
                                                        value=firm["withdrawal_buffer_pct"],
                                                        step=0.1, key=f"{_fk}_prop_acc_wb_{i}",
                                                        help="Min equity buffer above floor after "
                                                             "withdrawal (% of initial balance)")

                if personal:
                    max_equity = firm["max_equity"]
                    cap_buffer = firm["cap_buffer"]
                else:
                    st.markdown("*Equity Cap*")
                    max_equity = st.number_input("Max equity $", value=firm["max_equity"],
                                                 step=1000.0, key=f"{_fk}_prop_acc_me_{i}")
                    cap_buffer = st.number_input("Cap buffer $", value=firm["cap_buffer"],
                                                 step=500.0, key=f"{_fk}_prop_acc_cb_{i}")

                account_configs.append({
                    "enabled": enabled, "label": label, "firm": selected_firm,
                    "start": start, "account_size": account_size,
                    "account_fee": account_fee, "reset_on_breach": reset_classic,
                    "daily_stop_only": personal,
                    "drawdown_type": drawdown_type, "max_loss_pct": max_loss_pct,
                    "daily_loss_type": "pct" if daily_loss_type == "Percentage" else "fixed",
                    "daily_loss_pct": daily_loss_pct, "daily_loss_fixed": daily_loss_fixed,
                    "payout_threshold_pct": payout_threshold_pct,
                    "payout_amount": payout_amount, "trader_split_pct": trader_split_pct,
                    "withdrawal_buffer_pct": withdrawal_buffer_pct,
                    "max_equity": max_equity, "cap_buffer": cap_buffer,
                })

    gcfg = {"compound": compound}

    # ── Run simulations ───────────────────────────────────────────────────
    results, all_events, all_diags = {}, {}, {}
    for i, acc in enumerate(account_configs):
        if not acc["enabled"]:
            continue
        acc_stream = stream[stream["date"] >= acc["start"]]
        sim, events, diag = simulate_account(acc_stream, acc, strat_cfgs, gcfg)
        if not sim.empty:
            results[i] = sim
            all_events[i] = events
            all_diags[i] = diag

    if not results:
        st.warning("No simulation results — check account configuration.")
        return

    # ── Summary metrics ───────────────────────────────────────────────────
    st.markdown("---")
    total_net    = sum(sum(e["net"] for e in ev) for ev in all_events.values())
    total_gross  = sum(sum(e["gross"] for e in ev) for ev in all_events.values())
    total_events = sum(len(ev) for ev in all_events.values())
    total_fees   = sum(account_configs[i].get("account_fee", 0.0)
                       * all_diags[i].get("attempts", 1) for i in results)
    total_lost   = sum(all_diags[i].get("breaches", 0) for i in results)

    all_dates = pd.to_datetime(pd.concat([pd.Series(r["date"]) for r in results.values()],
                                         ignore_index=True))
    n_months = max(1, (all_dates.max() - all_dates.min()).days / 30.44)

    m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
    m1.metric("Active Accounts", len(results))
    m2.metric("Total Withdrawals", total_events)
    m3.metric(f"Total Gross {acct_ccy}", f"{total_gross:,.0f}")
    m4.metric(f"Total Net {acct_ccy}", f"{total_net:,.0f}")
    m5.metric("Net After Fees", f"{total_net - total_fees:,.0f}",
              help=f"Fees {total_fees:,.0f} across all accounts bought (initial + re-buys). "
                   f"Fee currency may differ from the account currency — adjust the fee "
                   f"inputs if you want exact conversion.")
    m6.metric("Avg Monthly Net", f"{total_net / n_months:,.0f}")
    m7.metric("Accounts Lost", total_lost)

    _forced_total = sum(d.get("forced_min_trades", 0) for d in all_diags.values())
    if _forced_total:
        st.warning(f"{_forced_total} trades were taken at the **forced minimum lot** (balance "
                   f"below balance-per-step for that strategy) — those trades ran oversized "
                   f"relative to the DD budget.", icon="⚠️")

    # ── Tabs ──────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📈 Equity Curves", "💰 Payout Events", "📅 Monthly Income",
        "⚠️ Risk Summary", "🎲 Monte Carlo",
    ])

    # ── TAB 1 — Equity curves
    with tab1:
        fig = go.Figure()
        combined_equity = None
        for i, sim in results.items():
            acc = account_configs[i]
            col = COLOURS[i % len(COLOURS)]
            acc_sz = acc["account_size"]
            danger_lo = acc_sz * (1 - acc["max_loss_pct"] / 100)
            _dl_pct = acc.get("daily_loss_pct", 0)
            warn_lo = acc_sz * (1 - _dl_pct / 100) if _dl_pct > 0 else acc_sz - acc.get("daily_loss_fixed", 0)
            fig.add_hrect(y0=0, y1=danger_lo, fillcolor="rgba(239,83,80,0.06)", line_width=0)
            fig.add_hrect(y0=danger_lo, y1=warn_lo, fillcolor="rgba(255,152,0,0.04)", line_width=0)
            fig.add_hline(y=acc_sz, line_dash="dash", line_color=col, line_width=1, opacity=0.4)
            fig.add_trace(go.Scatter(x=sim["close_time"], y=sim["equity"],
                                     name=acc["label"], line=dict(color=col, width=2)))
            for ev in all_events.get(i, []):
                fig.add_trace(go.Scatter(
                    x=[pd.Timestamp(ev["date"])], y=[ev["equity"]], mode="markers",
                    marker=dict(symbol="diamond", size=10, color=col,
                                line=dict(color="white", width=1)),
                    showlegend=False))
            _br = sim[sim["breached"]]
            if not _br.empty:
                fig.add_trace(go.Scatter(
                    x=_br["close_time"], y=_br["equity"], mode="markers",
                    marker=dict(symbol="x", size=10, color="#ef5350"),
                    name=f"{acc['label']} breach", showlegend=False))
            s = sim.set_index("close_time")["equity"].rename(i)
            combined_equity = s.to_frame() if combined_equity is None else combined_equity.join(s, how="outer")

        if combined_equity is not None and len(results) > 1:
            combined_sum = combined_equity.ffill().sum(axis=1)
            fig.add_trace(go.Scatter(x=combined_sum.index, y=combined_sum.values,
                                     name="Combined", line=dict(color="#ffffff", width=2, dash="dot")))

        _y_floor = min(a["account_size"] * (1 - a["max_loss_pct"] / 100)
                       for a in account_configs if a["enabled"])
        fig.update_layout(
            height=480, margin=dict(l=0, r=0, t=30, b=0),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#ccc"),
            xaxis=dict(gridcolor="#2d3250"),
            yaxis=dict(gridcolor="#2d3250", title=f"Equity ({acct_ccy})",
                       tickformat=",.0f", range=[_y_floor * 0.995, None]),
            legend=dict(orientation="h", y=-0.12),
        )
        st.plotly_chart(fig, width="stretch", key="prop_equity_curves")
        st.markdown('<small>◆ = payout event · ✕ = breach · 🔴 zone = max loss · '
                    '🟠 zone = daily warning</small>', unsafe_allow_html=True)

    # ── TAB 2 — Payout events
    with tab2:
        all_event_rows = []
        for i, events in all_events.items():
            acc = account_configs[i]
            for ev in events:
                all_event_rows.append({
                    "Account": acc["label"], "Date": str(ev["date"]),
                    "Gross": f"{ev['gross']:,.0f}", "Net": f"{ev['net']:,.0f}",
                    "Equity After": f"{ev['equity']:,.0f}",
                    "Cumulative Net": f"{ev['cumulative']:,.0f}",
                })
        if all_event_rows:
            st.dataframe(pd.DataFrame(all_event_rows), width="stretch", hide_index=True)
            st.markdown("#### Per Account Summary")
            summary_rows = []
            for i, events in all_events.items():
                acc, sim = account_configs[i], results[i]
                _n_br = int(sim["breached"].sum())
                summary_rows.append({
                    "Account": acc["label"], "Start Date": str(acc["start"]),
                    "Payouts": len(events),
                    "Total Net": f"{sum(e['net'] for e in events):,.0f}",
                    "Final Equity": f"{sim['equity'].iloc[-1]:,.0f}",
                    "Accounts Lost": f"✗ {_n_br}" if _n_br else "✓ 0",
                })
            st.dataframe(pd.DataFrame(summary_rows), width="stretch", hide_index=True)
        else:
            st.info("No payout events triggered — equity hasn't reached the payout threshold.")

    # ── TAB 3 — Monthly income
    with tab3:
        monthly_rows = [{"date": pd.Timestamp(ev["date"]), "net": ev["net"]}
                        for events in all_events.values() for ev in events]
        if monthly_rows:
            ev_monthly = pd.DataFrame(monthly_rows)
            ev_monthly["label"] = ev_monthly["date"].dt.to_period("M")
            monthly_agg = ev_monthly.groupby("label")["net"].sum().reset_index()
            monthly_agg["label"] = monthly_agg["label"].dt.strftime("%b %Y")
            fig2 = go.Figure(go.Bar(
                x=monthly_agg["label"], y=monthly_agg["net"],
                marker_color=["#26a69a" if v > 0 else "#ef5350" for v in monthly_agg["net"]],
                text=[f"{v:,.0f}" for v in monthly_agg["net"]], textposition="outside"))
            fig2.update_layout(
                height=350, margin=dict(l=0, r=0, t=30, b=60),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#ccc"),
                xaxis=dict(gridcolor="#2d3250", tickangle=-45),
                yaxis=dict(gridcolor="#2d3250", title=f"Net {acct_ccy} Withdrawn", tickformat=",.0f"),
                title="Monthly Net Income (all accounts combined)")
            st.plotly_chart(fig2, width="stretch", key="prop_monthly_income")
            ms1, ms2, ms3, ms4 = st.columns(4)
            ms1.metric("Months with payout", len(monthly_agg))
            ms2.metric("Avg monthly income", f"{monthly_agg['net'].mean():,.0f}")
            ms3.metric("Best month", f"{monthly_agg['net'].max():,.0f}")
            ms4.metric("Months > 5k", int((monthly_agg["net"] > 5000).sum()))

            st.markdown("#### Calendar Heatmap")
            st.caption("Net payouts per calendar month")
            _payout_heatmap(all_events, acct_ccy, "prop_heatmap")
        else:
            st.info("No payout events to display.")

    # ── TAB 4 — Risk summary
    with tab4:
        for i, sim in results.items():
            acc = account_configs[i]
            st.markdown(f"#### {acc['label']}")

            acc_sz = acc["account_size"]
            sim = sim.copy()
            sim["dd_pct"] = (acc_sz - sim["equity"]) / acc_sz * 100
            max_dd = sim["dd_pct"].max()
            _daily_eq = sim.groupby("date")["equity"].agg(["first", "min"])
            _daily_dd_pct = ((_daily_eq["first"] - _daily_eq["min"]) / _daily_eq["first"] * 100)

            _dd_lbl = {"static": "Static", "trailing": "Trailing", "trailing_lock": "Trail→Lock"}
            d1, d2, d3, d4 = st.columns(4)
            d1.metric("Max Drawdown %", f"{max_dd:.1f}%")
            d2.metric("Max DD Date", str(sim.loc[sim["dd_pct"].idxmax(), "date"]))
            d3.metric(f"DD Limit ({_dd_lbl.get(acc['drawdown_type'], 'Static')})",
                      f"{acc['max_loss_pct']:.0f}%",
                      delta=f"{acc['max_loss_pct'] - max_dd:.1f}% headroom",
                      delta_color="normal")
            d4.metric("Max Daily DD", f"{_daily_dd_pct.max():.2f}%")

            # Lot sizes at account start
            lot_rows = []
            for si in active:
                sc = strat_cfgs[si]
                lots0 = _lots_for(acc_sz, sc)
                lot_rows.append({
                    "Strategy": sc["name"], "Symbol": sc["symbol"],
                    "Balance/step": f"{sc['bal_per_step']:,.0f}",
                    f"Lots @ {acc_sz:,.0f}": f"{lots0:g}",
                    "Fits?": "⚠ forced min" if _is_forced_min(acc_sz, sc) else "✓",
                })
            st.dataframe(pd.DataFrame(lot_rows), width="stretch", hide_index=True)

            # Account survival — from the main simulation (resets included)
            _diag = all_diags.get(i, {})
            _n_lost   = _diag.get("breaches", 0)
            _attempts = _diag.get("attempts", 1)
            _fee      = acc.get("account_fee", 0.0)
            st.markdown("##### Account Survival")
            if acc.get("daily_stop_only"):
                st.caption("Daily limit is a circuit breaker — trading halts for the rest of "
                           "the day, only the max drawdown limit ends the account.")
            elif acc.get("reset_on_breach"):
                st.caption("Each breach buys a fresh account (fee re-paid) — the full history "
                           "is assessed.")
            else:
                st.caption("Reset on breach is off — the simulation stops at the first breach.")
            al1, al2, al3, al4 = st.columns(4)
            al1.metric("Accounts lost", _n_lost, delta_color="inverse")
            al2.metric("Avg account life", f"{len(sim) / max(1, _attempts):.0f} trades")
            if acc.get("daily_stop_only"):
                al3.metric("Daily stops hit", _diag.get("circuit_breaker_days", 0),
                           help="Days where the daily loss limit halted trading")
                al4.metric("Trades skipped", _diag.get("cb_skipped_trades", 0),
                           help="Trades not taken because the circuit breaker was active")
            else:
                al3.metric("Accounts bought", _attempts)
                al4.metric("Fees paid", f"{_fee * _attempts:,.0f}")
            _br_rows = sim[sim["breached"]]
            if not _br_rows.empty:
                with st.expander(f"Breach events ({len(_br_rows)})", expanded=False):
                    _al_df = _br_rows[["date", "status", "equity", "lots"]].copy()
                    _al_df.index = range(1, len(_al_df) + 1)
                    st.dataframe(_al_df, width="stretch")
            _cb_rows = sim[sim["status"].str.startswith("daily stop", na=False)]
            if not _cb_rows.empty:
                with st.expander(f"Daily stops ({len(_cb_rows)})", expanded=False):
                    _cb_df = _cb_rows[["date", "status", "equity", "trade_pnl"]].copy()
                    _cb_df.index = range(1, len(_cb_df) + 1)
                    st.dataframe(_cb_df, width="stretch")
            st.markdown("---")

    # ── TAB 5 — Monte Carlo
    with tab5:
        st.markdown("*Reshuffle the trade sequence to estimate breach probability and payout distribution.*")
        mc1, mc2, mc3 = st.columns(3)
        with mc1:
            mc_sims = st.slider("Simulations", 100, 5000, 500, step=100, key="prop_mc_sims")
        with mc2:
            mc_acc_idx = st.selectbox("Account to simulate", list(results.keys()),
                                      format_func=lambda i: account_configs[i]["label"],
                                      key="prop_mc_acc")
        with mc3:
            mc_block = st.radio("Block size", ["Per trade", "Weekly", "Monthly"],
                                key="prop_mc_block", horizontal=True)

        if st.button("Run Monte Carlo", key="prop_mc_run", type="primary"):
            acc = account_configs[mc_acc_idx]
            mc_trades = stream[stream["date"] >= acc["start"]].reset_index(drop=True)
            n = len(mc_trades)
            if n < 10:
                st.warning("Need at least 10 trades for Monte Carlo.")
                return

            rng = np.random.default_rng(42)

            if mc_block == "Per trade":
                block_indices = [[i] for i in range(n)]
            else:
                dates = pd.to_datetime(mc_trades["close_time"])
                if mc_block == "Weekly":
                    keys = dates.dt.strftime("%G-W%V").values
                else:
                    keys = dates.dt.to_period("M").astype(str).values
                block_indices, prev = [], None
                for idx, key in enumerate(keys):
                    if key != prev:
                        block_indices.append([idx])
                        prev = key
                    else:
                        block_indices[-1].append(idx)
                med = np.median([len(b) for b in block_indices])
                block_indices = [b for b in block_indices if len(b) >= med * 0.5]

            n_blocks = len(block_indices)
            if n_blocks < 3:
                st.warning(f"Only {n_blocks} complete blocks — need at least 3.")
                return

            mc_breached = 0
            mc_net = np.zeros(mc_sims)
            mc_lost = np.zeros(mc_sims, dtype=int)
            mc_final = np.zeros(mc_sims)
            mc_dd = np.zeros(mc_sims)
            curves = []
            store_n = min(mc_sims, 200)
            orig_ct = mc_trades["close_time"].values
            orig_dt = mc_trades["date"].values

            progress = st.progress(0, text="Running simulations...")
            for k in range(mc_sims):
                pieces, total = [], 0
                while total < n:
                    b = block_indices[rng.integers(0, n_blocks)]
                    pieces.extend(b)
                    total += len(b)
                idx = pieces[:n]
                shuffled = mc_trades.iloc[idx].reset_index(drop=True)
                shuffled["close_time"] = orig_ct
                shuffled["date"] = orig_dt

                sim_df, sim_events, mc_diag = simulate_account(shuffled, acc, strat_cfgs, gcfg)
                if not sim_df.empty:
                    if sim_df["breached"].any():
                        mc_breached += 1
                    mc_lost[k]  = mc_diag.get("breaches", 0)
                    mc_net[k]   = sum(e["net"] for e in sim_events)
                    mc_final[k] = sim_df["equity"].iloc[-1]
                    mc_dd[k]    = (acc["account_size"] - sim_df["equity"].min()) / acc["account_size"] * 100
                    if k < store_n:
                        curves.append(sim_df["equity"].values)
                if k % max(1, mc_sims // 20) == 0:
                    progress.progress((k + 1) / mc_sims, text=f"Simulation {k+1}/{mc_sims}")
            progress.empty()

            st.markdown("#### Results")
            r1, r2, r3, r4, r5 = st.columns(5)
            r1.metric("Paths with a Breach", f"{mc_breached / mc_sims * 100:.1f}%")
            r2.metric("Avg Accounts Lost", f"{mc_lost.mean():.1f}")
            r3.metric("Median Net Payout", f"{np.median(mc_net):,.0f}")
            r4.metric("Median Final Equity", f"{np.median(mc_final):,.0f}")
            r5.metric("Median Max DD", f"{np.median(mc_dd):.1f}%")

            pcts = np.percentile(mc_net, [5, 25, 75, 95])
            r5, r6, r7, r8 = st.columns(4)
            for col, pv, lbl in zip([r5, r6, r7, r8], pcts, ["5th", "25th", "75th", "95th"]):
                col.metric(f"{lbl} %ile Payout", f"{pv:,.0f}")

            # Fan chart
            fig_mc = go.Figure()
            for curve in curves:
                fig_mc.add_trace(go.Scatter(x=list(range(len(curve))), y=curve, mode="lines",
                                            line=dict(color="#4a90d9", width=0.3),
                                            opacity=0.15, showlegend=False))
            if curves:
                max_len = max(len(c) for c in curves)
                padded = np.full((len(curves), max_len), np.nan)
                for j, c in enumerate(curves):
                    padded[j, :len(c)] = c
                for pct, col, lbl in [(5, "#ef5350", "5th"), (50, "#26a69a", "Median"),
                                      (95, "#4a90d9", "95th")]:
                    fig_mc.add_trace(go.Scatter(
                        x=list(range(max_len)), y=np.nanpercentile(padded, pct, axis=0),
                        mode="lines", line=dict(color=col, width=2, dash="dash"),
                        name=f"{lbl} percentile"))
            if mc_acc_idx in results:
                orig = results[mc_acc_idx]["equity"].values
                fig_mc.add_trace(go.Scatter(x=list(range(len(orig))), y=orig, mode="lines",
                                            line=dict(color="#ff9800", width=2),
                                            name="Original sequence"))
            acc_sz = acc["account_size"]
            fig_mc.add_hline(y=acc_sz, line_dash="dash", line_color="#555", line_width=1)
            fig_mc.add_hline(y=acc_sz * (1 - acc["max_loss_pct"] / 100), line_dash="dash",
                             line_color="#ef5350", line_width=1, annotation_text="Max Loss",
                             annotation_font_color="#ef5350")
            fig_mc.update_layout(
                title=f"Monte Carlo — {mc_sims} paths",
                height=450, margin=dict(l=0, r=0, t=30, b=0),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#ccc"),
                xaxis=dict(gridcolor="#2d3250", title="Trade #"),
                yaxis=dict(gridcolor="#2d3250", title=f"Equity ({acct_ccy})", tickformat=",.0f"),
                legend=dict(orientation="h", y=-0.12))
            st.plotly_chart(fig_mc, width="stretch", key="prop_mc_fan")

            fig_pay = go.Figure(go.Histogram(x=mc_net, nbinsx=50,
                                             marker_color="#26a69a", opacity=0.7))
            fig_pay.update_layout(
                height=300, margin=dict(l=0, r=0, t=20, b=0),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#ccc"),
                xaxis=dict(gridcolor="#2d3250", title=f"Total Net Payout ({acct_ccy})",
                           tickformat=",.0f"),
                yaxis=dict(gridcolor="#2d3250", title="Count"))
            st.plotly_chart(fig_pay, width="stretch", key="prop_mc_pay_hist")
