"""
live_rules.py
=============
Rules-based EA review for live/demo accounts — the live counterpart of the
EA Portfolio Engine's benching rules (C:\\BulkBackTest\\EA_Portfolio_engine).

Evaluates each EA (strategy, from EA_Comment) on each account against the
configured rules and returns a status board:
  ok        — no rule near firing
  warning   — within the warning fraction of a limit (default 80%)
  triggered — a rule has fired; the EA is a candidate to be benched

Rules (all optional, config in ea_rules_config.json):
  loss_streak_limit   consecutive losing days (or trades) in a row
  streak_dollar_limit $ lost over the CURRENT losing streak
  ea_dd_limit         $ peak-to-trough over the lookback window
  streak_mode         'days' | 'trades'

Risk-unit note: with accounts running the standard UBS baseline
(ManualBalance=100000, lot step = HistoricalMaxDD / 0.05), live P&L is in the
same risk units as the backtest timeline, so thresholds transfer directly.
"""

import os
import json

import numpy as np
import pandas as pd

MODULE_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(MODULE_DIR, 'ea_rules_config.json')

DEFAULT_RULES = {
    'streak_mode'        : 'days',   # 'days' | 'trades'
    'loss_streak_limit'  : 5,        # consecutive losing days/trades; None = off
    'streak_dollar_limit': 1000,     # $ lost over current streak; None = off
    'ea_dd_limit'        : 2500,     # $ window drawdown; None = off
    'lookback_days'      : 63,       # trading-day window for DD
    'warn_fraction'      : 0.8,      # warning at 80% of any limit
    'stale_days'         : 21,       # no trades for this many calendar days
                                     # -> 'inactive', never 'triggered'
    # Relative rules — compare the current streak against each EA's own
    # historical baseline from the backtest timeline (self-calibrating:
    # a 28%-win-rate EA and a 97%-win-rate EA get judged on their own norms)
    'relative_rules'     : True,
    'streak_ratio_trigger': 1.0,     # trigger at 1.0x the EA's historical worst
    'streak_ratio_warn'  : 0.8,
    # OPTIONAL personal override: a fresh short-window tick-data compile for
    # the most realistic recent form. Empty = the trailing window of the
    # baseline timeline serves as the recent-form proxy (fine out of the box).
    'proxy_timeline_dir' : '',
    'proxy_lookback_days': 63,
    # timeline used for the per-EA streak BASELINES — should stay the long
    # full-history compile even when the proxy points at a short tick-data
    # timeline (3 months of history makes a meaningless "historical worst").
    # Empty = same as proxy_timeline_dir.
    'baseline_timeline_dir': '',
    # Account labels of the reference (baseline) demo accounts running the
    # full UBS pool. They are the live candidate BENCH: their per-EA forward
    # results feed the swap-in ranking, their rule triggers are information
    # rather than decisions, and they are excluded from the live alarm banner.
    'reference_accounts' : [],
    # Live-vs-benchmark divergence: flag a live copy whose losing streak exceeds
    # its benchmark twin's by this many days/trades (size-free account check).
    'divergence_streak'  : 3,
    # ...and whose $/lot over the window differs from its twin's by more than
    # this fraction (plus a floor, so pennies per lot never trip it). Lot size
    # is the one difference that is EXPECTED between a live copy and its
    # benchmark twin, so per lot is where a real difference shows — in either
    # direction: ahead is as much a discrepancy as behind, and usually means a
    # different set-file, symbol or spread rather than luck.
    'divergence_perlot_frac': 0.25,
    'divergence_perlot_min' : 5.0,
    # Cooling-off: once an EA is benched (recorded on the Management page),
    # it is not eligible to return / be promoted for this many days — the same
    # cooldown the backtest engine's rules regime uses (default 21).
    'cooldown_days'      : 21,
}

BENCH_LOG = os.path.join(MODULE_DIR, 'ea_bench_log.json')


def load_bench_log():
    """{strategy: {'benched_on': 'YYYY-MM-DD', 'account': ..., 'reason': ...}}
    — EA's you have benched, so the cooling-off period can be enforced."""
    if os.path.isfile(BENCH_LOG):
        try:
            with open(BENCH_LOG, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_bench_log(log):
    with open(BENCH_LOG, 'w', encoding='utf-8') as f:
        json.dump(log, f, indent=2)


def cooldown_status(strategy, rules=None, log=None):
    """(in_cooldown: bool, days_left: int, eligible_on: 'YYYY-MM-DD' | '')."""
    rules = rules or load_rules_config()
    log = log if log is not None else load_bench_log()
    e = log.get(strategy)
    if not e:
        return False, 0, ''
    days = int(rules.get('cooldown_days', 21) or 0)
    benched = pd.Timestamp(e['benched_on'])
    eligible = benched + pd.Timedelta(days=days)
    left = (eligible - pd.Timestamp.now().normalize()).days
    return left > 0, max(left, 0), str(eligible.date())


DATA_ROOT = os.path.join(MODULE_DIR, 'engine_data', 'timeline')
LIB_DIR   = os.path.join(MODULE_DIR, 'engine_lib')


def _discover_timeline(name):
    """Timelines ship WITH MT5Tools (engine_data/timeline/<name>) so a clone
    works out of the box — no other repo, no backtesting, no compiling."""
    c = os.path.join(DATA_ROOT, name)
    return c if os.path.isfile(os.path.join(c, 'ea_meta.csv')) else ''


def _discover_baseline_dir():
    return _discover_timeline('main_pool_2018')


def list_proxy_timelines(rules=None):
    """{name: path} of every proxy_* timeline in the bundled data folder
    (plus the folder of the saved proxy path, if elsewhere). For the proxy
    dropdown; user-compiled proxies land here too."""
    rules = rules or load_rules_config()
    roots = [DATA_ROOT]
    base = rules.get('baseline_timeline_dir')
    if base:
        roots.append(os.path.dirname(base.rstrip('\\/')))
    out = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if d.startswith('proxy_') and os.path.isfile(os.path.join(p, 'daily_pnl.csv')):
                out.setdefault(d, p)
    cur = rules.get('proxy_timeline_dir') or ''
    if cur and os.path.isfile(os.path.join(cur, 'daily_pnl.csv')):
        out.setdefault(os.path.basename(cur.rstrip('\\/')), cur)
    return out


def load_rules_config():
    cfg = dict(DEFAULT_RULES)
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    # Out-of-the-box default: the engine repo ships main_pool_2018, so an
    # unset baseline resolves to it automatically (resolved at load, never
    # written back — a saved path always wins).
    # A saved path that does not exist on THIS machine (config copied from
    # another install, repo cloned elsewhere) falls through to discovery too.
    def _ok(p):
        return bool(p) and os.path.isfile(os.path.join(p, 'ea_meta.csv'))
    if not _ok(cfg.get('baseline_timeline_dir')):
        found = _discover_baseline_dir()
        if found:
            cfg['baseline_timeline_dir'] = found
    # Likewise the shipped real-tick recent-form proxy: used until the
    # install compiles its own (weekly refresh) or the benchmark accounts take over.
    if not _ok(cfg.get('proxy_timeline_dir')):
        found = _discover_timeline('proxy_3m_realticks')
        if found:
            cfg['proxy_timeline_dir'] = found
    return cfg


def save_rules_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)


# ── Per-EA series from a parsed live report ───────────────────────────────────

def ea_daily_pnl(df):
    """Parsed account trades df -> daily net P&L per strategy (days x EAs)."""
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.dropna(subset=['close_time']).copy()
    d['close_date'] = pd.to_datetime(d['close_time']).dt.normalize()
    out = (d.pivot_table(index='close_date', columns='strategy',
                         values='net_profit', aggfunc='sum')
           .sort_index().fillna(0.0))
    return out


def _trailing_day_streak(series):
    vals = series.to_numpy()
    vals = vals[vals != 0.0]
    n, total = 0, 0.0
    for v in vals[::-1]:
        if v < 0:
            n += 1
            total += float(v)
        else:
            break
    return n, total


def _trailing_trade_streak(trades):
    pnl = trades.sort_values('close_time')['net_profit'].to_numpy()
    n, total = 0, 0.0
    for v in pnl[::-1]:
        if v < 0:
            n += 1
            total += float(v)
        else:
            break
    return n, total


# ── Historical baselines (from the compiled backtest timeline) ────────────────

BASELINE_COLS = ['hist_max_loss_streak', 'hist_max_loss_streak_days',
                 'hist_max_streak_cost', 'hist_avg_loss_streak',
                 'largest_single_loss', 'win_rate_pct']


def load_baselines(rules=None):
    """{strategy: baseline dict} from the baseline timeline's ea_meta.csv
    (falls back to the proxy timeline if no separate dir is configured)."""
    rules = rules or load_rules_config()
    bdir = rules.get('baseline_timeline_dir') or rules.get('proxy_timeline_dir', '')
    meta_p = os.path.join(bdir, 'ea_meta.csv')
    if not os.path.isfile(meta_p):
        return {}
    meta = pd.read_csv(meta_p)
    if not set(BASELINE_COLS).issubset(meta.columns):
        return {}   # timeline compiled before baselines existed — recompile
    out = {}
    for r in meta.itertuples():
        out[r.strategy] = {c: getattr(r, c) for c in BASELINE_COLS}
    return out


def load_name_map():
    """
    ea_name_map.json — flat {EA_Comment: set-file stem}, generated from the
    deployed UBS set files (build_ea_name_map.py). The stem IS the report /
    timeline strategy name, so live trades, backtest reports and timelines
    share one identity with a single lookup — no legacy shims.
    """
    p = os.path.join(MODULE_DIR, 'ea_name_map.json')
    if not os.path.isfile(p):
        # Fresh install: build it from the set files if we can find them
        try:
            import build_ea_name_map as _b
            if _b.DEFAULT_SETS:
                import subprocess, sys as _sys
                subprocess.run([_sys.executable, _b.__file__], cwd=MODULE_DIR,
                               capture_output=True, timeout=120)
        except Exception:
            pass
    mapping = {}
    if os.path.isfile(p):
        try:
            with open(p, encoding='utf-8') as f:
                mapping = json.load(f)
        except Exception:
            mapping = {}
    # Personal legacy-comment bridges (per install): {old live comment: stem}.
    # Keys starting with '_' are notes; empty values are unfilled slots.
    ov = os.path.join(MODULE_DIR, 'ea_name_map_overrides.json')
    if os.path.isfile(ov):
        try:
            with open(ov, encoding='utf-8') as f:
                for k, v in json.load(f).items():
                    if not k.startswith('_') and v:
                        mapping[k] = v
        except Exception:
            pass
    return mapping


def match_baseline(live_name, baselines, name_map=None):
    """Exact match: the live name itself, or its stem via the name map."""
    if live_name in baselines:
        return baselines[live_name]
    if name_map is None:
        name_map = load_name_map()
    stem = name_map.get(live_name)
    return baselines.get(stem) if stem else None


# ── Evaluation ────────────────────────────────────────────────────────────────

def canonicalize(df, name_map):
    """
    Rewrite strategy names to their canonical stem via the name map (incl.
    overrides). This is what keeps an EA's history CONTINUOUS across the
    comment migration: old-comment trades (bridged via
    ea_name_map_overrides.json) and new-comment trades merge into one series
    instead of resetting streaks/windows at the switchover.
    """
    if df is None or getattr(df, 'empty', True) or not name_map:
        return df
    out = df.copy()
    out['strategy'] = out['strategy'].map(lambda s: name_map.get(s, s))
    return out


def evaluate_account(df, rules, account_label='', baselines=None, name_map=None):
    """
    One parsed account df -> list of status dicts, one per strategy.
    Levels: ok | warning | triggered. `triggers`/`warnings` carry wording.
    """
    df = canonicalize(df, name_map)
    daily = ea_daily_pnl(df)
    if daily.empty:
        return []
    warn_frac = float(rules.get('warn_fraction', 0.8))
    lookback  = int(rules.get('lookback_days', 63))
    mode      = rules.get('streak_mode', 'days')

    rows = []
    for ea in daily.columns:
        s = daily[ea]
        window = s.tail(lookback)
        if mode == 'trades':
            streak_n, streak_cost = _trailing_trade_streak(
                df[df['strategy'] == ea])
            unit = 'trades'
        else:
            streak_n, streak_cost = _trailing_day_streak(s)
            unit = 'days'
        cum = window.cumsum()
        window_dd = float((cum.cummax() - cum).max()) if len(window) else 0.0

        triggers, warnings = [], []

        lim = rules.get('loss_streak_limit')
        if lim:
            if streak_n >= lim:
                triggers.append(f'losing streak {streak_n} {unit} '
                                f'(limit {lim})')
            elif streak_n >= lim * warn_frac:
                warnings.append(f'losing streak {streak_n} {unit} '
                                f'(limit {lim})')

        lim = rules.get('streak_dollar_limit')
        if lim:
            if -streak_cost >= lim:
                triggers.append(f'current streak has cost '
                                f'${-streak_cost:,.0f} (limit ${lim:,.0f})')
            elif -streak_cost >= lim * warn_frac:
                warnings.append(f'current streak cost ${-streak_cost:,.0f} '
                                f'(limit ${lim:,.0f})')

        lim = rules.get('ea_dd_limit')
        if lim:
            if window_dd >= lim:
                triggers.append(f'window drawdown ${window_dd:,.0f} '
                                f'(limit ${lim:,.0f})')
            elif window_dd >= lim * warn_frac:
                warnings.append(f'window drawdown ${window_dd:,.0f} '
                                f'(limit ${lim:,.0f})')

        # Relative rules — current streak vs this EA's own historical worst
        base = match_baseline(ea, baselines, name_map=name_map) if (baselines and
                rules.get('relative_rules', True)) else None
        base_streak = base_cost = None
        if base:
            r_trig = float(rules.get('streak_ratio_trigger', 1.0))
            r_warn = float(rules.get('streak_ratio_warn', 0.8))
            base_streak = int(base['hist_max_loss_streak'] if mode == 'trades'
                              else base['hist_max_loss_streak_days'])
            if base_streak > 0 and streak_n > 0:
                ratio = streak_n / base_streak
                msg = (f'streak {streak_n} {unit} = {ratio:.1f}x its '
                       f'historical worst ({base_streak})')
                if ratio >= r_trig:
                    triggers.append(msg)
                elif ratio >= r_warn:
                    warnings.append(msg)
            base_cost = float(base['hist_max_streak_cost'])
            if base_cost > 0 and -streak_cost > 0:
                cratio = -streak_cost / base_cost
                note = ''
                if base['largest_single_loss'] >= 0.8 * base_cost:
                    note = (' [baseline dominated by one large loss — possible '
                            'news-gap/backtest artifact, judge accordingly]')
                msg = (f'streak cost ${-streak_cost:,.0f} = {cratio:.1f}x its '
                       f'historical worst (${base_cost:,.0f}){note}')
                if cratio >= r_trig:
                    triggers.append(msg)
                elif cratio >= r_warn:
                    warnings.append(msg)

        level = 'triggered' if triggers else ('warning' if warnings else 'ok')

        # Staleness: an EA with no recent trades is 'inactive', not an alarm —
        # its streak/DD state is old news, and it may simply be off the chart.
        stale = int(rules.get('stale_days', 0) or 0)
        last_dt = s[s != 0].index.max() if (s != 0).any() else None
        if stale and last_dt is not None:
            age = (pd.Timestamp.now().normalize() - last_dt).days
            if age > stale:
                level = 'inactive'
                triggers, warnings = [], []

        rows.append({
            'account'     : account_label,
            'strategy'    : ea,
            'level'       : level,
            'streak'      : streak_n,
            'streak_unit' : unit,
            'streak_cost' : round(-streak_cost, 2),
            'window_dd'   : round(window_dd, 2),
            'window_pnl'  : round(float(window.sum()), 2),
            'last_trade'  : str(s[s != 0].index.max().date())
                            if (s != 0).any() else '',
            'baseline_streak': base_streak,
            'baseline_cost'  : base_cost,
            'triggers'    : triggers,
            'warnings'    : warnings,
        })
    return rows


def evaluate_all(cached_accounts, rules=None):
    """cached_accounts: list of cache dicts from view_live_mt5_eas.get_all_cached()."""
    rules = rules or load_rules_config()
    baselines = load_baselines(rules) if rules.get('relative_rules', True) else {}
    name_map = load_name_map()
    rows = []
    for data in cached_accounts:
        df = data.get('df')
        if df is None or getattr(df, 'empty', True):
            continue
        rows.extend(evaluate_account(df, rules, baselines=baselines,
                                     name_map=name_map,
                                     account_label=data.get('label',
                                                            data.get('account_folder', ''))))
    return rows


def forward_stats_by_strategy(fwd, name_map=None):
    """
    Re-key reference_forward_stats (keyed by live EA_Comment) by stem so
    candidate rows keyed by timeline strategy can look them up.
    """
    if name_map is None:
        name_map = load_name_map()
    out = {}
    for comment, stem in name_map.items():
        if comment in fwd:
            out[stem] = fwd[comment]
    for k, v in fwd.items():
        out.setdefault(k, v)
    return out


def reference_forward_stats(cached_accounts, rules=None, lookback=None):
    """
    Per-strategy live forward stats from the reference (baseline) accounts —
    the demo benchmark accounts, which replace the backtest proxy as they
    accumulate history.
    Returns {strategy: {live_days, live_pnl, live_sharpe, account}}.
    """
    rules = rules or load_rules_config()
    refs = set(rules.get('reference_accounts') or [])
    if not refs:
        return {}
    lookback = lookback or int(rules.get('proxy_lookback_days', 63))
    name_map = load_name_map()
    out = {}
    for d in cached_accounts:
        lbl = d.get('label', d.get('account_folder', ''))
        if lbl not in refs:
            continue
        df = d.get('df')
        if df is None or getattr(df, 'empty', True):
            continue
        daily = ea_daily_pnl(canonicalize(df, name_map))
        for ea in daily.columns:
            s   = daily[ea]
            win = s.tail(lookback)
            vol = win.std(ddof=0)
            out[ea] = {
                'live_days'  : int((s != 0).sum()),
                'live_pnl'   : round(float(win.sum()), 2),
                'live_sharpe': round(float(win.mean() / vol * np.sqrt(252)), 2)
                               if vol > 0 else 0.0,
                'account'    : lbl,
            }
    return out


# ── Benchmark-as-signal evaluation ────────────────────────────────────────────────
#
# The benchmark (reference) accounts run the whole pool at the STANDARD
# baseline ($100k, lot step = HistMaxDD/5%) — the exact environment the rule
# thresholds were designed for. So the rules are evaluated THERE (one canonical
# status per EA), and live accounts inherit the consequences: an EA flagged on
# the benchmark accounts is a candidate to be benched on every live account
# running it. Live
# accounts are then checked size-free against their benchmark twin — a live copy
# doing markedly worse than the same EA on the benchmark is an ACCOUNT
# problem
# (fills, VPS, set-file load), not a strategy-form problem.

def bench_signals(cached_accounts, rules=None):
    """{strategy: status row} from the reference accounts (best/worst level
    if an EA sits on several benchmark accounts: the most severe wins)."""
    rules = rules or load_rules_config()
    refs = set(rules.get('reference_accounts') or [])
    if not refs:
        return {}
    baselines = load_baselines(rules) if rules.get('relative_rules', True) else {}
    name_map = load_name_map()
    order = {'triggered': 3, 'warning': 2, 'ok': 1, 'inactive': 0}
    out = {}
    for d in cached_accounts:
        lbl = d.get('label', d.get('account_folder', ''))
        if lbl not in refs:
            continue
        for r in evaluate_account(d.get('df'), rules, account_label=lbl,
                                  baselines=baselines, name_map=name_map):
            cur = out.get(r['strategy'])
            if cur is None or order[r['level']] > order[cur['level']]:
                out[r['strategy']] = r
    return out


def _live_series(df, name_map):
    df = canonicalize(df, name_map)
    return ea_daily_pnl(df), df


def _window_perlot(cdf, ea, dates):
    """$ per lot for one EA over the given close dates — benchmark and live run
    the same EA at different sizes, so raw dollars compare nothing."""
    if cdf is None or getattr(cdf, 'empty', True) or 'volume' not in cdf:
        return None
    t = cdf[cdf['strategy'] == ea]
    if t.empty:
        return None
    d = pd.to_datetime(t['close_time'], errors='coerce').dt.normalize()
    t = t[d.isin(set(dates))]
    vol = float(pd.to_numeric(t['volume'], errors='coerce').fillna(0).sum())
    if not vol:
        return None
    return round(float(t['net_profit'].sum()) / vol, 2)


def live_vs_bench(cached_accounts, signals, rules=None):
    """
    For every EA on a LIVE (non-reference) account:
      - inherit the benchmark signal for that EA (if the EA is on the benchmark accounts)
      - size-free divergence check vs the benchmark twin over the lookback:
        live losing streak >= benchmark streak + divergence_streak (default 3),
        or live window P&L negative while benchmark window P&L positive.
      - EAs not on the benchmark accounts: direct evaluation with dollar thresholds scaled
        by the account balance / 100k (the weaker, labelled fallback).
    Returns list of rows (one per live EA-account).
    """
    rules = rules or load_rules_config()
    refs = set(rules.get('reference_accounts') or [])
    lookback = int(rules.get('lookback_days', 63))
    mode = rules.get('streak_mode', 'days')
    div_n = int(rules.get('divergence_streak', 3))
    pl_frac = float(rules.get('divergence_perlot_frac', 0.25) or 0)
    pl_min  = float(rules.get('divergence_perlot_min', 5.0) or 0)
    name_map = load_name_map()
    baselines = load_baselines(rules) if rules.get('relative_rules', True) else {}

    # benchmark per-EA window P&L / streak for the twin comparison
    bench_daily = {}
    for d in cached_accounts:
        lbl = d.get('label', d.get('account_folder', ''))
        if lbl in refs and d.get('df') is not None and not d['df'].empty:
            daily, cdf = _live_series(d['df'], name_map)
            for ea in daily.columns:
                bench_daily.setdefault(ea, (daily[ea], cdf))

    rows = []
    for d in cached_accounts:
        lbl = d.get('label', d.get('account_folder', ''))
        if lbl in refs or d.get('df') is None or d['df'].empty:
            continue
        bal = float(d.get('balance') or 100_000)
        scale = bal / 100_000.0
        daily, cdf = _live_series(d['df'], name_map)
        # fallback direct evaluation, thresholds scaled to the account
        scaled = dict(rules)
        for k in ('streak_dollar_limit', 'ea_dd_limit'):
            if rules.get(k):
                scaled[k] = float(rules[k]) * scale
        direct = {r['strategy']: r for r in evaluate_account(
            d['df'], scaled, account_label=lbl, baselines=baselines,
            name_map=name_map)}
        for ea in daily.columns:
            s = daily[ea]
            live_win = s.tail(lookback)
            if mode == 'trades':
                live_streak, _ = _trailing_trade_streak(cdf[cdf['strategy'] == ea])
            else:
                live_streak, _ = _trailing_day_streak(s)
            sig = signals.get(ea)
            row = {'account': lbl, 'strategy': ea, 'balance': bal,
                   'on_bench': sig is not None,
                   'bench_level': sig['level'] if sig else None,
                   'bench_triggers': (sig['triggers'] + sig['warnings']) if sig else [],
                   'live_streak': live_streak, 'streak_unit': mode,
                   'live_window_pnl': round(float(live_win.sum()), 2),
                   'last_trade': str(s[s != 0].index.max().date()) if (s != 0).any() else '',
                   'diverges': False, 'divergence': [],
                   'fallback_level': None, 'fallback_triggers': []}
            if ea in bench_daily:
                b_s, b_df = bench_daily[ea]
                b_win = b_s.tail(lookback)
                if mode == 'trades':
                    b_streak, _ = _trailing_trade_streak(b_df[b_df['strategy'] == ea])
                else:
                    b_streak, _ = _trailing_day_streak(b_s)
                row['bench_streak'] = b_streak
                row['bench_window_pnl'] = round(float(b_win.sum()), 2)
                row['live_perlot']  = _window_perlot(cdf, ea, live_win.index)
                row['bench_perlot'] = _window_perlot(b_df, ea, b_win.index)
                if row['live_perlot'] is not None and row['bench_perlot']:
                    # live per lot / benchmark per lot. Winning twin: 1.0 = same,
                    # <1 = the live copy keeps less, <0 = it loses where the
                    # twin wins. Losing twin: >1 = the live copy loses more.
                    row['perlot_ratio'] = round(
                        row['live_perlot'] / row['bench_perlot'], 3)
                lp, bp = row['live_perlot'], row['bench_perlot']
                if pl_frac and lp is not None and bp is not None:
                    tol = max(pl_frac * abs(bp), pl_min)
                    gap = lp - bp
                    if abs(gap) > tol:
                        row['perlot_direction'] = 'behind' if gap < 0 else 'ahead'
                        row['divergence'].append(
                            f'{"behind" if gap < 0 else "ahead of"} its benchmark '
                            f'twin per lot: ${lp:,.0f}/lot live vs ${bp:,.0f}'
                            f'/lot on the benchmark accounts over the same window '
                            f'({row["perlot_ratio"]:.0%}) — size is the one '
                            'difference that is meant to be there, so a gap '
                            'per lot is fills, set-file or symbol, not size')
                if live_streak >= b_streak + div_n and live_streak >= div_n:
                    row['divergence'].append(
                        f'live losing streak {live_streak} {mode} vs benchmark '
                        f'{b_streak} — the live copy is doing worse than the '
                        'same EA on the benchmark accounts')
                if live_win.sum() < 0 < b_win.sum():
                    row['divergence'].append(
                        f'live window P&L ${live_win.sum():,.0f} while the benchmark '
                        f'twin made ${b_win.sum():,.0f} — check fills / VPS / '
                        'set-file on this account')
                row['diverges'] = bool(row['divergence'])
            else:
                dr = direct.get(ea)
                if dr:
                    row['fallback_level'] = dr['level']
                    row['fallback_triggers'] = dr['triggers'] + dr['warnings']
            rows.append(row)
    return rows


def windowed_live_vs_bench(cached_accounts, signals, rules=None, days=7):
    """
    (rows, as_of) — live_vs_bench over the last `days` CALENDAR days instead of
    the rules' trailing trading-day window.

    The rules window counts trading days per EA, so a daily-timeframe EA's
    window reaches back months and two EA's need not span the same period at
    all. Cutting the trade data by date instead keeps the live and benchmark sides
    on exactly the same dates. The cut is anchored to the newest trade in the
    cache rather than the wall clock: reports pulled before the session closed
    would otherwise make "today" look empty.
    """
    rules = rules or load_rules_config()
    as_of = None
    for d in cached_accounts:
        df = d.get('df')
        if df is None or getattr(df, 'empty', True) or 'close_time' not in df:
            continue
        m = pd.to_datetime(df['close_time'], errors='coerce').max()
        if pd.notna(m) and (as_of is None or m > as_of):
            as_of = m
    if as_of is None:
        return [], None

    cutoff = as_of.normalize() - pd.Timedelta(days=int(days) - 1)
    win = []
    for d in cached_accounts:
        df = d.get('df')
        if df is None or getattr(df, 'empty', True) or 'close_time' not in df:
            win.append(d)
            continue
        ct = pd.to_datetime(df['close_time'], errors='coerce')
        win.append(dict(d, df=df[ct >= cutoff]))
    # the calendar cut IS the window now, so nothing further is trimmed
    return (live_vs_bench(win, signals, dict(rules, lookback_days=100_000)),
            as_of)


def summarize_triggers(rows, reference_accounts=None):
    """Short banner text for the Live page, or None if all clear.
    Reference-account rows are informational and excluded from the alarm."""
    refs = set(reference_accounts or [])
    rows = [r for r in rows if r['account'] not in refs]
    trig = [r for r in rows if r['level'] == 'triggered']
    warn = [r for r in rows if r['level'] == 'warning']
    if not trig and not warn:
        return None
    parts = []
    if trig:
        names = ', '.join(f"{r['strategy']} ({r['triggers'][0]})" for r in trig[:3])
        more  = f" +{len(trig) - 3} more" if len(trig) > 3 else ''
        parts.append(f"🛑 {len(trig)} rule trigger(s): {names}{more}")
    if warn:
        parts.append(f"⚠ {len(warn)} approaching a limit")
    return ' — '.join(parts) + '. Review on the Live UBS EA Management page.'


# ── Backtest proxy (until 3 months of demo data exists) ───────────────────────

def _resolve_proxy_dir(rules):
    for cand in (rules.get('proxy_timeline_dir'),
                 _discover_timeline('proxy_3m_realticks'),
                 rules.get('baseline_timeline_dir')):
        if cand and os.path.isfile(os.path.join(cand, 'daily_pnl.csv')) \
                and os.path.isfile(os.path.join(cand, 'ea_meta.csv')):
            return cand
    return ''


def proxy_daily(rules=None):
    """(daily P&L DataFrame [date x ea_id], {strategy: ea_id}) from the
    recent-form proxy — for correlating swap-in candidates against a live
    account's current book. Returns (None, {}) if unavailable."""
    rules = rules or load_rules_config()
    tdir = _resolve_proxy_dir(rules)
    if not tdir:
        return None, {}
    daily = pd.read_csv(os.path.join(tdir, 'daily_pnl.csv'),
                        index_col='date', parse_dates=['date'])
    meta = pd.read_csv(os.path.join(tdir, 'ea_meta.csv'))
    daily = daily.tail(int(rules.get('proxy_lookback_days', 63)))
    return daily, dict(zip(meta.strategy, meta.ea_id))


def load_proxy(rules=None):
    """
    Trailing-window per-EA stats from the compiled backtest timeline —
    the stand-in for reference-account data during the first months.
    Returns a DataFrame ranked by sharpe, or None if unavailable.
    """
    rules = rules or load_rules_config()
    # Resolution order: the chosen proxy timeline → the shipped
    # proxy_3m_realticks → the baseline timeline's trailing window. A bad
    # saved path must fall through, not kill the ranking.
    tdir = ''
    for cand in (rules.get('proxy_timeline_dir'),
                 _discover_timeline('proxy_3m_realticks'),
                 rules.get('baseline_timeline_dir')):
        if cand and os.path.isfile(os.path.join(cand, 'daily_pnl.csv')) \
                and os.path.isfile(os.path.join(cand, 'ea_meta.csv')):
            tdir = cand
            break
    if not tdir:
        return None
    daily_p = os.path.join(tdir, 'daily_pnl.csv')
    meta_p  = os.path.join(tdir, 'ea_meta.csv')
    daily = pd.read_csv(daily_p, index_col='date', parse_dates=['date'])
    meta  = pd.read_csv(meta_p).set_index('ea_id')
    win   = daily.tail(int(rules.get('proxy_lookback_days', 63)))

    mean, vol = win.mean(), win.std(ddof=0)
    sharpe = pd.Series(np.where(vol > 0, mean / vol * np.sqrt(252), 0.0),
                       index=win.columns)
    cum = win.cumsum()
    dd  = cum.cummax().sub(cum).max()

    out = pd.DataFrame({
        'strategy' : [meta.at[c, 'strategy'] if c in meta.index else c
                      for c in win.columns],
        'family'   : [meta.at[c, 'family'] if c in meta.index else ''
                      for c in win.columns],
        'symbol'   : [meta.at[c, 'symbol'] if c in meta.index else ''
                      for c in win.columns],
        'window_pnl': win.sum().round(2),
        'sharpe'    : sharpe.round(2),
        'window_dd' : dd.round(2),
    }, index=win.columns)
    # Historical streak baselines, when the timeline has them
    for col in BASELINE_COLS:
        if col in meta.columns:
            out[col] = [meta.at[c, col] if c in meta.index else None
                        for c in win.columns]
    out.index.name = 'ea_id'
    return out.sort_values('sharpe', ascending=False)
