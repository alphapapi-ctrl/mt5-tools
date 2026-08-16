"""
view_live_ea_portfolio.py
=========================
Live EA Portfolio Management page — the decision layer on top of the live
reporting pages. Applies a rules-based benching system (validated in
backtest simulation) to live/demo account data:

  - benching rules checked on the BENCHMARK accounts (demo accounts running
    the whole robot pool at the standard size), tripped robots matched to
    the live accounts running them
  - size-free "live copy worse than its bench twin" check
  - swap-in candidates ranked on recent form (bundled real-tick proxy,
    refreshed weekly) with bench forward results filling in over time

STANDALONE: everything it needs ships with MT5Tools — engine_data/ holds
the compiled datasets (baseline + real-tick proxy), engine_lib/ the compiler
and fill-trust helper. No other repo required.

Reporting pages show what IS; this page says what the rules would DO.
"""

import io
import os
import sys
import glob
import json
import contextlib

import numpy as np
import streamlit as st
import pandas as pd

from live_rules import (load_rules_config, save_rules_config, evaluate_all,
                        load_proxy, reference_forward_stats,
                        forward_stats_by_strategy, DEFAULT_RULES,
                        bench_signals, live_vs_bench,
                        load_bench_log, save_bench_log, cooldown_status,
                        list_proxy_timelines, DATA_ROOT, LIB_DIR)
from view_live_mt5_eas import get_all_cached, load_account_configs, CACHE_DIR

LEVEL_ICON = {'triggered': '🛑', 'warning': '⚠️', 'ok': '✅', 'inactive': '💤'}

# Fill-trust badges (computed by the engine's fill_trust.py into ea_meta.csv)
TRUST_ICON = {'real': '✅', 'high': '🟢', 'medium': '🟡', 'low': '🔴',
              'unknown': '⚪', 'mixed': '◔'}
TRUST_LEGEND = ('Fill trust — how much the backtest fills can be believed, '
                'from a real-tick check of the last 3 months: ✅ real-tick '
                'report · 🟢 high (real ticks keep ≥85% of the OHLC profit) · '
                '🟡 medium (50–85%) · 🔴 low (<50%, or profit turns to loss on '
                'real ticks) · ⚪ not checked. Low-trust robots\' backtest '
                'numbers are fill artifacts as much as edge.')


def _trust_by_strategy(rules):
    """{strategy: fill_trust} from the baseline timeline's ea_meta.csv."""
    bdir = rules.get('baseline_timeline_dir') or ''
    p = os.path.join(bdir, 'ea_meta.csv')
    if not os.path.isfile(p):
        return {}
    m = pd.read_csv(p)
    if 'fill_trust' not in m.columns:
        return {}
    return dict(zip(m.strategy, m.fill_trust))


def render():
    st.title("🎛 Live EA Portfolio Management")
    st.caption("The rules-based review layer. Your written benching rules are "
               "checked against the **benchmark accounts** — demo accounts "
               "running the whole robot pool at the standard size, where the "
               "rule thresholds mean exactly what they say — and any robot that "
               "trips a rule there is then matched to the **live accounts** "
               "running it. No triggers on the bench = no decisions to make "
               "today. Data comes from the same FTP cache as the Live MT5 EA's "
               "page (refresh there).")

    rules = load_rules_config()

    tab_mgmt, tab_rules, tab_bench = st.tabs(
        ['🎛 Management', '⚙️ Benching rules', '🧪 Benchmark accounts'])
    with tab_mgmt:
        _render_management(rules)
    with tab_rules:
        _render_rules_tab(rules)
    with tab_bench:
        _render_benchmark_config(rules)
    # The live layer is deliberately just: the rules, the bench, the
    # candidates. Regime analysis is a separate research tool.


def _setup_state(rules):
    """(refs_ok, proxy_set, proxy_ok, base_ok, setup_ok) — per-install config
    lives in ea_rules_config.json, never in code."""
    refs_ok  = bool(rules.get('reference_accounts'))
    proxy_set = bool(rules.get('proxy_timeline_dir'))
    proxy_ok = os.path.isfile(os.path.join(
        rules.get('proxy_timeline_dir') or '', 'daily_pnl.csv'))
    base_ok  = os.path.isfile(os.path.join(
        rules.get('baseline_timeline_dir') or '', 'ea_meta.csv'))
    return refs_ok, proxy_set, proxy_ok, base_ok, (refs_ok and base_ok)


def _render_setup_banner(rules, where):
    refs_ok, _, _, base_ok, setup_ok = _setup_state(rules)
    if setup_ok:
        return
    todo = []
    if not refs_ok:
        todo.append('**pick your benchmark (reference) accounts** on the '
                    '**🧪 Benchmark accounts** tab — the demo accounts '
                    'running your candidate pool. They come from your own '
                    "Live MT5 EA's account configs; every install selects "
                    'its own.')
    if not base_ok:
        todo.append('**baseline dataset missing** — MT5Tools ships it in '
                    '`engine_data\\timeline\\main_pool_2018`; if that folder '
                    'is gone, re-pull the repo, or point the baseline at a '
                    'compiled timeline on the **⚙️ Benching rules** tab. It '
                    'supplies the swap-in ranking and each EA\'s "historical '
                    'worst" for the relative rules.')
    st.info('🛠 **Setup needed** — this page adapts to whatever accounts '
            'and datasets you configure; nothing is tied to any specific '
            'portfolio. To finish setting up:\n\n' +
            '\n'.join(f'- {t}' for t in todo))


def _render_rules_tab(rules):
    _render_setup_banner(rules, 'rules')
    _, proxy_set, proxy_ok, base_ok, _ = _setup_state(rules)
    st.caption('These rules are checked against the **benchmark accounts**, '
               'robot by robot. Thresholds are in dollars at the standard '
               'baseline (100k balance, lot step = HistMaxDD/5%) — exactly the '
               'size the bench runs at, so a "\\$1,000 streak cost" means the '
               'same thing for every robot. A robot that trips a rule on the '
               'bench is then matched to whichever live accounts run it (🎛 '
               'Management → Affected live accounts). Same rule set the '
               'backtest engine validated: a firm streak-cost rule with prompt '
               'reviews did the heavy lifting; the drawdown rule is insurance.')
    with st.form('rules_form'):
        c1, c2, c3 = st.columns(3)
        mode = c1.radio('Count streaks in', ['days', 'trades'],
                        index=0 if rules.get('streak_mode', 'days') == 'days' else 1,
                        horizontal=True)
        streak = c2.number_input('Losing streak limit (0 = off)', 0, 30,
                                 int(rules.get('loss_streak_limit') or 0))
        cost = c3.number_input('Streak cost limit $ (0 = off)', 0, 30000,
                               int(rules.get('streak_dollar_limit') or 0), step=250)
        c4, c5, c6 = st.columns(3)
        dd = c4.number_input('Window drawdown limit $ (0 = off)', 0, 30000,
                             int(rules.get('ea_dd_limit') or 0), step=250)
        lookback = c5.number_input('Lookback window (trading days)', 21, 252,
                                   int(rules.get('lookback_days', 63)))
        warn = c6.slider('Warn at fraction of limit', 0.5, 0.95,
                         float(rules.get('warn_fraction', 0.8)), 0.05)
        c7, c8, c9 = st.columns(3)
        stale = c7.number_input('Inactive after N days without a trade', 0, 120,
                                int(rules.get('stale_days', 21)),
                                help='EAs quiet for longer than this are marked '
                                     '💤 instead of raising alarms — filters out '
                                     'strategies that were removed from the '
                                     'account long ago. 0 disables.')
        relative = c8.checkbox('Relative rules (per-EA baselines)',
                               bool(rules.get('relative_rules', True)),
                               help='Judges each EA against its OWN historical '
                                    'worst streak from the backtests: a 28%-win-rate '
                                    'robot that routinely has 8-loss runs and a '
                                    '97%-win-rate robot that never lost twice in a '
                                    'row get their own yardsticks. Baselines '
                                    'dominated by one large loss (news gap / '
                                    'backtest artifact) are flagged as such.')
        ratio = c9.slider('Trigger at x historical worst', 0.5, 2.0,
                          float(rules.get('streak_ratio_trigger', 1.0)), 0.1,
                          help='1.0 = trigger when the current streak equals the '
                               'EA\'s historical worst; warning tier scales with '
                               'the warn fraction.')
        c10, c11 = st.columns(2)
        cooldown = c10.number_input(
            'Cooling-off after benching (days)', 0, 120,
            int(rules.get('cooldown_days', 21)),
            help='Once you bench a robot (record it with the "Benched" button '
                 'on the Management tab), it is not eligible to return or to '
                 'be promoted as a swap-in for this many days — the same '
                 'cooldown the backtest engine uses. Stops the flip-flop of '
                 're-adding a robot the moment it has one good week. 21 = '
                 'roughly one month of trading days.')
        c11.number_input(
            'Live-vs-bench divergence: extra losing streak', 0, 15,
            int(rules.get('divergence_streak', 3)),
            key='rules_div_streak',
            help='A live copy whose losing streak exceeds its bench twin\'s by '
                 'this many days/trades is flagged as an account-level '
                 'problem (fills / VPS / set-file), separately from the '
                 'robot\'s form.')
        proxies = list_proxy_timelines(rules)
        cur_proxy = rules.get('proxy_timeline_dir') or ''
        cur_name = next((n for n, p in proxies.items()
                         if os.path.normcase(p) == os.path.normcase(cur_proxy)), None)
        opts = ['(baseline trailing window)'] + list(proxies)
        proxy_pick = st.selectbox(
            'Recent-form proxy timeline',
            opts,
            index=opts.index(cur_name) if cur_name in opts else 0,
            help='Which short-window real-tick compile ranks the swap-in '
                 'candidates on recent form. Lists every `proxy_*` dataset in '
                 '`engine_data\\timeline` — MT5Tools ships '
                 '`proxy_3m_realticks`; compile your own on the Benchmark tab '
                 '(weekly review) and it appears here. "(baseline trailing '
                 'window)" ranks on the last ~3 months of the long-history '
                 'baseline instead.')
        proxy_dir = proxies.get(proxy_pick, '') if proxy_pick in proxies else ''
        if proxy_pick in proxies:
            man = os.path.join(proxy_dir, 'manifest.json')
            gen = ''
            if os.path.isfile(man):
                try:
                    with open(man, encoding='utf-8') as f:
                        gen = json.load(f).get('dataset', {}).get('generated', '')[:10]
                except (ValueError, OSError):
                    pass
            st.caption(f'✅ `{proxy_dir}`' + (f' — compiled {gen}' if gen else ''))
        elif cur_proxy and not proxies:
            st.caption('❌ no proxy datasets found in engine_data\\timeline — '
                       'the ranking uses the baseline trailing window until '
                       'one is compiled (Benchmark tab).')
        base_dir = st.text_input(
            'Streak baseline timeline folder',
            rules.get('baseline_timeline_dir', ''),
            help='Source of each EA\'s historical-worst streak baselines '
                 'for the relative rules (and the recent-form fallback). '
                 'MT5Tools ships `engine_data\\timeline\\main_pool_2018` '
                 'ready-compiled (2018-2026, 140 robots) and uses it '
                 'automatically — only change this to use your own long '
                 'full-history compile; a 3-month window cannot define a '
                 'meaningful "historical worst".')
        if base_ok:
            st.caption('✅ found')
        else:
            st.caption('❌ not found — the bundled dataset lives at '
                       '`engine_data\\timeline\\main_pool_2018`; re-pull the '
                       'repo if it is missing. Until then relative rules and '
                       'swap-in candidates are unavailable. *(re-checked after '
                       'saving)*')
        if st.form_submit_button('💾 Save rules'):
            rules.update({
                'streak_mode'        : mode,
                'loss_streak_limit'  : int(streak) or None,
                'streak_dollar_limit': int(cost) or None,
                'ea_dd_limit'        : int(dd) or None,
                'lookback_days'      : int(lookback),
                'warn_fraction'      : float(warn),
                'stale_days'         : int(stale),
                'relative_rules'     : bool(relative),
                'streak_ratio_trigger': float(ratio),
                'streak_ratio_warn'  : round(float(ratio) * float(warn), 2),
                'cooldown_days'      : int(cooldown),
                'divergence_streak'  : int(st.session_state.get('rules_div_streak', 3)),
                'proxy_timeline_dir' : proxy_dir.strip(),
                'baseline_timeline_dir': base_dir.strip(),
            })
            save_rules_config(rules)
            st.success('Rules saved.')


def _render_management(rules):
    _render_setup_banner(rules, 'mgmt')
    # ── Status board ──────────────────────────────────────────────────────
    st.subheader('Rule check')
    st.caption('Rules (⚙️ tab) are checked against the benchmark accounts, then '
               'any tripped robot is matched to the live accounts running it.')
    # Only accounts still present in the FTP settings take part — cache files
    # of removed accounts would otherwise keep raising stale flags forever.
    cfg_accounts = load_account_configs()
    cfg_ids = ({a.get('account') for a in cfg_accounts} |
               {a.get('label', a.get('account', '')) for a in cfg_accounts})
    cached, orphans = [], []
    for d in get_all_cached():
        if d.get('account_folder') in cfg_ids or d.get('label') in cfg_ids:
            cached.append(d)
        else:
            orphans.append(d)
    if orphans:
        names = ', '.join(sorted(
            (d.get('label') or d.get('account_folder') or '?')
            for d in orphans))
        st.warning(f'🧹 {len(orphans)} cached account(s) are no longer in the '
                   f"FTP settings and are excluded from the rule check: "
                   f'{names}. Their old trades stay on disk until cleaned up.')
        if st.button('🧹 Delete their stale cache files'):
            removed = 0
            for d in orphans:
                p = CACHE_DIR / f"ftp_{d.get('account_folder')}.pkl"
                if p.exists():
                    p.unlink()
                    removed += 1
            st.success(f'{removed} stale cache file(s) removed.')
            st.rerun()
    if not cached:
        st.info("No cached account data. Refresh accounts on the "
                "**Live MT5 EA's** page first.")
        return

    rows = evaluate_all(cached, rules)
    if not rows:
        st.info('No EA trade history found in the cached accounts.')
        return

    # Reference accounts are the candidate bench — informational, not alarms
    refs_set  = set(rules.get('reference_accounts') or [])
    rows_ref  = [r for r in rows if r['account'] in refs_set]
    rows      = [r for r in rows if r['account'] not in refs_set]

    if refs_set and rows_ref:
        _render_bench_driven(cached, rules, rows, rows_ref)
        return

    st.info('No benchmark accounts configured — so the rules are checked '
            'directly on each live account instead. Dollar thresholds assume '
            'the standard \\$100k baseline size, which live accounts rarely '
            'match. Pick benchmark accounts on the 🧪 tab: the rules are then '
            'checked there (fair size for every robot) and tripped robots are '
            'matched to your live accounts.')
    trig  = [r for r in rows if r['level'] == 'triggered']
    warn  = [r for r in rows if r['level'] == 'warning']
    ok    = [r for r in rows if r['level'] == 'ok']
    idle  = [r for r in rows if r['level'] == 'inactive']
    c1, c2, c3, c4 = st.columns(4)
    c1.metric('🛑 Triggered', len(trig))
    c2.metric('⚠️ Approaching a limit', len(warn))
    c3.metric('✅ OK', len(ok))
    c4.metric('💤 Inactive', len(idle),
              help=f"No trades for over {rules.get('stale_days', 21)} days — "
                   'old streaks/drawdowns are not treated as alarms. Check '
                   'whether these EAs are still attached and enabled.')

    if not trig and not warn:
        st.success('All clear — no rules fired, nothing to decide today. '
                   'That is the system working, not the system being idle.')

    # Decision cards for triggered/warning EAs
    for r in trig + warn:
        with st.container(border=True):
            head = (f"{LEVEL_ICON[r['level']]} **{r['strategy']}** — "
                    f"{r['account']}")
            st.markdown(head)
            for t in r['triggers']:
                st.markdown(f"- 🛑 {t.replace('$', chr(92) + '$')}")
            for w in r['warnings']:
                st.markdown(f"- ⚠️ {w.replace('$', chr(92) + '$')}")
            st.caption(f"Window P&L \\${r['window_pnl']:,.0f} · "
                       f"streak {r['streak']} {r['streak_unit']} "
                       f"(\\${r['streak_cost']:,.0f}) · "
                       f"window DD \\${r['window_dd']:,.0f} · "
                       f"last trade {r['last_trade']}")

    with st.expander(f'Full status table ({len(rows)} EA/account rows)'):
        tbl = pd.DataFrame(rows)
        tbl['status'] = tbl['level'].map(LEVEL_ICON)
        show = tbl[['status', 'account', 'strategy', 'streak', 'streak_unit',
                    'baseline_streak', 'streak_cost', 'baseline_cost',
                    'window_dd', 'window_pnl', 'last_trade']]
        show = show.rename(columns={
            'status': ' ', 'account': 'Account', 'strategy': 'EA',
            'streak': 'Streak', 'streak_unit': 'Unit',
            'baseline_streak': 'Hist worst streak',
            'streak_cost': 'Streak cost ($)',
            'baseline_cost': 'Hist worst cost ($)',
            'window_dd': 'Window DD ($)',
            'window_pnl': 'Window P&L ($)', 'last_trade': 'Last trade'})
        st.dataframe(show, use_container_width=True, hide_index=True)

    # ── Reference bench monitor ───────────────────────────────────────────
    if rows_ref:
        r_trig = [r for r in rows_ref if r['level'] == 'triggered']
        with st.expander(f'🧪 Reference bench — {len(rows_ref)} EA rows across '
                         f'{len({r["account"] for r in rows_ref})} baseline '
                         f'account(s), {len(r_trig)} rule trigger(s)'):
            st.caption('These demo accounts run the full UBS pool to generate '
                       'live forward data. Rule triggers here are *information* '
                       'about candidates (a benched-worthy robot should rank '
                       'low), never decisions — nothing to action.')
            rtbl = pd.DataFrame(rows_ref)
            rtbl['status'] = rtbl['level'].map(LEVEL_ICON)
            rshow = rtbl[['status', 'account', 'strategy', 'streak',
                          'streak_cost', 'window_dd', 'window_pnl',
                          'last_trade']].rename(columns={
                'status': ' ', 'account': 'Account', 'strategy': 'EA',
                'streak': 'Streak', 'streak_cost': 'Streak cost ($)',
                'window_dd': 'Window DD ($)', 'window_pnl': 'Window P&L ($)',
                'last_trade': 'Last trade'})
            st.dataframe(rshow, use_container_width=True, hide_index=True)

    trig = [r for r in rows if r['level'] == 'triggered']
    warn = [r for r in rows if r['level'] == 'warning']
    _render_candidates(cached, rules, rows, trig + warn)


def _render_bench_driven(cached, rules, live_rows, bench_rows):
    """Bench-as-signal flow: rules on the bench, consequences on live accounts,
    size-free divergence check for live copies."""
    signals = bench_signals(cached, rules)
    lv = live_vs_bench(cached, signals, rules)
    lookback = int(rules.get('lookback_days', 63))

    # ── 1. Bench signals ──────────────────────────────────────────────────
    st.subheader('1 · Rules checked on the benchmark accounts')
    st.caption('Every robot in the pool is checked against the benching rules '
               'as it runs on the benchmark accounts — standard \\$100k / '
               'lot-step size, so thresholds mean the same thing for every '
               'robot. One status per robot, including robots not yet live. '
               'The most severe status wins if a robot sits on several bench '
               'accounts.')
    b_trig = {s: r for s, r in signals.items() if r['level'] == 'triggered'}
    b_warn = {s: r for s, r in signals.items() if r['level'] == 'warning'}
    b_ok   = [s for s, r in signals.items() if r['level'] == 'ok']
    b_idle = [s for s, r in signals.items() if r['level'] == 'inactive']
    c1, c2, c3, c4 = st.columns(4)
    c1.metric('🛑 Triggered', len(b_trig))
    c2.metric('⚠️ Approaching', len(b_warn))
    c3.metric('✅ OK', len(b_ok))
    c4.metric('💤 Inactive', len(b_idle))
    if not b_trig and not b_warn:
        st.success('All clear on the bench — no rules fired, nothing to '
                   'decide today.')

    # ── 2. Affected live accounts ─────────────────────────────────────────
    st.subheader('2 · Matched to live accounts — what to actually do today')
    st.caption('Each robot that tripped a rule on the bench, matched to the '
               'live accounts currently running it. This is the action list: '
               'bench the robot on these accounts (or overrule, and say why).')
    live_by_ea = {}
    for r in lv:
        live_by_ea.setdefault(r['strategy'], []).append(r)
    actions = []
    for s, sig in list(b_trig.items()) + list(b_warn.items()):
        accts = live_by_ea.get(s, [])
        if accts:
            actions.append((sig, accts))
    if not actions:
        st.success('No live account is running a robot that tripped a rule on '
                   'the bench — nothing to action.')
    blog = load_bench_log()
    for sig, accts in actions:
        with st.container(border=True):
            st.markdown(f"{LEVEL_ICON[sig['level']]} **{sig['strategy']}** — "
                        f"tripped on the bench · running live on: "
                        + ', '.join(f"**{a['account']}**" for a in accts))
            for t in sig['triggers']:
                st.markdown(f"- 🛑 {t.replace('$', chr(92) + '$')}")
            for w in sig['warnings']:
                st.markdown(f"- ⚠️ {w.replace('$', chr(92) + '$')}")
            st.caption(f"Bench window P&L \\${sig['window_pnl']:,.0f} · "
                       f"streak {sig['streak']} {sig['streak_unit']} "
                       f"(\\${sig['streak_cost']:,.0f}) · window DD "
                       f"\\${sig['window_dd']:,.0f} · last trade "
                       f"{sig['last_trade']}")
            for a in accts:
                st.caption(f"↳ {a['account']}: live streak {a['live_streak']} "
                           f"{a['streak_unit']}, live window P&L "
                           f"\\${a['live_window_pnl']:,.0f}"
                           + (' — **also diverging from bench**' if a['diverges'] else ''))
            in_cd, left, elig = cooldown_status(sig['strategy'], rules, blog)
            if in_cd:
                st.caption(f"🧊 Recorded as benched on {blog[sig['strategy']]['benched_on']} "
                           f"— cooling off, eligible to return {elig} "
                           f"({left} day(s)).")
            elif st.button(f"🪑 Mark {sig['strategy']} as benched today",
                           key=f"bench_{sig['strategy']}",
                           help='Records the benching so the cooling-off '
                                'period applies: the robot is held out of the '
                                'swap-in candidates and flagged if still '
                                'running until the cooldown ends. Does not '
                                'touch your MT5 terminals.'):
                blog[sig['strategy']] = {
                    'benched_on': str(pd.Timestamp.now().date()),
                    'accounts': [a['account'] for a in accts],
                    'reason': '; '.join(sig['triggers'] + sig['warnings'])}
                save_bench_log(blog)
                st.rerun()
    # inherited-OK summary
    ok_live = sum(1 for r in lv if r['on_bench'] and r['bench_level'] in ('ok', 'inactive'))
    st.caption(f'{ok_live} live EA/account row(s) match a robot that is OK on '
               'the bench.')

    # ── Benched register / cooling-off ────────────────────────────────────
    if blog:
        cd_days = int(rules.get('cooldown_days', 21))
        with st.expander(f'🧊 Benched robots — cooling off ({len(blog)}), '
                         f'{cd_days}-day cooldown'):
            st.caption('Robots you recorded as benched. During the cooling-off '
                       'they are held out of the swap-in candidates and, if a '
                       'live account is still running one, it is flagged '
                       'below. After the cooldown they become eligible again '
                       '— re-adding is your call, ideally only when the bench '
                       'shows the robot back in form.')
            live_running = {r['strategy'] for r in lv}
            rows_b = []
            for s, e in sorted(blog.items(), key=lambda kv: kv[1]['benched_on']):
                in_cd, left, elig = cooldown_status(s, rules, blog)
                rows_b.append({'EA': s, 'Benched on': e['benched_on'],
                               'Eligible again': elig,
                               'Status': (f'🧊 {left} day(s) left' if in_cd
                                          else '✅ cooldown over'),
                               'Still running live?': '⚠️ yes' if s in live_running else 'no',
                               'Reason': e.get('reason', '')[:80]})
            st.dataframe(pd.DataFrame(rows_b), use_container_width=True,
                         hide_index=True)
            still = [s for s in blog if s in live_running and cooldown_status(s, rules, blog)[0]]
            if still:
                st.warning('Still running on a live account during cooling-off: '
                           + ', '.join(f'**{s}**' for s in still) +
                           ' — remove them from the terminal, or clear the '
                           'record below if you decided to keep them.')
            rel = st.multiselect('Clear from the benched register',
                                 list(blog), key='bench_release')
            if rel and st.button('Clear selected', key='bench_release_btn'):
                for s in rel:
                    blog.pop(s, None)
                save_bench_log(blog)
                st.rerun()

    # ── 3. Live divergence ────────────────────────────────────────────────
    st.subheader('3 · Live copies behaving worse than the same robot on the bench')
    st.caption(f'Size-free check over the last {lookback} trading days: a live '
               'copy on a longer losing streak than its bench twin, or losing '
               'while the twin is winning, points at an ACCOUNT problem — '
               'fills, VPS latency, set-file load — not the robot\'s form. '
               'Different diagnosis, different fix.')
    div = [r for r in lv if r['diverges']]
    if not div:
        st.success('No live copy is materially behind its bench twin.')
    for r in div:
        with st.container(border=True):
            st.markdown(f"🔀 **{r['strategy']}** on **{r['account']}**")
            for m in r['divergence']:
                st.markdown(f"- {m.replace('$', chr(92) + '$')}")
    off_bench = [r for r in lv if not r['on_bench']]
    if off_bench:
        flagged = [r for r in off_bench if r['fallback_level'] in ('triggered', 'warning')]
        with st.expander(f"Live EAs not on the bench ({len(off_bench)}) — direct "
                         f"check, thresholds scaled to account balance "
                         f"({len(flagged)} flagged)"):
            st.caption('These robots have no bench twin (packaged EAs, other '
                       'developers, etc.), so the rules run directly on the '
                       'live account with dollar limits scaled by balance / '
                       '\\$100k — the weaker check; streak-length rules are '
                       'size-free and stay exact.')
            for r in flagged:
                st.markdown(f"{LEVEL_ICON[r['fallback_level']]} **{r['strategy']}** "
                            f"— {r['account']}: " +
                            '; '.join(t.replace('$', chr(92) + '$')
                                      for t in r['fallback_triggers']))
            tbl = pd.DataFrame(off_bench)[['account', 'strategy', 'live_streak',
                                           'live_window_pnl', 'fallback_level',
                                           'last_trade']].rename(columns={
                'account': 'Account', 'strategy': 'EA', 'live_streak': 'Streak',
                'live_window_pnl': 'Window P&L ($)', 'fallback_level': 'Status',
                'last_trade': 'Last trade'})
            st.dataframe(tbl, use_container_width=True, hide_index=True)

    with st.expander(f'Full bench table ({len(signals)} EAs)'):
        tbl = pd.DataFrame(list(signals.values()))
        tbl['status'] = tbl['level'].map(LEVEL_ICON)
        show = tbl[['status', 'strategy', 'account', 'streak', 'streak_unit',
                    'baseline_streak', 'streak_cost', 'baseline_cost',
                    'window_dd', 'window_pnl', 'last_trade']].rename(columns={
            'status': ' ', 'strategy': 'EA', 'account': 'Bench account',
            'streak': 'Streak', 'streak_unit': 'Unit',
            'baseline_streak': 'Hist worst streak',
            'streak_cost': 'Streak cost ($)', 'baseline_cost': 'Hist worst cost ($)',
            'window_dd': 'Window DD ($)', 'window_pnl': 'Window P&L ($)',
            'last_trade': 'Last trade'})
        st.dataframe(show, use_container_width=True, hide_index=True)

    flagged_live = [dict(r, level=r['bench_level'], triggers=r['bench_triggers'],
                         warnings=[], window_pnl=r['live_window_pnl'],
                         window_dd=0.0, streak=r['live_streak'],
                         streak_cost=0.0, baseline_streak=None,
                         baseline_cost=None)
                    for r in lv if r['bench_level'] in ('triggered', 'warning')]
    _render_candidates(cached, rules, live_rows, flagged_live)


def _render_candidates(cached, rules, rows, flagged):
    """Swap-in candidates (backtest proxy + bench forward stats) and the
    flagged-vs-candidates comparison."""
    trig = [r for r in flagged if r.get('level') == 'triggered']
    warn = [r for r in flagged if r.get('level') == 'warning']
    # ── Swap-in candidates (backtest proxy) ───────────────────────────────
    st.subheader('Swap-in candidates')
    st.caption('Ranked from the **backtest proxy** (compiled timeline, trailing '
               'window). The *Live fwd* columns fill in as the reference bench '
               'accounts accumulate trades — after ~3 months of forward data '
               'they become the primary evidence, and disagreement between the '
               'two ("backtested well, demo says otherwise") is exactly the '
               'survivorship correction to watch for. A shortlist, not an order.')
    proxy = load_proxy(rules)
    if proxy is None:
        st.warning('No recent-form data available — no proxy timeline and no '
                   'baseline timeline could be found. Set the baseline on the '
                   '⚙️ Benching rules tab (the engine repo ships it).')
        return
    src = rules.get('proxy_timeline_dir') or ''
    src_name = (os.path.basename(src.rstrip('\\/'))
                if src and os.path.isfile(os.path.join(src, 'daily_pnl.csv'))
                else 'baseline trailing window')
    st.caption(f'Recent-form source: **{src_name}** (change on the ⚙️ Benching '
               'rules tab).')

    # Exclude only EAs on LIVE accounts — the reference bench's EAs are
    # precisely the candidates, so they must stay eligible
    live_names = {r['strategy'] for r in rows}
    cands = proxy[~proxy['strategy'].isin(live_names)].copy()
    # Cooling-off: recently benched robots are held out until eligible
    blog = load_bench_log()
    cooling = [s for s in cands['strategy'] if cooldown_status(s, rules, blog)[0]]
    if cooling:
        cands = cands[~cands['strategy'].isin(cooling)]
        st.caption('🧊 Held out during cooling-off: ' + ', '.join(
            f"{s} (eligible {cooldown_status(s, rules, blog)[2]})" for s in cooling))

    # Live forward results from the reference bench, as they accumulate —
    # re-keyed via the name map so candidate strategies find their comments
    fwd = forward_stats_by_strategy(reference_forward_stats(cached, rules))
    if fwd:
        cands['live_days']   = cands['strategy'].map(
            lambda s: fwd.get(s, {}).get('live_days'))
        cands['live_pnl']    = cands['strategy'].map(
            lambda s: fwd.get(s, {}).get('live_pnl'))
        cands['live_sharpe'] = cands['strategy'].map(
            lambda s: fwd.get(s, {}).get('live_sharpe'))

    # Fill trust per robot (from the engine's real-tick check), plus the
    # too-good-to-verify marker for anything claiming >25% in one window.
    trust = _trust_by_strategy(rules)
    cands['flag'] = [
        (TRUST_ICON.get(trust.get(s, 'unknown'), '⚪')
         + ('🚩' if p > 25_000 else ''))
        for s, p in zip(cands['strategy'], cands['window_pnl'])]

    n_show = st.slider('Show top', 5, 40, 15)
    cols = ['flag', 'strategy', 'family', 'symbol', 'window_pnl', 'sharpe',
            'window_dd']
    extra = [c for c in ['hist_max_loss_streak', 'hist_max_streak_cost',
                         'largest_single_loss', 'live_days', 'live_pnl',
                         'live_sharpe'] if c in cands.columns]
    show = cands.head(n_show)[cols + extra]
    show = show.rename(columns={
        'flag': ' ',
        'strategy': 'EA', 'family': 'Family', 'symbol': 'Market',
        'window_pnl': '3m P&L ($)', 'sharpe': '3m Sharpe',
        'window_dd': '3m DD ($)',
        'hist_max_loss_streak': 'Worst streak (hist)',
        'hist_max_streak_cost': 'Worst streak cost ($, hist)',
        'largest_single_loss': 'Largest loss ($, hist)',
        'live_days': 'Live fwd days', 'live_pnl': 'Live fwd P&L ($)',
        'live_sharpe': 'Live fwd Sharpe'})
    st.dataframe(show, use_container_width=True, hide_index=True)
    if trust:
        st.caption(TRUST_LEGEND)
    if cands.head(n_show)['flag'].str.contains('🚩').any():
        st.caption('🚩 = window profit above \\$25k (25% of the account) on a '
                   'robot calibrated to a \\$5k max drawdown — backtest form '
                   'this extreme rarely survives live spreads and fills. Wait '
                   'for its **Live fwd** columns from the bench before '
                   'trusting it.')
    st.caption('Diversification reminder: this list ranks robots on recent '
               'form only — it does not know what your account already '
               'holds. Before swapping one in, check its Market column '
               'against the robots already on the account: if the top '
               'candidates are all gold (or all Bitcoin) and the account is '
               'already gold-heavy, taking "the best" one adds concentration, '
               'not diversification. In the simulations, always promoting the '
               'top-ranked robot without a per-market limit ended up with a '
               'team dominated by one market.')

    # ── Breach vs candidates comparison (proof of concept) ────────────────
    flagged = trig + warn
    if flagged:
        st.subheader('Compare a flagged EA against the candidates')
        opts = {f"{LEVEL_ICON[r['level']]} {r['strategy']} — {r['account']}": r
                for r in flagged}
        sel = st.selectbox('Flagged EA', ['(choose one)'] + list(opts))
        if sel != '(choose one)':
            r = opts[sel]
            balances = {a.get('label', a.get('account', '')): a.get('balance')
                        for a in load_account_configs()}
            bal = float(balances.get(r['account']) or 100_000)
            scale = bal / 100_000.0
            st.caption(f"Candidate 3-month figures are scaled to "
                       f"**{r['account']}**'s balance \\${bal:,.0f} "
                       f"(×{scale:.2f} of the \\$100k baseline), so the "
                       f"comparison is in this account's money. The flagged "
                       f"EA's row shows its actual results over the last "
                       f"{int(rules.get('lookback_days', 63))} trading days.")
            comp_rows = [{
                'EA'         : f"{LEVEL_ICON[r['level']]} {r['strategy']} (current)",
                'Market'     : '',
                '3m P&L ($)' : r['window_pnl'],
                '3m DD ($)'  : r['window_dd'],
                '3m Sharpe'  : None,
                'Worst streak (hist)': r.get('baseline_streak'),
                'Worst streak cost ($)': round(r['baseline_cost'] * scale, 2)
                                         if r.get('baseline_cost') else None,
                'Source'     : f"live — {r['account']}",
            }]
            any_artifact = False
            for ea_id, c in cands.head(n_show).iterrows():
                streak_hist = c.get('hist_max_loss_streak')
                cost_hist   = c.get('hist_max_streak_cost')
                # flag cost baselines dominated by one big loss (likely a
                # news-gap / 1m-OHLC artifact rather than a real streak)
                artifact = (pd.notna(cost_hist) and cost_hist > 0 and
                            pd.notna(c.get('largest_single_loss')) and
                            c['largest_single_loss'] >= 0.8 * cost_hist)
                any_artifact = any_artifact or artifact
                comp_rows.append({
                    'EA'         : ('🚩 ' if c.get('window_pnl', 0) > 25_000
                                    else '') + c['strategy'],
                    'Market'     : c['symbol'],
                    '3m P&L ($)' : round(c['window_pnl'] * scale, 2),
                    '3m DD ($)'  : round(c['window_dd'] * scale, 2),
                    '3m Sharpe'  : c['sharpe'],
                    'Worst streak (hist)': int(streak_hist)
                                           if pd.notna(streak_hist) else None,
                    'Worst streak cost ($)': (str(round(cost_hist * scale, 2))
                                              + (' *' if artifact else ''))
                                             if pd.notna(cost_hist) else None,
                    'Source'     : 'backtest proxy (scaled)',
                })
            st.dataframe(pd.DataFrame(comp_rows), use_container_width=True,
                         hide_index=True)
            cap = ('Historical worst streak/cost = each robot\'s full-history '
                   'baseline (scaled to this account) — what "normal bad" '
                   'looks like before you commit to it. ')
            if any_artifact:
                cap += ('Rows marked * have a cost baseline dominated by one '
                        'large loss — likely a news-gap or backtest artifact '
                        'rather than a true streak; judge those accordingly. ')
            cap += ('Proof of concept — flagged row is live data, candidates '
                    'are backtest proxy until the reference demo accounts '
                    'supply live history (phase 2).')
            st.caption(cap)


# ── Benchmark accounts tab ────────────────────────────────────────────────────

def _render_benchmark_config(rules):
    """Choose which Live-MT5-EA accounts form the benchmark (reference) bench
    and show each one's forward-history accumulation. Per-install config —
    nothing here is tied to any specific portfolio."""
    st.caption('Benchmark accounts are **demo accounts running your whole robot '
               'pool** at the standard baseline (\\$100k, lot step = '
               'HistMaxDD / 5%) — one per strategy bucket works well (e.g. '
               'FX / Gold / Indices / Crypto). They do three jobs: the '
               '**benching rules are checked here** (every robot, same size, '
               'so thresholds are fair) and any robot that trips a rule is '
               'matched to the live accounts running it; their per-robot '
               'forward results **feed the swap-in ranking**; and after ~3 '
               'months of history they **replace the backtest proxy** as the '
               'primary evidence.')

    accounts = load_account_configs()
    if not accounts:
        st.info("No accounts configured yet — add accounts on the **Live MT5 "
                "EA's** page first, then pick which of them form the "
                'benchmark bench here.')
        _render_proxy_builder(rules, has_bench=False)
        return
    labels = [a.get('label', a.get('account', '')) for a in accounts]
    current = [l for l in (rules.get('reference_accounts') or [])
               if l in labels]
    sel = st.multiselect(
        'Benchmark (reference) accounts', labels, default=current,
        help='Any account from your Live MT5 EA\'s configs can serve. The '
             'benching rules are checked on these accounts; their rule trips '
             'are matched to live accounts rather than raised as alarms on '
             'the bench itself, and their EAs stay eligible as swap-in '
             'candidates.')
    if st.button('💾 Save benchmark accounts'):
        rules['reference_accounts'] = sel
        save_rules_config(rules)
        st.success('Saved — the Management tab and the Live MT5 EA\'s page '
                   'now treat these accounts as the bench.')
        st.rerun()

    if not sel:
        st.info('No benchmark accounts selected — the swap-in ranking relies '
                'on the backtest proxy below.')
        _render_proxy_builder(rules, has_bench=False)
        return

    # ── Status board ──────────────────────────────────────────────────────
    st.subheader('Bench status')
    cached = {d.get('label', d.get('account_folder', '')): d
              for d in get_all_cached()}
    cfg_by_label = {a.get('label', a.get('account', '')): a for a in accounts}
    rows, off_baseline = [], []
    for l in sel:
        a = cfg_by_label.get(l, {})
        bal = a.get('balance')
        if bal is not None and float(bal) != 100_000:
            off_baseline.append(l)
        row = {'Account': l, 'Type': a.get('type', ''),
               'Balance ($)': bal}
        d = cached.get(l)
        df = d.get('df') if d else None
        if df is not None and not df.empty:
            t = df.dropna(subset=['close_time'])
            ct = pd.to_datetime(t['close_time'])
            row.update({
                'Status'      : '✅ cached',
                'EAs seen'    : t['strategy'].nunique(),
                'Trades'      : len(t),
                'First trade' : str(ct.min().date()),
                'Last trade'  : str(ct.max().date()),
                'Forward days': int(ct.dt.normalize().nunique()),
                'Refreshed'   : (d.get('fetched_at', '') or '')[:16]
                                .replace('T', ' '),
            })
        else:
            row['Status'] = "❌ no cached data — refresh on Live MT5 EA's"
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), use_container_width=True,
                 hide_index=True)
    if off_baseline:
        st.warning('⚖️ ' + ', '.join(f'**{l}**' for l in off_baseline) +
                   ' — balance is not \\$100,000. Benchmark accounts are '
                   'measurement instruments: keep them on the \\$100k linear '
                   'baseline (no compounding) so their forward stats stay '
                   'comparable with the backtest timelines.')

    # ── Forward-evidence progress ─────────────────────────────────────────
    fwd = forward_stats_by_strategy(
        reference_forward_stats(list(cached.values()), rules))
    if fwd:
        lookback = int(rules.get('proxy_lookback_days', 63))
        days = sorted((v.get('live_days') or 0) for v in fwd.values())
        ready = sum(1 for x in days if x >= lookback)
        median = days[len(days) // 2] if days else 0
        st.subheader('Forward evidence accumulating')
        st.progress(min(1.0, median / lookback),
                    text=f'Median strategy: {median} of {lookback} live '
                         'trading days (the proxy-replacement threshold)')
        st.caption(f'**{len(fwd)} strategies** tracked across the bench; '
                   f'**{ready}** already have ≥{lookback} live trading days. '
                   'Once most candidates cross the threshold, the swap-in '
                   'ranking\'s "Live fwd" columns carry more weight than the '
                   'backtest proxy.')

    _render_proxy_builder(rules, has_bench=True)


def _render_proxy_builder(rules, has_bench):
    """Recent-form proxy: fresh 3-month REALTICKS batch backtest of the pool.
    Without benchmark accounts it IS the recent-form source (weekly review);
    with them it is the stand-in until ~3 months of forward data exist."""
    st.subheader('No benchmark accounts? Build recent form from a fresh backtest'
                 if not has_bench else
                 'Recent-form proxy — the backtest stand-in for the bench')
    if not has_bench:
        st.markdown("""
Without benchmark (demo) accounts there is no live forward data to rank
swap-in candidates on. The stand-in is a **fresh short-window backtest of
the whole candidate pool** — refresh it as part of your **weekly review**:

1. On the **Batch Backtest** page, bulk-backtest your UBS set files over the
   **last 3 months**, model **4 — REALTICKS** (every tick based on real
   ticks — this matters: 1m-OHLC and *generated* every-tick both flatter
   scalpers and breakout robots badly), standard \\$100k / lot-step setup.
2. Point the box below at the folder holding the exported reports — it is
   searched **recursively**, and subfolder names become the families.
3. Compile. The proxy is built inside the engine and the benching rules are
   pointed at it automatically; the swap-in ranking uses it immediately.

Compiling under the same name each week updates the proxy in place, so
"recent form" always means the last three months of honest fills.
""")
    else:
        st.markdown("""
Your benchmark accounts supply live forward data as it accumulates; until
they have ~3 months of history, a **fresh 3-month REALTICKS backtest** of the
candidate pool is the stand-in for recent form (and stays useful afterwards
as a cross-check — "backtested well, bench says otherwise" is the
survivorship signal to watch). Refresh it as part of a weekly review: batch
backtest the set files (model **4 — REALTICKS**, last 3 months, \\$100k /
lot-step), point the box at the reports folder, compile.
""")
    # Freshness of the current proxy — the weekly-review prompt
    cur = rules.get('proxy_timeline_dir') or ''
    man = os.path.join(cur, 'manifest.json') if cur else ''
    if os.path.isfile(man):
        try:
            with open(man, encoding='utf-8') as f:
                gen = json.load(f).get('dataset', {}).get('generated', '')
            age = (pd.Timestamp.now() - pd.Timestamp(gen)).days if gen else None
            if age is not None:
                (st.warning if age > 14 else st.caption)(
                    f'Current proxy `{os.path.basename(cur)}` was compiled '
                    f'**{age} day(s) ago** ({gen[:10]}).' +
                    (' Older than two weeks — refresh it as part of this '
                     'week\'s review.' if age > 14 else ''))
        except (ValueError, OSError):
            pass
    elif not cur:
        st.caption('No recent-form proxy compiled yet — the ranking is '
                   'using the baseline timeline\'s trailing window.')
    c1, c2 = st.columns([3, 1])
    rep_dir = c1.text_input(
        'Backtest reports folder', key='proxy_reports_dir',
        help='Folder with the .htm reports exported by the batch '
             'backtest — scanned recursively, duplicate filenames '
             'de-duplicated.')
    tl_name = c2.text_input('Proxy name', 'proxy_3m_realticks',
                            key='proxy_tl_name',
                            help='Dataset name (a folder inside '
                                 'engine_data\\timeline). Reusing a name each '
                                 'week updates it in place; the name must '
                                 'start with proxy_ to appear in the dropdown.')
    n_htm = (len(glob.glob(os.path.join(rep_dir, '**', '*.htm'),
                           recursive=True) +
                 glob.glob(os.path.join(rep_dir, '**', '*.html'),
                           recursive=True))
             if rep_dir and os.path.isdir(rep_dir) else 0)
    if rep_dir:
        st.caption(f'✅ {n_htm} report(s) found' if n_htm else
                   '❌ no .htm/.html reports found in that folder')
    if st.button('⚙️ Compile proxy & point the rules at it',
                 disabled=not (n_htm and tl_name.strip())):
        name = tl_name.strip()
        if not name.startswith('proxy_'):
            name = 'proxy_' + name
        out_dir = os.path.join(DATA_ROOT, name)
        log_buf = io.StringIO()
        ok = True
        try:
            with st.spinner(f'Compiling {n_htm} report(s) into {name}…'):
                if LIB_DIR not in sys.path:
                    sys.path.insert(0, LIB_DIR)
                import compile_timeline as _ct
                with contextlib.redirect_stdout(log_buf):
                    _ct.compile_reports(rep_dir, out_dir)
                # Refresh the fill-trust badges on the baseline against the
                # new real-tick proxy (bundled fill_trust.py, pointed at
                # engine_data)
                try:
                    import fill_trust as _ft
                    _ft.ENGINE_DIR = os.path.dirname(DATA_ROOT)
                    base_name = os.path.basename(
                        (rules.get('baseline_timeline_dir') or '').rstrip('\\/'))
                    if base_name and os.path.isdir(os.path.join(DATA_ROOT, base_name)):
                        mm, _ = _ft.compute_trust(base_name, name)
                        mm.to_csv(os.path.join(DATA_ROOT, base_name, 'ea_meta.csv'),
                                  index=False)
                        log_buf.write(f'\nfill-trust refreshed on {base_name}\n')
                except Exception as e:  # trust refresh is best-effort
                    log_buf.write(f'\n(fill-trust refresh skipped: {e})\n')
        except SystemExit:
            ok = False
        except Exception as e:
            ok = False
            log_buf.write(f'\nERROR: {e}\n')
        with st.expander('Compiler log', expanded=not ok):
            st.code(log_buf.getvalue() or '(no output)')
        if ok:
            rules['proxy_timeline_dir'] = out_dir
            save_rules_config(rules)
            st.success(f'Proxy **{name}** compiled into engine_data and the '
                       'benching rules now use it for recent form. Reload to '
                       'see it in the dropdown and the candidates.')
        else:
            st.error('Compile failed — see the log above.')
