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


def _set_side(side: str, df, fmt: str):
    """Put a dataframe on side A or B, dropping that side's now-stale filter
    widgets (dates from the previous file would fall outside the new range)."""
    k = side.lower()
    st.session_state[f'tc_df_{k}']  = df
    st.session_state[f'tc_fmt_{k}'] = fmt
    for key in _FILTER_KEYS[side]:
        st.session_state.pop(key, None)
    st.session_state.pop('tc_matched', None)


# ── Flagged pairs from the benchmark check ─────────────────────────────

@st.cache_data(show_spinner=False)
def _flagged_pairs(_sig):
    """
    (pairs, note) — account/EA rows the benching tool has flagged on the
    🎛 Live UBS EA Management page, each resolved to the two reports worth
    comparing: the live copy and its twin on a benchmark account.

    Nothing is re-judged here; this only saves hunting for the right two
    reports. `_sig` is the cache fingerprint, so the list refreshes when the
    FTP cache or the rules change.
    """
    try:
        from live_rules import (load_rules_config, bench_signals, live_vs_bench,
                                load_name_map, canonicalize)
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
    rows = live_vs_bench(cached, bench_signals(cached, rules), rules)

    pairs = []
    for r in rows:
        if not (r['diverges'] or r.get('bench_level') in ('triggered', 'warning')):
            continue
        twin = next((a for a in refs if r['strategy'] in strats_on.get(a, ())), None)
        if twin is None:
            continue                      # no benchmark twin -> nothing to compare
        icon = '🔀' if r['diverges'] else (
            '🛑' if r['bench_level'] == 'triggered' else '⚠️')
        pairs.append({
            'label': f"{icon} {r['strategy']} — live on {r['account']} "
                     f"vs bench {twin}",
            'strategy': r['strategy'],
            'live_account': r['account'],
            'live_folder': folder_of.get(r['account']),
            'bench_account': twin,
            'bench_folder': folder_of.get(twin),
            'why': r['divergence'] + (r.get('bench_triggers') or []),
            'live_streak': r.get('live_streak'),
            'bench_streak': r.get('bench_streak'),
            'streak_unit': r.get('streak_unit', 'days'),
            'live_window_pnl': r.get('live_window_pnl'),
            'bench_window_pnl': r.get('bench_window_pnl'),
            'lookback': int(rules.get('lookback_days', 63)),
        })
    pairs.sort(key=lambda p: (p['label'][0] != '🔀', p['label']))
    if not pairs:
        twins = sum(1 for r in rows
                    if any(r['strategy'] in strats_on.get(a, ()) for a in refs))
        return [], (f"Nothing flagged — {len(rows)} live EA/account row(s), "
                    f"{twins} of them with a twin on a benchmark account. "
                    "A robot whose live trade comments are not bridged to the "
                    "benchmark names has nothing to be measured against "
                    "(🎛 Live UBS EA Management → 🔗 Name mapping). Load the "
                    "reports manually below.")
    return pairs, None


def _ea_trades(account_folder, strategy, days=None):
    """That account's cached trades for one EA (canonical names), optionally
    trimmed to the last `days` of its own history."""
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
    if days:
        cutoff = df['open_time'].max() - pd.Timedelta(days=int(days))
        df = df[df['open_time'] >= cutoff]
    return df.sort_values('open_time')


def _render_flagged_loader():
    """Dropdown of flagged account/EA pairs that loads the two reports into
    A and B. Purely a shortcut — the comparison below is the normal one."""
    pairs, note = _flagged_pairs(_cache_signature())
    if note:
        st.caption(note)
        return
    if not pairs:
        return

    labels = [p['label'] for p in pairs]
    c1, c2 = st.columns([4, 1])
    pick = c1.selectbox(
        'Flagged against benchmark', labels, key='tc_flag_pick',
        help='Account/EA rows flagged on 🎛 Live UBS EA Management. Picking '
             'one loads the benchmark copy into A and the live copy into B.')
    p = pairs[labels.index(pick)]
    with c2:
        st.markdown('<br>', unsafe_allow_html=True)
        load = st.button('Load A/B', type='primary', use_container_width=True,
                         key='tc_flag_load')

    bits = []
    if p['live_streak'] is not None and p['bench_streak'] is not None:
        bits.append(f"live streak {p['live_streak']} {p['streak_unit']} vs bench "
                    f"{p['bench_streak']}")
    if p['live_window_pnl'] is not None and p['bench_window_pnl'] is not None:
        bits.append(f"window P&L \\${p['live_window_pnl']:,.0f} live vs "
                    f"\\${p['bench_window_pnl']:,.0f} bench")
    if bits:
        st.caption(' · '.join(bits) + f" (last {p['lookback']} trading days)")
    for w in p['why'][:4]:
        st.caption('· ' + w.replace('$', chr(92) + '$'))

    if load:
        b = _ea_trades(p['bench_folder'], p['strategy'], days=p['lookback'] * 2)
        l = _ea_trades(p['live_folder'],  p['strategy'], days=p['lookback'] * 2)
        if b is None or l is None or b.empty or l.empty:
            st.error('No cached trades for that EA on one of the accounts — '
                     'refresh on the 📡 Live MT5 EA\'s page.')
        else:
            _set_side('A', b, f"Bench · {p['bench_account']} · {p['strategy']}")
            _set_side('B', l, f"Live · {p['live_account']} · {p['strategy']}")
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
            _set_side(side, df.dropna(subset=['open_time']), f'FTP · {sel}')
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

# A live account and a benchmark account run the same robot at different lot
# sizes, so raw dollars say nothing. Everything below is size-free: the
# direction of the result, profit per lot, and the fill/timing gap.

DISC_OPPOSITE = '🔴 opposite result'
DISC_WORSE    = '🟡 worse per lot'
DISC_LATE     = '🟠 late fill'
DISC_OK       = '✅ in line'


def _per_lot(profit, volume):
    try:
        v = float(volume)
        if v > 0:
            return float(profit) / v
    except (TypeError, ValueError):
        pass
    return None


def classify_pairs(matched, per_lot_tol=0.25, late_min=5.0):
    """
    Per matched pair, what differs beyond position size:
      opposite result — one side won, the other lost (the real discrepancy)
      worse per lot   — same direction, B keeps materially less per lot
      late fill       — B opened more than `late_min` minutes away from A
    Returns (flags, a_per_lot, b_per_lot).
    """
    flags, a_pl, b_pl = [], [], []
    for _, r in matched.iterrows():
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
        Compare two trade history files — backtest vs real account, or any two exports.
        Trades are matched by symbol, direction, and open time within a configurable
        tolerance window to account for gaps, slippage, and market open variations.
    </div>
    """, unsafe_allow_html=True)

    # ── Session state ─────────────────────────────────────────────────────────
    for k in ['tc_df_a', 'tc_df_b', 'tc_fmt_a', 'tc_fmt_b']:
        if k not in st.session_state:
            st.session_state[k] = None

    # ── Load ──────────────────────────────────────────────────────────────────
    st.subheader("Load Files")

    _pairs, _ = _flagged_pairs(_cache_signature())
    with st.expander(
            "🔀 Flagged against the benchmark accounts"
            + (f" ({len(_pairs)}) — load that pair" if _pairs else " — none right now"),
            expanded=bool(_pairs)):
        st.caption("The account/EA rows the benching tool has already flagged. "
                   "Picking one just loads the two reports — benchmark copy "
                   "into A, live copy into B — so the comparison below shows "
                   "trade by trade where the live copy went its own way.")
        _render_flagged_loader()

    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("**File A** — Reference (backtest, or the benchmark account)")
        up_a = st.file_uploader("Upload File A", type=['html','htm','csv'], key='tc_up_a')
        if up_a:
            df_a, fmt_a = detect_and_parse(up_a.read(), up_a.name)
            if df_a is not None:
                _set_side('A', df_a, fmt_a)
                st.success(f"✓ {len(df_a)} trades — {fmt_a}")
            else:
                st.error("Could not parse File A")
        _render_ftp_source('A', 'tc_a')
        if st.session_state['tc_df_a'] is not None:
            st.caption(f"Loaded: **{st.session_state['tc_fmt_a']}** · {len(st.session_state['tc_df_a'])} trades")

    with col_b:
        st.markdown("**File B** — Comparison (the live account)")
        up_b = st.file_uploader("Upload File B", type=['html','htm','csv'], key='tc_up_b')
        if up_b:
            df_b, fmt_b = detect_and_parse(up_b.read(), up_b.name)
            if df_b is not None:
                _set_side('B', df_b, fmt_b)
                st.success(f"✓ {len(df_b)} trades — {fmt_b}")
            else:
                st.error("Could not parse File B")
        _render_ftp_source('B', 'tc_b')
        if st.session_state['tc_df_b'] is not None:
            st.caption(f"Loaded: **{st.session_state['tc_fmt_b']}** · {len(st.session_state['tc_df_b'])} trades")

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
            st.caption('No counterpart within the tolerance window. On a live '
                       'vs benchmark pair these are the trades the live copy '
                       'missed (or took on its own): EA stopped, set-file '
                       'difference, margin, or the terminal was down.')
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

    ac1, ac2 = st.columns(2)

    with ac1:
        st.markdown("**File A (Reference)**")
        a_net   = matched['A_profit'].sum()
        a_wr    = (matched['A_profit'] > 0).mean() * 100
        a_avg   = matched['A_profit'].mean()
        a_dur   = matched['A_duration'].mean() if 'A_duration' in matched else None
        st.metric("Net Profit",   f"${a_net:,.2f}")
        st.metric("Win Rate",     f"{a_wr:.1f}%")
        st.metric("Avg Profit",   f"${a_avg:,.2f}")
        if a_dur:
            st.metric("Avg Duration", f"{a_dur:.0f}m")

    with ac2:
        st.markdown("**File B (Comparison)**")
        b_net   = matched['B_profit'].sum()
        b_wr    = (matched['B_profit'] > 0).mean() * 100
        b_avg   = matched['B_profit'].mean()
        b_dur   = matched['B_duration'].mean() if 'B_duration' in matched else None
        delta_net = b_net - a_net
        st.metric("Net Profit",   f"${b_net:,.2f}",
                  delta=f"{delta_net:+.2f}", delta_color="normal")
        st.metric("Win Rate",     f"{b_wr:.1f}%",
                  delta=f"{b_wr - a_wr:+.1f}%", delta_color="normal")
        st.metric("Avg Profit",   f"${b_avg:,.2f}",
                  delta=f"{b_avg - a_avg:+.2f}", delta_color="normal")
        if b_dur and a_dur:
            st.metric("Avg Duration", f"{b_dur:.0f}m",
                      delta=f"{b_dur - a_dur:+.0f}m", delta_color="off")

    # ── Slippage summary ───────────────────────────────────────────────────────
    st.divider()
    st.subheader("Slippage & Variance Summary")

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

    # ── Equity curve overlay ───────────────────────────────────────────────────
    st.divider()
    st.subheader("Equity Curve Overlay")

    m_sorted = matched.sort_values('A_open_time')
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=m_sorted['A_open_time'],
        y=m_sorted['A_profit'].cumsum(),
        mode='lines', name='File A',
        line=dict(color='#7c6af7', width=2),
        fill='tozeroy', fillcolor='rgba(124,106,247,0.05)'
    ))
    fig.add_trace(go.Scatter(
        x=m_sorted['B_open_time'],
        y=m_sorted['B_profit'].cumsum(),
        mode='lines', name='File B',
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

    flags, a_pl, b_pl = classify_pairs(matched)
    matched = matched.copy()
    matched['flag']    = flags
    matched['A_perlot'] = a_pl
    matched['B_perlot'] = b_pl

    n_disc = sum(f != DISC_OK for f in flags)
    fc1, fc2 = st.columns([1, 3])
    only_disc = fc1.checkbox(f'Only discrepancies ({n_disc})', value=False,
                             key='tc_only_disc')
    fc2.caption('🔴 opposite result — one side won, the other lost · 🟡 worse '
                'per lot — same direction, materially less kept per lot · 🟠 '
                'late fill — opened more than 5 minutes apart. Per-lot figures '
                'because A and B run different sizes.')
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
        'open_slippage', 'close_slippage', 'profit_var', 'time_diff_min'
    ]].copy()

    display.columns = [
        'Flag',
        'A Open Time', 'Symbol', 'Type',
        'A Entry', 'A Exit', 'A Profit', 'A $/lot', 'A Dur(m)',
        'B Open Time',
        'B Entry', 'B Exit', 'B Profit', 'B $/lot', 'B Dur(m)',
        'Entry Slip', 'Exit Slip', 'Profit Var', 'Time Diff(m)'
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

    def colour_flag_row(row):
        tint = {DISC_OPPOSITE: 'rgba(230,57,70,0.13)',
                DISC_WORSE:    'rgba(240,200,60,0.10)',
                DISC_LATE:     'rgba(240,150,60,0.10)'}.get(row['Flag'], '')
        return [f'background-color: {tint}' if tint else ''] * len(row)

    st.dataframe(
        display.style
            .apply(colour_flag_row,  axis=1)
            .map(colour_diff,        subset=['Entry Slip', 'Exit Slip', 'Profit Var', 'Time Diff(m)'])
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
