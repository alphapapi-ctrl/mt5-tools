"""
pages/trade_compare.py
======================
Side-by-side comparison of two trade history files.
Matches trades by symbol + type + open time within a tolerance window.
Highlights slippage, profit variance, and timing differences.
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import sys, os, json, pickle
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mt5_parser import detect_and_parse, calc_stats


# ── FTP report cache (same source as 📡 Live MT5 EA's) ────────────────────────

FTP_ACCOUNTS_FILE = Path("ftp_accounts.json")
FTP_CACHE_DIR     = Path("cache")


def _ftp_account_configs() -> list:
    if FTP_ACCOUNTS_FILE.exists():
        try:
            return json.loads(FTP_ACCOUNTS_FILE.read_text())
        except Exception:
            return []
    return []


def _ftp_cache(account_folder: str):
    """Cached payload for one account: {df, stats, label, balance, ...}."""
    p = FTP_CACHE_DIR / f"ftp_{account_folder}.pkl"
    if not p.exists():
        return None
    try:
        return pickle.loads(p.read_bytes())
    except Exception:
        return None


def _ftp_choices() -> list:
    """[(display, account_folder)] for configured accounts that have a cache."""
    return [(f"{ac.get('label', ac['account'])} ({ac['account']})", ac['account'])
            for ac in _ftp_account_configs() if _ftp_cache(ac['account'])]


def _cache_signature() -> tuple:
    """Cheap fingerprint of the FTP cache + rules file, so the flagged list is
    recomputed only when a refresh or a rules edit actually changed something."""
    sig = []
    if FTP_CACHE_DIR.exists():
        for p in sorted(FTP_CACHE_DIR.glob("ftp_*.pkl")):
            try:
                stt = p.stat()
                sig.append((p.name, int(stt.st_mtime), stt.st_size))
            except OSError:
                pass
    for extra in ('ea_rules_config.json', 'ea_name_map_overrides.json'):
        try:
            sig.append((extra, int(os.path.getmtime(extra)), 0))
        except OSError:
            pass
    return tuple(sig)


def _all_cached() -> list:
    out = []
    if not FTP_CACHE_DIR.exists():
        return out
    for p in sorted(FTP_CACHE_DIR.glob("ftp_*.pkl")):
        try:
            out.append(pickle.loads(p.read_bytes()))
        except Exception:
            pass
    return out


# ── Loading a side ────────────────────────────────────────────────────────────

_FILTER_KEYS = {
    'A': ['tc_a_from', 'tc_a_to', 'tc_a_sym', 'tc_a_strat', 'tc_a_type'],
    'B': ['tc_b_from', 'tc_b_to', 'tc_b_sym', 'tc_b_strat', 'tc_b_type'],
}


def _canonical(df):
    """
    (df, n_rewritten) — EA names rewritten to their canonical stem with the
    same map the benching pages use (🔗 Name mapping).

    Applied to EVERY side, however it was loaded: a live account still writing
    the old trade comments would otherwise never line up with the benchmark's
    names, so the strategy filter would list the same EA twice and picking
    it on one side would select nothing on the other.
    """
    try:
        from live_rules import load_name_map, canonicalize
    except Exception:
        return df, 0
    if df is None or getattr(df, 'empty', True) or 'strategy' not in df:
        return df, 0
    nm = load_name_map()
    if not nm:
        return df, 0
    out = canonicalize(df, nm)
    return out, int((out['strategy'] != df['strategy']).sum())


def _set_side(side: str, df, fmt: str, name: str = None, span: str = None):
    """Put a dataframe on side A or B, dropping that side's now-stale filter
    widgets (dates from the previous file would fall outside the new range).
    `name` is the short label charts use — the account it came from, not the
    file format."""
    k = side.lower()
    df, mapped = _canonical(df)
    st.session_state[f'tc_df_{k}']   = df
    st.session_state[f'tc_fmt_{k}']  = fmt
    st.session_state[f'tc_name_{k}'] = name or fmt
    st.session_state[f'tc_map_{k}']  = mapped
    st.session_state[f'tc_span_{k}'] = span or (
        f"{df['open_time'].min():%d %b} – {df['open_time'].max():%d %b}"
        if df is not None and not df.empty and 'open_time' in df else '')
    for key in _FILTER_KEYS[side]:
        st.session_state.pop(key, None)
    st.session_state.pop('tc_matched', None)


# ── Flagged pairs from the benchmark check ─────────────────────────────

@st.cache_data(show_spinner=False)
def _flagged_pairs(sig, days=None):
    """
    (pairs, note) — account/EA rows the benching tool has flagged on the
    🎛 Live UBS EA Management page, each resolved to the two reports worth
    comparing: the live copy and its twin on a benchmark account.

    Nothing is re-judged here; this only saves hunting for the right two
    reports. `sig` is the cache fingerprint — it must stay a plain name, since
    Streamlit drops underscore-prefixed arguments from the cache key and the
    list would then survive an FTP refresh unchanged.
    """
    try:
        from live_rules import (load_rules_config, bench_signals, live_vs_bench,
                                windowed_live_vs_bench, load_name_map,
                                canonicalize)
    except Exception as e:
        return [], f"Benching rules unavailable ({e}) — load reports manually."
    cached = _all_cached()
    if not cached:
        return [], ("No FTP account cache yet — refresh accounts on the "
                    "📡 Live MT5 EA's page.")
    rules = load_rules_config()
    if not rules.get('reference_accounts'):
        return [], ("No benchmark accounts configured — set them on 🎛 Live UBS "
                    "EA Management → 🧪 Benchmark accounts.")

    name_map = load_name_map()
    folder_of, strats_on = {}, {}
    for d in cached:
        lbl = d.get('label', d.get('account_folder', ''))
        folder_of[lbl] = d.get('account_folder')
        df = d.get('df')
        if df is not None and not df.empty:
            cdf = canonicalize(df, name_map)
            strats_on[lbl] = set(cdf['strategy'].dropna().unique())

    refs = [a for a in (rules.get('reference_accounts') or []) if a in strats_on]
    signals = bench_signals(cached, rules)
    if days:
        rows, as_of = windowed_live_vs_bench(cached, signals, rules, days)
    else:
        rows, as_of = live_vs_bench(cached, signals, rules), None

    pairs = []
    for r in rows:
        level = r.get('bench_level') or r.get('fallback_level')
        flagged = r['diverges'] or level in ('triggered', 'warning')
        # Preferred partner is the benchmark twin. Failing that — the EAs
        # the rules flag directly on a live account have no twin by definition
        # — another live account running the same EA, which still separates
        # the account from the strategy.
        twin = next((a for a in refs if r['strategy'] in strats_on.get(a, ())), None)
        kind = 'benchmark'
        if twin is None:
            kind = 'live'
            twin = next((a for a, ss in strats_on.items()
                         if a not in refs and a != r['account']
                         and r['strategy'] in ss), None)
        if twin is None:
            continue                      # nothing anywhere to compare against
        # A benchmark twin is always worth offering — comparing an EA that
        # is behaving is how you learn what behaving looks like. Live-vs-live
        # is the fallback, so it is offered only where a rule fired.
        if not flagged and kind != 'benchmark':
            continue
        icon, sev = (('🔼', 0) if r.get('perlot_direction') == 'ahead' else
                     ('🔀', 0) if r['diverges'] else
                     ('🛑', 1) if level == 'triggered' else
                     ('⚠️', 2) if level == 'warning' else ('✅', 3))
        pairs.append({
            'label': f"{icon} {r['strategy']} — live on {r['account']} "
                     f"vs {kind} {twin}",
            'icon': icon,
            'strategy': r['strategy'],
            'live_account': r['account'],
            'live_folder': folder_of.get(r['account']),
            'bench_account': twin,
            'bench_folder': folder_of.get(twin),
            'partner_kind': kind,
            'why': (r['divergence'] + (r.get('bench_triggers') or [])
                    + (r.get('fallback_triggers') or [])),
            'live_streak': r.get('live_streak'),
            'bench_streak': r.get('bench_streak'),
            'streak_unit': r.get('streak_unit', 'days'),
            'live_window_pnl': r.get('live_window_pnl'),
            'bench_window_pnl': r.get('bench_window_pnl'),
            'live_perlot': r.get('live_perlot'),
            'bench_perlot': r.get('bench_perlot'),
            'perlot_ratio': r.get('perlot_ratio'),
            'flagged': flagged,
            'sev': sev,
            'lookback': int(rules.get('lookback_days', 63)),
            'window_days': days,
            'as_of': as_of,
        })
    pairs.sort(key=lambda p: (p['sev'], p['label']))
    if not pairs:
        twins = sum(1 for r in rows
                    if any(r['strategy'] in strats_on.get(a, ()) for a in refs))
        return [], (f"Nothing to pair — {len(rows)} live EA/account row(s), "
                    f"{twins} of them running on a second account. An EA "
                    "whose trade comments are not bridged to the same "
                    "canonical name on both accounts has nothing to be paired "
                    "with (🎛 Live UBS EA Management → 🔗 Name mapping). Load "
                    "the reports manually below.")
    return pairs, None


def _ea_trades(account_folder, strategy, days=None, since=None):
    """That account's cached trades for one EA (canonical names). `since` cuts
    both sides at the same date; `days` falls back to the last `days` of this
    account's own history."""
    from live_rules import load_name_map, canonicalize
    payload = _ftp_cache(account_folder) if account_folder else None
    if not payload or payload.get('df') is None or payload['df'].empty:
        return None
    df = canonicalize(payload['df'].copy(), load_name_map())
    df = df[df['strategy'] == strategy].copy()
    if df.empty:
        return df
    df['open_time']  = pd.to_datetime(df['open_time'], errors='coerce')
    df['close_time'] = pd.to_datetime(df['close_time'], errors='coerce')
    df = df.dropna(subset=['open_time'])
    if since is not None:
        df = df[df['open_time'] >= pd.Timestamp(since)]
    elif days:
        cutoff = df['open_time'].max() - pd.Timedelta(days=int(days))
        df = df[df['open_time'] >= cutoff]
    return df.sort_values('open_time')


WINDOW_DAYS = {'Today': 1, 'This week': 7, 'This month': 30}


def _render_flagged_loader():
    """Dropdown of flagged account/EA pairs that loads the two reports into
    A and B. Purely a shortcut — the comparison below is the normal one."""
    rules_win = 'Quarter'
    win_pick = st.radio(
        'Window', list(WINDOW_DAYS) + [rules_win], index=3, horizontal=True,
        key='tc_flag_window',
        help='Which period the flags — and the trades loaded into A and B — '
             'cover. Quarter is the benching rules\' own trailing window (63 '
             'trading days per EA); today / week / month cut by calendar '
             'date, the same dates on both accounts.')
    # options change with the window, so a stale pick must not survive it
    if st.session_state.get('tc_flag_window_prev') != win_pick:
        st.session_state['tc_flag_window_prev'] = win_pick
        st.session_state.pop('tc_flag_pick', None)

    days = WINDOW_DAYS.get(win_pick)
    pairs, note = _flagged_pairs(_cache_signature(), days)
    if note:
        st.caption(note)
        return
    if not pairs:
        st.caption(f'Nothing to pair over {win_pick.lower()}.')
        return

    st.dataframe(pd.DataFrame([{
        ' ': p['icon'],
        'EA': p['strategy'],
        'Account': p['live_account'],
        'Compared with': p['bench_account'],
        'P&L ($)': p['live_window_pnl'],
        'Compared P&L ($)': p['bench_window_pnl'],
    } for p in pairs]), use_container_width=True, hide_index=True)

    labels = [p['label'] for p in pairs]
    if st.session_state.get('tc_flag_pick') not in labels:
        st.session_state.pop('tc_flag_pick', None)
    c1, c2 = st.columns([4, 1])
    pick = c1.selectbox(
        'EA — the two accounts to compare', labels, key='tc_flag_pick',
        help='Every EA running on more than one cached account, paired with '
             'the account worth comparing it against. Picking one loads that '
             'account into A and the live copy into B.')
    p = pairs[labels.index(pick)]
    with c2:
        st.markdown('<br>', unsafe_allow_html=True)
        load = st.button('Load A/B', type='primary', use_container_width=True,
                         key='tc_flag_load')

    # everything reads "B vs A", whichever kind of account A turned out to be
    bits = []
    if p['live_streak'] is not None and p['bench_streak'] is not None:
        bits.append(f"losing streak {p['live_streak']} {p['streak_unit']} vs "
                    f"{p['bench_streak']}")
    if p['live_window_pnl'] is not None and p['bench_window_pnl'] is not None:
        bits.append(f"P&L \\${p['live_window_pnl']:,.0f} vs "
                    f"\\${p['bench_window_pnl']:,.0f}")
    if p.get('perlot_ratio') is not None:
        bits.append(f"\\${p['live_perlot']:,.0f}/lot vs "
                    f"\\${p['bench_perlot']:,.0f}/lot "
                    f"({p['perlot_ratio']:.0%})")
    if bits:
        span = (f"last {p['lookback']} trading days" if not p['window_days'] else
                f"{p['window_days']} calendar day(s) to "
                f"{pd.Timestamp(p['as_of']).date()}")
        st.caption(f"{p['live_account']} vs {p['bench_account']} — "
                   + ' · '.join(bits) + f' ({span})')
    for w in p['why'][:4]:
        st.caption('· ' + w.replace('$', chr(92) + '$'))

    since = (pd.Timestamp(p['as_of']).normalize()
             - pd.Timedelta(days=p['window_days'] - 1)) if p['window_days'] else None
    fallback = p['lookback'] * 2
    b = _ea_trades(p['bench_folder'], p['strategy'], days=fallback, since=since)
    l = _ea_trades(p['live_folder'],  p['strategy'], days=fallback, since=since)
    nb = 0 if b is None else len(b)
    nl = 0 if l is None else len(l)
    st.caption(f'{nb} trade(s) on {p["bench_account"]} · {nl} on '
               f'{p["live_account"]} in this window.')

    # Reload when the window moves under an already-loaded pair: leaving the
    # previous window's trades on screen reads as "the window changed nothing".
    token = (p['strategy'], p['bench_folder'], p['live_folder'], p['window_days'])
    prev  = st.session_state.get('tc_flag_loaded')
    # Anything loaded from this dropdown must match the controls above it. A
    # pair loaded before tc_flag_loaded existed has no token at all, which is
    # why a stale window could sit on screen describing trades it did not hold.
    from_here = str(st.session_state.get('tc_fmt_a') or '').startswith(
        ('Bench · ', 'Live · '))
    stale = (prev != token) if (prev is not None or from_here) else False

    if load or stale:
        if not nb or not nl:
            st.session_state['tc_flag_loaded'] = token
            st.warning(f'No trades for {p["strategy"]} on both accounts in '
                       f'this window — A and B left as they were.')
        else:
            span = (f"{win_pick.lower()}, "
                    f"{b['open_time'].min():%d %b} – {b['open_time'].max():%d %b}")
            side_a = 'Bench' if p.get('partner_kind', 'benchmark') == 'benchmark' else 'Live'
            _set_side('A', b, f"{side_a} · {p['bench_account']} · {p['strategy']}",
                      name=p['bench_account'], span=span)
            _set_side('B', l, f"Live · {p['live_account']} · {p['strategy']}",
                      name=p['live_account'],
                      span=(f"{win_pick.lower()}, "
                            f"{l['open_time'].min():%d %b} – "
                            f"{l['open_time'].max():%d %b}"))
            st.session_state['tc_flag_loaded'] = token
            # the pair was picked to be compared — don't make the user press
            # Match Trades to see what they just asked for
            st.session_state['tc_auto_match'] = True
            st.rerun()


def _render_ftp_source(side: str, key: str):
    """FTP-account picker for one side, alongside the file uploader."""
    choices = _ftp_choices()
    if not choices:
        st.caption('No cached FTP accounts — refresh on the 📡 Live MT5 '
                   'EA\'s page.')
        return
    st.caption('…or load a cached FTP report:')
    labels = [d for d, _ in choices]
    sel = st.selectbox(f'FTP account — {side}', ['(none)'] + labels,
                       key=f'{key}_ftp_sel', label_visibility='collapsed')
    if sel != '(none)' and st.button(f'Load {side} from FTP', key=f'{key}_ftp_btn',
                                     use_container_width=True):
        folder = next(f for d, f in choices if d == sel)
        payload = _ftp_cache(folder)
        if payload and payload.get('df') is not None and not payload['df'].empty:
            df = payload['df'].copy()
            df['open_time']  = pd.to_datetime(df['open_time'], errors='coerce')
            df['close_time'] = pd.to_datetime(df['close_time'], errors='coerce')
            _set_side(side, df.dropna(subset=['open_time']), f'FTP · {sel}',
                      name=sel.rsplit(' (', 1)[0])
            st.rerun()
        else:
            st.error('Cache is empty for that account — refresh it first.')


# ── Match trades ──────────────────────────────────────────────────────────────

def match_trades(df_a, df_b, tolerance_hours):
    """
    Match trades between two DataFrames.
    Match criteria: same symbol_base + same type + open_time within tolerance.
    Returns DataFrame of matched pairs with diff columns.
    """
    tol = pd.Timedelta(hours=tolerance_hours)
    matched = []
    used_b  = set()

    for i, a in df_a.iterrows():
        best_match = None
        best_delta = tol + pd.Timedelta(seconds=1)

        for j, b in df_b.iterrows():
            if j in used_b:
                continue
            if a['symbol_base'] != b['symbol_base']:
                continue
            if a['type'] != b['type']:
                continue
            delta = abs(a['open_time'] - b['open_time'])
            if delta <= tol and delta < best_delta:
                best_delta  = delta
                best_match  = (j, b)

        if best_match:
            j, b = best_match
            used_b.add(j)

            open_slip  = round(float(b['open_price'])  - float(a['open_price']),  5) if pd.notna(a['open_price'])  and pd.notna(b['open_price'])  else None
            close_slip = round(float(b['close_price']) - float(a['close_price']), 5) if pd.notna(a['close_price']) and pd.notna(b['close_price']) else None
            profit_var = round(float(b['net_profit'])  - float(a['net_profit']),  2) if pd.notna(a['net_profit'])  and pd.notna(b['net_profit'])  else None
            time_diff  = round((b['open_time'] - a['open_time']).total_seconds() / 60, 1)
            dur_diff   = round(float(b.get('duration_min', 0) or 0) - float(a.get('duration_min', 0) or 0), 1)

            matched.append({
                'A_idx'        : i,
                'B_idx'        : j,
                # File A
                'A_open_time'  : a['open_time'],
                'A_close_time' : a['close_time'],
                'A_symbol'     : a['symbol'],
                'A_type'       : a['type'],
                'A_volume'     : a.get('volume'),
                'A_open_price' : a.get('open_price'),
                'A_close_price': a.get('close_price'),
                'A_profit'     : a.get('net_profit'),
                'A_duration'   : a.get('duration_min'),
                # File B
                'B_open_time'  : b['open_time'],
                'B_close_time' : b['close_time'],
                'B_symbol'     : b['symbol'],
                'B_type'       : b['type'],
                'B_volume'     : b.get('volume'),
                'B_open_price' : b.get('open_price'),
                'B_close_price': b.get('close_price'),
                'B_profit'     : b.get('net_profit'),
                'B_duration'   : b.get('duration_min'),
                # Differences
                'open_slippage' : open_slip,
                'close_slippage': close_slip,
                'profit_var'    : profit_var,
                'time_diff_min' : time_diff,
                'duration_diff' : dur_diff,
            })

    return pd.DataFrame(matched)


# ── Discrepancies ─────────────────────────────────────────────────────────────

# A live account and a benchmark account run the same EA at different lot
# sizes, so raw dollars say nothing. Everything below is size-free: the
# direction of the result, profit per lot, and the fill/timing gap.

# Excess-slippage thresholds in basis points, from measuring this cache:
# after netting each symbol's median broker offset, gold / FX / indices sit
# within ±1bp, crypto is structurally an order of magnitude noisier.
SLIP_BP_DEFAULT = 2.0
SLIP_BP_CRYPTO  = 10.0
CRYPTO_TOKENS   = ('BTC', 'ETH', 'XRP', 'LTC', 'DOGE', 'SOL', 'ADA')


def _slip_threshold(symbol):
    su = str(symbol or '').upper()
    return SLIP_BP_CRYPTO if any(t in su for t in CRYPTO_TOKENS) else SLIP_BP_DEFAULT


def entry_slippage_bp(matched):
    # (entry_bp, excess_bp, thresholds) per matched pair.
    #
    # entry_bp: entry-price difference in basis points, signed against the
    # trade direction so positive always means B got the worse fill. Two
    # brokers quote the same symbol at a standing offset (USTEC sits ~7bp
    # apart between these feeds), so the flaggable number is the EXCESS over
    # the pair's median offset for that symbol — the median IS the feed
    # difference.
    raw, thr = [], []
    for _, r in matched.iterrows():
        try:
            a, b = float(r['A_open_price']), float(r['B_open_price'])
            sign = 1.0 if str(r['A_type']).lower() == 'buy' else -1.0
            raw.append(sign * (b - a) / a * 1e4)
        except (TypeError, ValueError, ZeroDivisionError, KeyError):
            raw.append(None)
        thr.append(_slip_threshold(r.get('A_symbol')))
    med = {}
    for sym in set(matched['A_symbol'].dropna()):
        vals = [v for v, (_, r) in zip(raw, matched.iterrows())
                if v is not None and r['A_symbol'] == sym]
        if vals:
            med[sym] = sorted(vals)[len(vals) // 2]
    excess = [None if v is None else round(v - med.get(r['A_symbol'], 0.0), 2)
              for v, (_, r) in zip(raw, matched.iterrows())]
    return ([None if v is None else round(v, 2) for v in raw], excess, thr)


DISC_OPPOSITE = '🔴 opposite result'
DISC_WORSE    = '🟡 behind per lot'
DISC_BETTER   = '🔼 ahead per lot'
DISC_SLIP     = '🟠 excess slippage'
DISC_LATE     = '⏱ late fill'
DISC_OK       = '✅ in line'


def _per_lot(profit, volume):
    try:
        v = float(volume)
        if v > 0:
            return float(profit) / v
    except (TypeError, ValueError):
        pass
    return None


def classify_pairs(matched, per_lot_tol=0.25, late_min=5.0,
                   excess_bp=None, slip_thr=None):
    """
    Per matched pair, what differs beyond position size:
      opposite result — one side won, the other lost (the real discrepancy)
      behind per lot  — same direction, B keeps materially less per lot
      ahead per lot   — B keeps materially MORE; on the same trade that is a
                        set-file / symbol / fill difference, not luck, so it
                        is flagged too
      excess slippage — B's entry is beyond the symbol's slippage threshold
                        after netting the standing broker offset (2bp, crypto
                        10bp) — a fill problem even when the P&L survived it
      late fill       — B opened more than `late_min` minutes away from A
    Returns (flags, a_per_lot, b_per_lot).
    """
    flags, a_pl, b_pl = [], [], []
    for i, (_, r) in enumerate(matched.iterrows()):
        exc = excess_bp[i] if excess_bp is not None else None
        thr = slip_thr[i] if slip_thr is not None else None
        pa = _per_lot(r.get('A_profit'), r.get('A_volume'))
        pb = _per_lot(r.get('B_profit'), r.get('B_volume'))
        a_pl.append(pa)
        b_pl.append(pb)
        try:
            ra, rb = float(r.get('A_profit')), float(r.get('B_profit'))
        except (TypeError, ValueError):
            flags.append(DISC_OK)
            continue
        td = abs(float(r.get('time_diff_min') or 0))
        if (ra > 0) != (rb > 0):
            flags.append(DISC_OPPOSITE)
        elif (pa is not None and pb is not None and pa != 0
              and (pb - pa) < -abs(pa) * per_lot_tol):
            flags.append(DISC_WORSE)
        elif (pa is not None and pb is not None and pa != 0
              and (pb - pa) > abs(pa) * per_lot_tol):
            flags.append(DISC_BETTER)
        elif exc is not None and thr and abs(exc) > thr:
            flags.append(DISC_SLIP)
        elif td > late_min:
            flags.append(DISC_LATE)
        else:
            flags.append(DISC_OK)
    return flags, a_pl, b_pl


# ── Render ────────────────────────────────────────────────────────────────────

def render():
    st.title("🔄 Trade Compare")

    st.markdown("""
    <div class="info-card">
        Compare two sets of trades — a backtest against a real account, one
        account against another, or any two exports.
        Trades are matched by symbol, direction, and open time within a configurable
        tolerance window to account for gaps, slippage, and market open variations.
    </div>
    """, unsafe_allow_html=True)

    # ── Session state ─────────────────────────────────────────────────────────
    for k in ['tc_df_a', 'tc_df_b', 'tc_fmt_a', 'tc_fmt_b',
              'tc_name_a', 'tc_name_b', 'tc_map_a', 'tc_map_b',
              'tc_span_a', 'tc_span_b']:
        if k not in st.session_state:
            st.session_state[k] = None

    # ── Load ──────────────────────────────────────────────────────────────────
    st.subheader("Load Files")

    with st.expander('🔀 Auto Compare All Accounts — Performance Discrepancies',
                     expanded=False):
        st.caption("EA's running on more than one of your cached accounts, "
                   "paired up and flagged first (🔀 behind its twin · 🔼 ahead "
                   "of it · 🛑/⚠️ tripped a rule · ✅ in line). A benchmark "
                   "account is the partner where there is one, since it runs "
                   "the pool at the standard size; otherwise it is another "
                   "live account running the same EA. Picking a pair loads "
                   "both reports — partner into A, live copy into B — so the "
                   "comparison below shows trade by trade where the two went "
                   "their own ways.")
        _render_flagged_loader()

    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("**File A** — Reference (backtest, benchmark, or any "
                    "other account)")
        up_a = st.file_uploader("Upload File A", type=['html','htm','csv'], key='tc_up_a')
        if up_a:
            df_a, fmt_a = detect_and_parse(up_a.read(), up_a.name)
            if df_a is not None:
                _set_side('A', df_a, fmt_a, name=os.path.splitext(up_a.name)[0])
                st.success(f"✓ {len(df_a)} trades — {fmt_a}")
            else:
                st.error("Could not parse File A")
        _render_ftp_source('A', 'tc_a')
        if st.session_state['tc_df_a'] is not None:
            _mapped = st.session_state.get('tc_map_a') or 0
            _span   = st.session_state.get('tc_span_a') or ''
            st.caption(f"Loaded: **{st.session_state['tc_fmt_a']}** · "
                       f"{len(st.session_state['tc_df_a'])} trades"
                       + (f" · {_span}" if _span else "")
                       + (f" · {_mapped} renamed to canonical EA names"
                          if _mapped else ""))

    with col_b:
        st.markdown("**File B** — Comparison (the account under review)")
        up_b = st.file_uploader("Upload File B", type=['html','htm','csv'], key='tc_up_b')
        if up_b:
            df_b, fmt_b = detect_and_parse(up_b.read(), up_b.name)
            if df_b is not None:
                _set_side('B', df_b, fmt_b, name=os.path.splitext(up_b.name)[0])
                st.success(f"✓ {len(df_b)} trades — {fmt_b}")
            else:
                st.error("Could not parse File B")
        _render_ftp_source('B', 'tc_b')
        if st.session_state['tc_df_b'] is not None:
            _mapped = st.session_state.get('tc_map_b') or 0
            _span   = st.session_state.get('tc_span_b') or ''
            st.caption(f"Loaded: **{st.session_state['tc_fmt_b']}** · "
                       f"{len(st.session_state['tc_df_b'])} trades"
                       + (f" · {_span}" if _span else "")
                       + (f" · {_mapped} renamed to canonical EA names"
                          if _mapped else ""))

    df_a = st.session_state['tc_df_a']
    df_b = st.session_state['tc_df_b']

    if df_a is None or df_b is None:
        return

    # ── Filters ───────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Filters")

    fa1, fa2, fa3 = st.columns(3)
    fb1, fb2, fb3 = st.columns(3)

    with fa1:
        st.markdown("**File A filters**")
    with fb1:
        st.markdown("**File B filters**")

    col1, col2, col3, col4, col5, col6 = st.columns(6)

    with col1:
        a_date_min = df_a['open_time'].min().date()
        a_date_max = df_a['open_time'].max().date()
        a_from = st.date_input("A — From", value=a_date_min, min_value=a_date_min,
                               max_value=a_date_max, key='tc_a_from')
        a_to   = st.date_input("A — To",   value=a_date_max, min_value=a_date_min,
                               max_value=a_date_max, key='tc_a_to')

    with col2:
        a_syms    = sorted(df_a['symbol'].dropna().unique().tolist())
        a_sel_sym = st.multiselect("A — Symbol", a_syms, key='tc_a_sym')

    with col3:
        a_strats    = sorted(df_a['strategy'].dropna().unique().tolist())
        a_sel_strat = st.multiselect("A — Strategy", a_strats, key='tc_a_strat')
        a_sel_type  = st.multiselect("A — Type", ['buy', 'sell'], key='tc_a_type')

    with col4:
        b_date_min = df_b['open_time'].min().date()
        b_date_max = df_b['open_time'].max().date()
        b_from = st.date_input("B — From", value=b_date_min, min_value=b_date_min,
                               max_value=b_date_max, key='tc_b_from')
        b_to   = st.date_input("B — To",   value=b_date_max, min_value=b_date_min,
                               max_value=b_date_max, key='tc_b_to')

    with col5:
        b_syms    = sorted(df_b['symbol'].dropna().unique().tolist())
        b_sel_sym = st.multiselect("B — Symbol", b_syms, key='tc_b_sym')

    with col6:
        b_strats    = sorted(df_b['strategy'].dropna().unique().tolist())
        b_sel_strat = st.multiselect("B — Strategy", b_strats, key='tc_b_strat')
        b_sel_type  = st.multiselect("B — Type", ['buy', 'sell'], key='tc_b_type')

    # ── Matching tolerance ────────────────────────────────────────────────────
    st.divider()
    col_tol, col_run = st.columns([3, 1])
    with col_tol:
        tolerance = st.slider(
            "Match tolerance (hours) — max time difference between A and B open times",
            min_value=1, max_value=24, value=4, step=1,
            help="Trades within this window are considered the same setup. "
                 "Increase for daily charts, decrease for intraday."
        )
    with col_run:
        st.markdown("<br>", unsafe_allow_html=True)
        run = st.button("🔍 Match Trades", type="primary", use_container_width=True)

    run = run or st.session_state.pop('tc_auto_match', False)

    if not run and 'tc_matched' not in st.session_state:
        return

    # Apply filters
    fa = df_a.copy()
    fa = fa[(fa['open_time'].dt.date >= a_from) & (fa['open_time'].dt.date <= a_to)]
    if a_sel_sym:   fa = fa[fa['symbol'].isin(a_sel_sym)]
    if a_sel_strat: fa = fa[fa['strategy'].isin(a_sel_strat)]
    if a_sel_type:  fa = fa[fa['type'].isin(a_sel_type)]

    fb = df_b.copy()
    fb = fb[(fb['open_time'].dt.date >= b_from) & (fb['open_time'].dt.date <= b_to)]
    if b_sel_sym:   fb = fb[fb['symbol'].isin(b_sel_sym)]
    if b_sel_strat: fb = fb[fb['strategy'].isin(b_sel_strat)]
    if b_sel_type:  fb = fb[fb['type'].isin(b_sel_type)]

    if run:
        with st.spinner("Matching trades..."):
            matched = match_trades(fa, fb, tolerance)
        st.session_state['tc_matched'] = matched
        st.session_state['tc_fa_len']  = len(fa)
        st.session_state['tc_fb_len']  = len(fb)
        m_a = set(matched['A_idx']) if 'A_idx' in matched.columns else set()
        m_b = set(matched['B_idx']) if 'B_idx' in matched.columns else set()
        st.session_state['tc_only_a'] = fa[~fa.index.isin(m_a)]
        st.session_state['tc_only_b'] = fb[~fb.index.isin(m_b)]

    matched  = st.session_state.get('tc_matched', pd.DataFrame())
    fa_len   = st.session_state.get('tc_fa_len', len(fa))
    fb_len   = st.session_state.get('tc_fb_len', len(fb))

    if matched is None or len(matched) == 0:
        st.warning("No matching trades found — try increasing the tolerance window or adjusting filters.")
        return

    # ── Summary stats ─────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Match Summary")

    span_a = st.session_state.get('tc_span_a') or ''
    span_b = st.session_state.get('tc_span_b') or ''
    if span_a or span_b:
        st.caption(f"A: {span_a or 'n/a'}  ·  B: {span_b or 'n/a'}  ·  "
                   f"tolerance {tolerance}h")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("File A Trades",   fa_len)
    m2.metric("File B Trades",   fb_len)
    m3.metric("Matched Pairs",   len(matched))
    m4.metric("Unmatched A",     fa_len - len(matched))
    m5.metric("Unmatched B",     fb_len - len(matched))

    only_a = st.session_state.get('tc_only_a')
    only_b = st.session_state.get('tc_only_b')
    if (only_a is not None and len(only_a)) or (only_b is not None and len(only_b)):
        with st.expander('Trades only one side took — the biggest discrepancy '
                         'of all'):
            st.caption('No counterpart within the tolerance window. On two '
                       'accounts running the same EA these are the trades '
                       'one of them missed (or took on its own): EA stopped, '
                       'set-file difference, margin, or the terminal was '
                       'down.')
            ua, ub = st.columns(2)
            cols = ['open_time', 'symbol', 'type', 'volume', 'open_price',
                    'close_price', 'net_profit']
            for col, frame, name in ((ua, only_a, 'A only'),
                                     (ub, only_b, 'B only')):
                with col:
                    n = 0 if frame is None else len(frame)
                    st.markdown(f'**{name}** — {n} trade(s)')
                    if n:
                        st.dataframe(frame[[c for c in cols if c in frame.columns]],
                                     use_container_width=True, hide_index=True,
                                     height=240)

    st.divider()

    # ── Aggregate comparison ───────────────────────────────────────────────────
    st.subheader("Aggregate Comparison")

    a_net = matched['A_profit'].sum()
    b_net = matched['B_profit'].sum()
    a_wr  = (matched['A_profit'] > 0).mean() * 100
    b_wr  = (matched['B_profit'] > 0).mean() * 100
    a_avg = matched['A_profit'].mean()
    b_avg = matched['B_profit'].mean()
    a_dur = matched['A_duration'].mean() if 'A_duration' in matched else None
    b_dur = matched['B_duration'].mean() if 'B_duration' in matched else None
    a_lot = matched['A_volume'].sum() if 'A_volume' in matched else None
    b_lot = matched['B_volume'].sum() if 'B_volume' in matched else None
    a_pl  = (a_net / a_lot) if a_lot else None
    b_pl  = (b_net / b_lot) if b_lot else None

    def _money(v):    return '' if v is None or pd.isna(v) else f"${v:,.2f}"
    def _dmoney(v):   return '' if v is None or pd.isna(v) else f"{v:+,.2f}"
    def _pct(v):      return '' if v is None or pd.isna(v) else f"{v:.1f}%"
    def _dpct(v):     return '' if v is None or pd.isna(v) else f"{v:+.1f}%"
    def _mins(v):     return '' if v is None or pd.isna(v) else f"{v:,.0f}m"
    def _dmins(v):    return '' if v is None or pd.isna(v) else f"{v:+,.0f}m"

    agg = [
        ('Net profit',   _money(a_net), _money(b_net), _dmoney(b_net - a_net)),
        ('Win rate',     _pct(a_wr),    _pct(b_wr),    _dpct(b_wr - a_wr)),
        ('Avg profit / trade', _money(a_avg), _money(b_avg), _dmoney(b_avg - a_avg)),
    ]
    if a_pl is not None and b_pl is not None:
        # A and B can run different lot sizes (a benchmark account against a
        # live one), which makes the dollar rows incomparable and this one the
        # honest read.
        agg.append(('Net profit per lot', _money(a_pl), _money(b_pl),
                    _dmoney(b_pl - a_pl)))
    if a_dur is not None and b_dur is not None:
        agg.append(('Avg duration', _mins(a_dur), _mins(b_dur),
                    _dmins(b_dur - a_dur)))

    agg_tbl = pd.DataFrame(agg, columns=['Metric', 'A (reference)',
                                         'B (comparison)', 'Δ (B − A)'])

    def _colour_delta(val):
        try:
            v = float(str(val).replace(',', '').replace('$', '')
                      .replace('%', '').replace('m', ''))
        except ValueError:
            return ''
        if v > 0:  return 'color: #2dc653; font-weight: 600'
        if v < 0:  return 'color: #e63946; font-weight: 600'
        return 'color: #666'

    st.dataframe(agg_tbl.style.map(_colour_delta, subset=['Δ (B − A)']),
                 use_container_width=True, hide_index=True)

    # ── Slippage summary ───────────────────────────────────────────────────────
    st.divider()
    st.subheader("Slippage & Variance Summary")

    entry_bp_s, excess_bp_s, slip_thr_s = entry_slippage_bp(matched)
    exc_vals = [(e, t) for e, t in zip(excess_bp_s, slip_thr_s) if e is not None]
    n_over = sum(1 for e, t in exc_vals if abs(e) > t)
    avg_excess = (sum(abs(e) for e, _ in exc_vals) / len(exc_vals)
                  if exc_vals else None)

    sc1, sc2, sc3, sc4 = st.columns(4)
    avg_open_slip  = matched['open_slippage'].mean()
    avg_close_slip = matched['close_slip'].mean() if 'close_slip' in matched else matched['close_slippage'].mean()
    avg_profit_var = matched['profit_var'].mean()
    avg_time_diff  = matched['time_diff_min'].mean()

    sc1.metric("Avg Entry Slippage",  f"{avg_open_slip:+.5f}"  if pd.notna(avg_open_slip) else "N/A",
               help="B open price minus A open price. Positive = B filled higher.")
    sc2.metric("Avg Exit Slippage",   f"{avg_close_slip:+.5f}" if pd.notna(avg_close_slip) else "N/A",
               help="B close price minus A close price.")
    sc3.metric("Avg Profit Variance", f"${avg_profit_var:+.2f}" if pd.notna(avg_profit_var) else "N/A",
               help="B net profit minus A net profit per trade.")
    sc4.metric("Avg Time Difference", f"{avg_time_diff:+.0f}m" if pd.notna(avg_time_diff) else "N/A",
               help="B open time minus A open time in minutes.")

    sc5, sc6, sc7 = st.columns([1, 1, 2])
    sc5.metric("Avg Excess Slippage", f"{avg_excess:.2f}bp" if avg_excess is not None else "N/A",
               help="Entry-price difference in basis points, signed against "
                    "the trade direction, after netting each symbol's median "
                    "offset between the two accounts — the median is the "
                    "brokers' standing feed difference, not slippage.")
    sc6.metric("Over Threshold", n_over,
               help="Matched trades whose excess slippage is beyond the "
                    "symbol's threshold: 2bp, crypto 10bp.")
    _meds = {}
    for sym in sorted(set(matched['A_symbol'].dropna())):
        vals = [b for b, (_, r) in zip(entry_bp_s, matched.iterrows())
                if b is not None and r['A_symbol'] == sym]
        if vals:
            _meds[sym] = sorted(vals)[len(vals) // 2]
    if _meds:
        sc7.caption('Standing broker offset (median entry bp): '
                    + ' · '.join(f'{k} {v:+.1f}' for k, v in _meds.items()))

    # ── Equity curve overlay ───────────────────────────────────────────────────
    st.divider()
    st.subheader("Equity Curve Overlay")

    name_a = st.session_state.get('tc_name_a') or 'File A'
    name_b = st.session_state.get('tc_name_b') or 'File B'

    m_sorted = matched.sort_values('A_open_time')
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=m_sorted['A_open_time'],
        y=m_sorted['A_profit'].cumsum(),
        mode='lines', name=name_a,
        line=dict(color='#7c6af7', width=2),
        fill='tozeroy', fillcolor='rgba(124,106,247,0.05)'
    ))
    fig.add_trace(go.Scatter(
        x=m_sorted['B_open_time'],
        y=m_sorted['B_profit'].cumsum(),
        mode='lines', name=name_b,
        line=dict(color='#2dc653', width=2),
        fill='tozeroy', fillcolor='rgba(45,198,83,0.05)'
    ))
    fig.update_layout(
        height=320,
        plot_bgcolor='rgba(10,10,15,1)',
        paper_bgcolor='rgba(10,10,15,1)',
        font=dict(color='#aaa', family='JetBrains Mono'),
        xaxis=dict(gridcolor='rgba(255,255,255,0.04)'),
        yaxis=dict(gridcolor='rgba(255,255,255,0.04)', tickprefix='$'),
        legend=dict(bgcolor='rgba(0,0,0,0.3)'),
        margin=dict(l=60, r=20, t=20, b=40)
    )
    st.plotly_chart(fig, use_container_width=True)

    # ── Profit variance scatter ────────────────────────────────────────────────
    st.subheader("Profit Variance per Trade")
    fig2 = go.Figure()
    colours = matched['profit_var'].apply(
        lambda v: 'rgba(45,198,83,0.7)' if v >= 0 else 'rgba(230,57,70,0.7)'
    )
    fig2.add_trace(go.Bar(
        x=list(range(1, len(matched) + 1)),
        y=matched['profit_var'],
        marker_color=colours,
        name='Profit Variance (B - A)'
    ))
    # Trade numbers are whole trades: force an integer tick step, thinned out
    # as the list grows, so the axis never lands on half a trade.
    fig2.update_layout(
        height=250,
        plot_bgcolor='rgba(10,10,15,1)',
        paper_bgcolor='rgba(10,10,15,1)',
        font=dict(color='#aaa'),
        xaxis=dict(gridcolor='rgba(255,255,255,0.04)', title='Trade #',
                   tickmode='linear', tick0=1,
                   dtick=max(1, round(len(matched) / 20)) or 1),
        yaxis=dict(gridcolor='rgba(255,255,255,0.04)', tickprefix='$'),
        margin=dict(l=60, r=20, t=20, b=40)
    )
    st.plotly_chart(fig2, use_container_width=True)

    # ── Matched trade table ────────────────────────────────────────────────────
    st.divider()
    st.subheader("Matched Trade Detail")

    def colour_diff(val):
        try:
            v = float(str(val).replace('+', ''))
            if v > 0:  return 'color: #2dc653; font-weight: 600'
            if v < 0:  return 'color: #e63946; font-weight: 600'
        except:
            pass
        return 'color: #666'

    def colour_profit_cell(val):
        try:
            v = float(str(val).replace(',', ''))
            if v > 0: return 'background-color: rgba(0,180,0,0.10)'
            if v < 0: return 'background-color: rgba(180,0,0,0.10)'
        except:
            pass
        return ''

    entry_bp, excess_bp, slip_thr = entry_slippage_bp(matched)
    flags, a_pl, b_pl = classify_pairs(matched, excess_bp=excess_bp,
                                       slip_thr=slip_thr)
    matched = matched.copy()
    matched['flag']    = flags
    matched['A_perlot'] = a_pl
    matched['B_perlot'] = b_pl
    matched['entry_bp']  = entry_bp
    matched['excess_bp'] = excess_bp

    n_disc = sum(f != DISC_OK for f in flags)
    fc1, fc2 = st.columns([1, 3])
    only_disc = fc1.checkbox(f'Only discrepancies ({n_disc})', value=False,
                             key='tc_only_disc')
    fc2.caption('🔴 opposite result — one side won, the other lost · 🟡 behind '
                'per lot — B keeps materially less · 🔼 ahead per lot — B keeps '
                'materially more · 🟠 excess slippage — entry fill beyond the '
                'symbol threshold (2bp, crypto 10bp) once the standing broker '
                'offset is netted out, which on the same trade is a set-file / '
                'symbol / fill difference, not luck · 🟠 late fill — opened '
                'more than 5 minutes apart. Per lot throughout, because A and '
                'B run different sizes.')
    if only_disc:
        matched = matched[matched['flag'] != DISC_OK]
        if matched.empty:
            st.success('No discrepancies on the matched trades.')
            return

    display = matched[[
        'flag',
        'A_open_time', 'A_symbol', 'A_type',
        'A_open_price', 'A_close_price', 'A_profit', 'A_perlot', 'A_duration',
        'B_open_time',
        'B_open_price', 'B_close_price', 'B_profit', 'B_perlot', 'B_duration',
        'open_slippage', 'close_slippage', 'entry_bp', 'excess_bp',
        'profit_var', 'time_diff_min'
    ]].copy()

    display.columns = [
        'Flag',
        'A Open Time', 'Symbol', 'Type',
        'A Entry', 'A Exit', 'A Profit', 'A $/lot', 'A Dur(m)',
        'B Open Time',
        'B Entry', 'B Exit', 'B Profit', 'B $/lot', 'B Dur(m)',
        'Entry Slip', 'Exit Slip', 'Entry Slip (bp)', 'Excess (bp)',
        'Profit Var', 'Time Diff(m)'
    ]

    # Format numeric columns
    for col in ['A Entry', 'A Exit', 'B Entry', 'B Exit']:
        if col in display.columns:
            display[col] = display[col].apply(
                lambda x: f"{x:.5f}" if pd.notna(x) else '')

    for col in ['A $/lot', 'B $/lot']:
        display[col] = display[col].apply(
            lambda x: f"{x:+,.2f}" if pd.notna(x) else '')

    for col in ['A Profit', 'B Profit', 'Profit Var']:
        display[col] = display[col].apply(
            lambda x: f"{x:+.2f}" if pd.notna(x) else '')

    for col in ['Entry Slip', 'Exit Slip']:
        display[col] = display[col].apply(
            lambda x: f"{x:+.5f}" if pd.notna(x) else '')

    for col in ['Entry Slip (bp)', 'Excess (bp)']:
        display[col] = display[col].apply(
            lambda x: f"{x:+.2f}" if pd.notna(x) else '')

    def colour_flag_row(row):
        tint = {DISC_OPPOSITE: 'rgba(230,57,70,0.13)',
                DISC_WORSE:    'rgba(240,200,60,0.10)',
                DISC_BETTER:   'rgba(60,160,240,0.10)',
                DISC_LATE:     'rgba(240,150,60,0.10)'}.get(row['Flag'], '')
        return [f'background-color: {tint}' if tint else ''] * len(row)

    st.dataframe(
        display.style
            .apply(colour_flag_row,  axis=1)
            .map(colour_diff,        subset=['Entry Slip', 'Exit Slip', 'Entry Slip (bp)', 'Excess (bp)', 'Profit Var', 'Time Diff(m)'])
            .map(colour_profit_cell, subset=['A Profit', 'B Profit']),
        use_container_width=True, hide_index=True, height=500
    )

    # ── Export ────────────────────────────────────────────────────────────────
    st.download_button(
        "⬇ Download matched trades CSV",
        data      = display.to_csv(index=False),
        file_name = "trade_comparison.csv",
        mime      = 'text/csv'
    )
