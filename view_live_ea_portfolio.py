"""
view_live_ea_portfolio.py
=========================
Live UBS EA Management page — the decision layer on top of the live
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
import re
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
                        list_proxy_timelines, proxy_daily, DATA_ROOT, LIB_DIR,
                        load_name_map)
from view_live_mt5_eas import get_all_cached, load_account_configs, CACHE_DIR

LEVEL_ICON = {'triggered': '🛑', 'warning': '⚠️', 'ok': '✅', 'inactive': '💤'}

# Fill-trust badges (computed by the engine's fill_trust.py into ea_meta.csv)
TRUST_ICON = {'real': '✅', 'high': '🟢', 'medium': '🟡', 'low': '🔴',
              'unknown': '⚪', 'mixed': '◔'}
TRUST_LEGEND = ('Backtest quality — how much the backtest fills can be believed, '
                'from a real-tick check of the last 3 months: ✅ real-tick '
                'report · 🟢 high (real ticks keep ≥85% of the OHLC profit) · '
                '🟡 medium (50–85%) · 🔴 low (<50%, or profit turns to loss on '
                'real ticks) · ⚪ not checked. 🏅 = validated live: at least 3 '
                'months of forward history on the benchmark accounts — the '
                'strongest evidence there is. Low-trust robots\' backtest '
                'numbers are fill artifacts as much as edge.')


def _configured_cache():
    """(cached, orphans): cached account dicts still present in the FTP
    settings, and stale cache files of accounts since removed."""
    cfg_accounts = load_account_configs()
    cfg_ids = ({a.get('account') for a in cfg_accounts} |
               {a.get('label', a.get('account', '')) for a in cfg_accounts})
    cached, orphans = [], []
    for d in get_all_cached():
        if d.get('account_folder') in cfg_ids or d.get('label') in cfg_ids:
            cached.append(d)
        else:
            orphans.append(d)
    return cached, orphans


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
    st.title("🎛 Live UBS EA Management")
    st.caption("The rules-based review layer. Your written benching rules are "
               "checked against the **benchmark accounts** — demo accounts "
               "running the whole robot pool at the standard size, where the "
               "rule thresholds mean exactly what they say — and any robot that "
               "trips a rule there is then matched to the **live accounts** "
               "running it. No triggers on the bench = no decisions to make "
               "today. Data comes from the same FTP cache as the Live MT5 EA's "
               "page (refresh there).")

    rules = load_rules_config()

    tab_mgmt, tab_robots, tab_rules, tab_bench, tab_map = st.tabs(
        ['🎛 Management', '📋 EA Recent Performance', '⚙️ Benching rules',
         '🧪 Benchmark accounts', '🔗 Name mapping'])
    with tab_mgmt:
        _render_management(rules)
    with tab_robots:
        _render_robot_table(rules)
    with tab_rules:
        _render_rules_tab(rules)
    with tab_bench:
        _render_benchmark_config(rules)
    with tab_map:
        _render_mapping_tab(rules)
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
    cached, orphans = _configured_cache()
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
    """Swap-in candidates for a chosen live account: filters, correlation
    against that account's book, and a PROPOSED SWAP built the way the
    simulator's rules regime builds one — best recent form, subject to the
    correlation cap and a per-market cap, cooling-off respected."""
    st.subheader('Swap-in candidates')
    proxy = load_proxy(rules)
    if proxy is None:
        st.warning('No recent-form data available — no proxy dataset and no '
                   'baseline dataset could be found. Check the ⚙️ Benching '
                   'rules tab (MT5Tools ships both in engine_data).')
        return
    src = rules.get('proxy_timeline_dir') or ''
    src_name = (os.path.basename(src.rstrip('\\/'))
                if src and os.path.isfile(os.path.join(src, 'daily_pnl.csv'))
                else 'baseline trailing window')

    # ── Which live account are we swapping on? ────────────────────────────
    live_accts = sorted({r['account'] for r in rows})
    by_acct = {}
    for r in rows:
        by_acct.setdefault(r['account'], set()).add(r['strategy'])
    acct = st.selectbox(
        'Live account', ['(all live accounts — shortlist only)'] + live_accts,
        key='cand_acct',
        help='Pick the account you are reviewing: candidates already on it '
             'drop out, correlations are measured against its current book, '
             'and a proposed swap is built for its tripped robots.')
    scoped = acct in live_accts
    st.caption(f'Ranked on recent form from **{src_name}** (change on the ⚙️ '
               'Benching rules tab). The *Live fwd* columns fill in as the '
               'benchmark accounts accumulate trades — "backtested well, '
               'bench says otherwise" is the survivorship signal to watch. '
               'A shortlist, not an order.')

    # Candidates: robots not on THIS account (or, unscoped, not on any live
    # account). Bench-account robots are precisely the candidates.
    exclude = by_acct.get(acct, set()) if scoped else {r['strategy'] for r in rows}
    cands = proxy[~proxy['strategy'].isin(exclude)].copy()
    blog = load_bench_log()
    cooling = [s for s in cands['strategy'] if cooldown_status(s, rules, blog)[0]]
    if cooling:
        cands = cands[~cands['strategy'].isin(cooling)]
        st.caption('🧊 Held out during cooling-off: ' + ', '.join(
            f"{s} (eligible {cooldown_status(s, rules, blog)[2]})" for s in cooling))

    fwd = forward_stats_by_strategy(reference_forward_stats(cached, rules))
    if fwd:
        for k in ('live_days', 'live_pnl', 'live_sharpe'):
            cands[k] = cands['strategy'].map(lambda s, k=k: fwd.get(s, {}).get(k))
    trust = _trust_by_strategy(rules)
    live_ok = int(rules.get('proxy_lookback_days', 63))
    cands['flag'] = [
        (TRUST_ICON.get(trust.get(s, 'unknown'), '⚪')
         + ('🏅' if pd.notna(d) and d >= live_ok else '')
         + ('🚩' if p > 25_000 else ''))
        for s, p, d in zip(cands['strategy'], cands['window_pnl'],
                           cands['live_days'] if 'live_days' in cands.columns
                           else [np.nan] * len(cands))]

    # ── Filters ───────────────────────────────────────────────────────────
    f1, f2, f3, f4 = st.columns([2, 2, 3, 1])
    fam_pick = f1.multiselect('Family', sorted(cands['family'].dropna().unique()),
                              key='cand_fam', help='Empty = all families.')
    mkt_pick = f2.multiselect('Market', sorted(cands['symbol'].dropna().unique()),
                              key='cand_mkt', help='Empty = all markets.')
    # Strategy list cascades from the family / market filters
    _scope = cands
    if fam_pick:
        _scope = _scope[_scope['family'].isin(fam_pick)]
    if mkt_pick:
        _scope = _scope[_scope['symbol'].isin(mkt_pick)]
    strat_opts = sorted(_scope['strategy'].dropna().unique())
    strat_pick = f3.multiselect(
        'Strategy', strat_opts, key='cand_strat_pick',
        help='Strategies within the selected family / market. Empty = all.')
    strat_pick = [s for s in strat_pick if s in strat_opts]
    q_pick = f4.multiselect('Quality', ['🏅', '✅', '🟢', '🟡', '🔴', '⚪'],
                            key='cand_quality',
                            help='Backtest-quality badge, e.g. ✅🟢 only. 🏅 = '
                                 'validated live (≥3 months of forward history '
                                 'on the benchmark accounts).')
    h1, h2 = st.columns(2)
    hide_low = h1.checkbox(
        'Hide low-quality backtests (🔴)', True, key='cand_hide_low',
        help='Robots whose family lost more than half its 1m-OHLC profit on '
             'real ticks (or went from profit to loss). Their recent-form '
             'numbers are fill artifacts as much as edge — the bench will '
             'tell you if they are real; the backtest cannot.')
    hide_scalp = h2.checkbox(
        'Hide scalpers', True, key='cand_hide_scalp',
        help='Scalper families (Gold Scalp, Advanced Scalper, Bitcoin Scalp '
             'Pro). Fast, fill-sensitive strategies whose backtests are the '
             'least transferable to live spreads — even after real-tick '
             'reruns. Untick to see them.')
    total = len(cands)
    if hide_low:
        cands = cands[cands['flag'].str[0] != '🔴']
    if hide_scalp:
        cands = cands[~cands['family'].isin(SCALPER_FAMILIES) &
                      ~cands['strategy'].str.contains('scalp', case=False, na=False)]
    if fam_pick:
        cands = cands[cands['family'].isin(fam_pick)]
    if mkt_pick:
        cands = cands[cands['symbol'].isin(mkt_pick)]
    if strat_pick:
        cands = cands[cands['strategy'].isin(strat_pick)]
    if q_pick:
        want_live = '🏅' in q_pick
        badges = [q for q in q_pick if q != '🏅']
        m = pd.Series(True, index=cands.index)
        if badges:
            m &= cands['flag'].str[0].isin(badges)
        if want_live:
            m &= cands['flag'].str.contains('🏅')
        cands = cands[m]
    if len(cands) < total:
        st.caption(f'{len(cands)} of {total} candidates match the filters.')
    if cands.empty:
        st.info('No candidates match — loosen the filters.')
        return

    # ── Account book: correlation + market mix ────────────────────────────
    corr_cap = 1.0
    sym_cap = 0
    team_syms = {}
    if scoped:
        g1, g2 = st.columns(2)
        corr_cap = g1.slider(
            'Correlation cap vs this account\'s book', 0.3, 1.0,
            float(rules.get('swap_corr_cap', 0.7)), 0.05, key='cand_corr_cap',
            help='Candidates whose daily P&L (recent-form window) correlates '
                 'with the account\'s combined book above this are hidden and '
                 'never proposed. 1.0 = off. The simulator\'s validated '
                 'default is 0.7.')
        sym_cap = g2.number_input(
            'Max robots per market on this account', 0, 10,
            int(rules.get('swap_max_per_symbol', 5)), key='cand_sym_cap',
            help='Diversification cap for the proposal: a candidate is not '
                 'proposed if the account already holds this many robots on '
                 'its market. 0 = off. This is the rule that stopped "best '
                 'available" from rebuilding a one-market team in the sim.')
        pdaily, s2e = proxy_daily(rules)
        team = sorted(by_acct.get(acct, set()))
        sym_of = dict(zip(proxy['strategy'], proxy['symbol']))
        for s in team:
            if s in sym_of:
                team_syms[s] = sym_of[s]
        if pdaily is not None:
            team_ids = [s2e[s] for s in team if s in s2e and s2e[s] in pdaily.columns]
            if team_ids:
                book = pdaily[team_ids].sum(axis=1)
                cb, cmax, cwho = [], [], []
                for s in cands['strategy']:
                    e = s2e.get(s)
                    if e is None or e not in pdaily.columns:
                        cb.append(np.nan); cmax.append(np.nan); cwho.append('')
                        continue
                    x = pdaily[e]
                    cb.append(x.corr(book) if x.std() > 0 and book.std() > 0 else np.nan)
                    per = pdaily[team_ids].corrwith(x)
                    if per.notna().any():
                        cmax.append(float(per.max()))
                        who = per.idxmax()
                        cwho.append(next((k for k, v in s2e.items() if v == who), who))
                    else:
                        cmax.append(np.nan); cwho.append('')
                cands = cands.copy()
                cands['corr_book'] = np.round(cb, 2)
                cands['corr_max'] = np.round(cmax, 2)
                cands['corr_who'] = cwho
                if corr_cap < 1.0:
                    before = len(cands)
                    cands = cands[~(cands['corr_book'] > corr_cap)]
                    if len(cands) < before:
                        st.caption(f'{before - len(cands)} candidate(s) hidden: '
                                   f'correlation with the book above {corr_cap:.2f}.')
                st.caption(f'Correlations vs **{acct}** — {len(team_ids)} of its '
                           f'{len(team)} robots are in the recent-form dataset '
                           f'({len(pdaily)} trading days).')
            else:
                st.caption(f'None of **{acct}**\'s robots are in the recent-form '
                           'dataset — correlation not available for it.')
        if cands.empty:
            st.info('Every candidate is filtered out for this account — raise '
                    'the correlation cap or loosen the filters.')
            return

    # ── Proposed swap for this account ────────────────────────────────────
    if scoped:
        st.markdown('#### Proposed swap')
        acct_flags = [f for f in flagged if f['account'] == acct]
        trig = [f for f in acct_flags if f.get('level') == 'triggered']
        warn = [f for f in acct_flags if f.get('level') == 'warning']
        extra = st.number_input(
            'Extra open slots to fill on this account', 0, 10, 0,
            key='cand_extra_slots',
            help='Vacancies beyond the tripped robots — e.g. you are growing '
                 'the account, or a robot was removed for another reason.')
        n_vac = len(trig) + int(extra)
        if not n_vac:
            st.success(f'No swap proposed for **{acct}** — nothing tripped a '
                       'rule on the bench for its robots' +
                       (f' ({len(warn)} approaching a limit — watch, don\'t '
                        'act)' if warn else '') + '. Add open slots above to '
                       'get a pure addition proposal.')
        else:
            # Greedy, like Rules.review(): best recent form first, skip
            # anything breaching the market cap given the REMAINING team.
            remaining = [s for s in team if s not in {t['strategy'] for t in trig}]
            per_sym = {}
            for s in remaining:
                if s in team_syms:
                    per_sym[team_syms[s]] = per_sym.get(team_syms[s], 0) + 1
            picks, rejects = [], []
            for _, c in cands.iterrows():
                if len(picks) >= n_vac:
                    break
                sym = c['symbol']
                if sym_cap and per_sym.get(sym, 0) >= sym_cap:
                    rejects.append((c['strategy'], f'market cap: already {per_sym[sym]} on {sym}'))
                    continue
                if pd.notna(c.get('corr_book', np.nan)) and corr_cap < 1.0 \
                        and c['corr_book'] > corr_cap:
                    rejects.append((c['strategy'], f'corr {c["corr_book"]:.2f} > cap'))
                    continue
                picks.append(c)
                per_sym[sym] = per_sym.get(sym, 0) + 1
            prop_rows = []
            for i in range(n_vac):
                out = trig[i] if i < len(trig) else None
                inn = picks[i] if i < len(picks) else None
                row = {'#': i + 1}
                if out is not None:
                    row['Bench'] = f"🛑 {out['strategy']}"
                    row['Why (bench rule)'] = '; '.join(out.get('triggers') or [])
                else:
                    row['Bench'] = '➕ open slot'
                    row['Why (bench rule)'] = ''
                if inn is not None:
                    row['Add'] = f"{inn['flag']} {inn['strategy']}"
                    row['Family'] = inn['family']
                    row['Market'] = inn['symbol']
                    row['3m Sharpe'] = round(float(inn['sharpe']), 2)
                    row['3m P&L ($)'] = round(float(inn['window_pnl']))
                    row['3m DD ($)'] = round(float(inn['window_dd']))
                    row['Corr vs book'] = (round(float(inn['corr_book']), 2)
                                           if pd.notna(inn.get('corr_book', np.nan))
                                           else None)
                    row['Most similar on account'] = (
                        f"{inn['corr_who']} ({inn['corr_max']:.2f})"
                        if inn.get('corr_who') else '')
                    row['Bench fwd'] = (f"{int(inn['live_days'])}d, "
                                        f"${inn['live_pnl']:,.0f}"
                                        if pd.notna(inn.get('live_days', np.nan))
                                        else '')
                else:
                    row['Add'] = '— no eligible candidate (loosen caps/filters)'
                prop_rows.append(row)
            st.dataframe(pd.DataFrame(prop_rows), use_container_width=True,
                         hide_index=True)
            if rejects:
                with st.expander(f'{len(rejects)} higher-ranked candidate(s) '
                                 'passed over by the caps'):
                    for s, why in rejects:
                        st.caption(f'• {s} — {why}')
            st.caption('A proposal, not an order: the rules chose the '
                       'shortlist; the final pick is yours. Once you act, '
                       'press **Mark as benched** on the Management card so '
                       'the cooling-off applies.')

    # ── Table ─────────────────────────────────────────────────────────────
    n_show = st.slider('Show top', 5, 40, 15, key='cand_n_show')
    cols = ['flag', 'strategy', 'family', 'symbol', 'window_pnl', 'sharpe',
            'window_dd']
    extra_cols = [c for c in ['corr_book', 'corr_max', 'corr_who',
                              'hist_max_loss_streak', 'hist_max_streak_cost',
                              'largest_single_loss', 'live_days', 'live_pnl',
                              'live_sharpe'] if c in cands.columns]
    show = cands.head(n_show)[cols + extra_cols].rename(columns={
        'flag': 'Backtest quality',
        'strategy': 'EA', 'family': 'Family', 'symbol': 'Market',
        'window_pnl': '3m P&L ($)', 'sharpe': '3m Sharpe',
        'window_dd': '3m DD ($)',
        'corr_book': 'Corr vs book', 'corr_max': 'Max corr (robot)',
        'corr_who': 'Most similar robot on account',
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
    if not scoped:
        st.caption('Pick a live account above to measure correlation against '
                   'its book and get a proposed swap. Without one, remember the '
                   'list ranks on recent form only — it does not know what an '
                   'account already holds; always promoting the top-ranked '
                   'robot without a per-market limit ended up with a one-market '
                   'team in the simulations.')

    # ── Flagged EA vs candidates comparison ───────────────────────────────
    comp_pool = [f for f in flagged if (not scoped or f['account'] == acct)]
    if comp_pool:
        st.subheader('Compare a flagged EA against the candidates')
        opts = {f"{LEVEL_ICON[r['level']]} {r['strategy']} — {r['account']}": r
                for r in comp_pool}
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
                artifact = (pd.notna(cost_hist) and cost_hist > 0 and
                            pd.notna(c.get('largest_single_loss')) and
                            c['largest_single_loss'] >= 0.8 * cost_hist)
                any_artifact = any_artifact or artifact
                comp_rows.append({
                    'EA'         : f"{c['flag']} {c['strategy']}",
                    'Market'     : c['symbol'],
                    '3m P&L ($)' : round(c['window_pnl'] * scale, 2),
                    '3m DD ($)'  : round(c['window_dd'] * scale, 2),
                    '3m Sharpe'  : c['sharpe'],
                    'Worst streak (hist)': int(streak_hist)
                                           if pd.notna(streak_hist) else None,
                    'Worst streak cost ($)': (str(round(cost_hist * scale, 2))
                                              + (' *' if artifact else ''))
                                             if pd.notna(cost_hist) else None,
                    'Source'     : 'recent-form proxy (scaled)',
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
            cap += ('Flagged row is live data; candidates are the recent-form '
                    'proxy until the benchmark accounts supply live history.')
            st.caption(cap)



# ── EA Recent Performance tab ─────────────────────────────────────────────────

SCALPER_FAMILIES = {'Gold Scalp', 'Advanced Scalper', 'Bitcoin Scalp Pro'}


def _render_robot_table(rules):
    """Every robot in the pool: recent form (proxy window), backtest quality,
    bench status, live forward stats, which live accounts run it, cooling-off.
    Same filters as the swap-in candidates."""
    st.caption('The whole robot pool at a glance — the last 3 months on '
               'real ticks (recent-form dataset), backtest quality, what the '
               'bench says, live forward evidence as it accumulates, and where '
               'each robot is running live. Same filters as the swap-in '
               'candidates.')
    bdir = rules.get('baseline_timeline_dir') or ''
    meta_p = os.path.join(bdir, 'ea_meta.csv')
    if not os.path.isfile(meta_p):
        st.warning('Baseline dataset not found — check the ⚙️ Benching rules tab.')
        return
    meta = pd.read_csv(meta_p)
    proxy = load_proxy(rules)
    lookback = int(rules.get('proxy_lookback_days', 63))

    # Base table: every robot in the baseline, left-joined with recent form
    t = meta[['strategy', 'family', 'symbol', 'timeframe']].copy()
    t['fill_trust'] = meta['fill_trust'] if 'fill_trust' in meta.columns else 'unknown'
    for c in ('hist_max_loss_streak', 'hist_max_streak_cost', 'net_profit',
              'realized_dd_pct'):
        t[c] = meta[c] if c in meta.columns else np.nan
    if proxy is not None:
        px = proxy.reset_index(drop=True)[['strategy', 'window_pnl', 'sharpe',
                                            'window_dd']]
        t = t.merge(px, on='strategy', how='left')
    else:
        t['window_pnl'] = np.nan; t['sharpe'] = np.nan; t['window_dd'] = np.nan

    # Bench status + live forward + live accounts + cooling-off
    cached, _ = _configured_cache()
    signals = bench_signals(cached, rules) if rules.get('reference_accounts') else {}
    fwd = forward_stats_by_strategy(reference_forward_stats(cached, rules)) if cached else {}
    live_rows = evaluate_all(cached, rules) if cached else []
    refs = set(rules.get('reference_accounts') or [])
    live_on = {}
    for r in live_rows:
        if r['account'] not in refs:
            live_on.setdefault(r['strategy'], []).append(r['account'])
    blog = load_bench_log()

    def bench_str(s):
        sig = signals.get(s)
        if not sig:
            return ''
        return (f"{LEVEL_ICON[sig['level']]} {sig['level']}"
                + (f" · streak {sig['streak']} {sig['streak_unit']}"
                   if sig['level'] in ('triggered', 'warning') else ''))

    t['bench'] = t['strategy'].map(bench_str)
    t['live_days'] = t['strategy'].map(lambda s: fwd.get(s, {}).get('live_days'))
    t['live_pnl'] = t['strategy'].map(lambda s: fwd.get(s, {}).get('live_pnl'))
    t['live_sharpe'] = t['strategy'].map(lambda s: fwd.get(s, {}).get('live_sharpe'))
    t['live_accounts'] = t['strategy'].map(lambda s: ', '.join(live_on.get(s, [])))
    t['cooling'] = t['strategy'].map(
        lambda s: (lambda cd: f'🧊 {cd[1]}d (until {cd[2]})' if cd[0] else '')(
            cooldown_status(s, rules, blog)))
    t['flag'] = [
        (TRUST_ICON.get(ft, '⚪')
         + ('🏅' if pd.notna(d) and d >= lookback else '')
         + ('🚩' if pd.notna(p) and p > 25_000 else ''))
        for ft, d, p in zip(t['fill_trust'], t['live_days'], t['window_pnl'])]
    t['data_status'] = [
        ('real-tick' if ft == 'real' else '1m OHLC') +
        ('' if pd.notna(p) else ' · no trades in 3m window')
        for ft, p in zip(t['fill_trust'], t['window_pnl'])]

    # ── Filters (same as candidates) ──────────────────────────────────────
    h1, h2 = st.columns(2)
    hide_low = h1.checkbox('Hide low-quality backtests (🔴)', False, key='rt_hide_low')
    hide_scalp = h2.checkbox('Hide scalpers', False, key='rt_hide_scalp')
    f1, f2, f3, f4 = st.columns([2, 2, 3, 1])
    fam_pick = f1.multiselect('Family', sorted(t['family'].dropna().unique()),
                              key='rt_fam')
    mkt_pick = f2.multiselect('Market', sorted(t['symbol'].dropna().unique()),
                              key='rt_mkt')
    _scope = t
    if fam_pick:
        _scope = _scope[_scope['family'].isin(fam_pick)]
    if mkt_pick:
        _scope = _scope[_scope['symbol'].isin(mkt_pick)]
    strat_opts = sorted(_scope['strategy'].dropna().unique())
    strat_pick = [s for s in f3.multiselect('Strategy', strat_opts, key='rt_strat')
                  if s in strat_opts]
    q_pick = f4.multiselect('Quality', ['🏅', '✅', '🟢', '🟡', '🔴', '⚪'],
                            key='rt_quality')
    x1, x2 = st.columns(2)
    only_live = x1.checkbox('Only robots running live', False, key='rt_only_live')
    only_flag = x2.checkbox('Only bench-flagged (🛑/⚠️)', False, key='rt_only_flag')

    total = len(t)
    if hide_low:
        t = t[t['flag'].str[0] != '🔴']
    if hide_scalp:
        t = t[~t['family'].isin(SCALPER_FAMILIES) &
              ~t['strategy'].str.contains('scalp', case=False, na=False)]
    if fam_pick:
        t = t[t['family'].isin(fam_pick)]
    if mkt_pick:
        t = t[t['symbol'].isin(mkt_pick)]
    if strat_pick:
        t = t[t['strategy'].isin(strat_pick)]
    if q_pick:
        want_live = '🏅' in q_pick
        badges = [q for q in q_pick if q != '🏅']
        m = pd.Series(True, index=t.index)
        if badges:
            m &= t['flag'].str[0].isin(badges)
        if want_live:
            m &= t['flag'].str.contains('🏅')
        t = t[m]
    if only_live:
        t = t[t['live_accounts'] != '']
    if only_flag:
        t = t[t['bench'].str.contains('🛑|⚠️', na=False)]
    st.caption(f'{len(t)} of {total} robots shown · sorted by 3-month Sharpe.')

    show = t.sort_values('sharpe', ascending=False)[[
        'flag', 'strategy', 'family', 'symbol', 'timeframe', 'data_status',
        'window_pnl', 'sharpe', 'window_dd', 'bench', 'live_days', 'live_pnl',
        'live_sharpe', 'live_accounts', 'cooling', 'hist_max_loss_streak',
        'hist_max_streak_cost', 'net_profit', 'realized_dd_pct']].rename(columns={
        'flag': 'Backtest quality', 'strategy': 'EA', 'family': 'Family',
        'symbol': 'Market', 'timeframe': 'TF',
        'data_status': 'Full-history backtest',
        'window_pnl': '3m real-tick P&L ($)', 'sharpe': '3m real-tick Sharpe',
        'window_dd': '3m real-tick DD ($)',
        'bench': 'Bench status', 'live_days': 'Live fwd days',
        'live_pnl': 'Live fwd P&L ($)', 'live_sharpe': 'Live fwd Sharpe',
        'live_accounts': 'Running live on', 'cooling': 'Cooling-off',
        'hist_max_loss_streak': 'Worst streak (hist)',
        'hist_max_streak_cost': 'Worst streak cost ($, hist)',
        'net_profit': 'Full-history P&L ($)', 'realized_dd_pct': 'Full-history DD (%)'})
    st.dataframe(show, use_container_width=True, hide_index=True, height=600)
    st.caption(TRUST_LEGEND)
    st.caption('**3m columns** come from the recent-form dataset — a fresh '
               'every-tick-REAL-tick backtest of the whole pool over the last '
               '3 months (refresh it on the Benchmark tab). **Full-history '
               'backtest** says what the robot\'s 2018-onward report was built '
               'on: real-tick or 1-minute OHLC (the quality badge says how well '
               'OHLC held up). "no trades in 3m window" = the robot did not '
               'trade recently, so its 3m columns are blank. **Bench status** '
               '= the benching rules as checked on the benchmark accounts.')



# Family keyword -> stem prefixes. Order matters (more specific first).
_FAMILY_HINTS = [
    (('bitcoinreaper', 'btcreaper', 'btc_reaper'), ('BitcoinReaper_', 'BTC_Reaper_Aggr_')),
    (('goldreaper', 'gold reaper', 'thegoldreaper', 'gr_'), ('GoldReaper_',)),
    (('goldphantom', 'gold phantom', 'phantom'), ('GoldPhantom_',)),
    (('goldtradepro', 'gold trade pro', 'goldtrade', 'gtp_'), ('GOLDTRADEPRO',)),
    (('goldbotone', 'goldbot', 'golddaily', 'gbo_'), ('GoldBotOne_',)),
    (('goldscalp', 'gold_scalp', 'gold scalp', 'gs_'), ('GoldScalp_',)),
    (('bitcoinscalp', 'btcscalp'), ('BitcoinScalpPro_',)),
    (('advscalp', 'advscal', 'advanced scalper'), ('AdvScalp_', 'AdvScal_')),
    (('dtp2',), ('DTP2_',)),
    (('daytradepro', 'daytrade pro', 'day trade pro', 'dtp_'), ('DaytradePro_',)),
    (('indicement', 'ind_'), ('Indicement_',)),
    (('orbmaster', 'orb master', 'orb_'), ('ORBMASTER_',)),
    (('volatilitybreakout', 'volatility breakout', 'vb_', 'volbreak'), ('VolatilityBreakout_',)),
    (('nas100', 'ustec', 'us100'), ('Indicement_US100_', 'OtherSets_nas100_', 'VolatilityBreakout_NAS_VOL_')),
    (('us30',), ('Indicement_US30_', 'ORBMASTER_US30_', 'VolatilityBreakout_US30_VOL_')),
    (('us500', 'spx'), ('Indicement_US500', 'ORBMASTER_US500_', 'VolatilityBreakout_VolUS500_')),
    (('dax', 'de40'), ('ORBMASTER_DAX_',)),
]


_NOISE = re.compile(r'(?i)(us100|us30|us500|nas100|de40|xauusd|btcusd|eurusd|'
                    r'gbpusd|usdjpy|audusd|chfjpy|ustec|dax|h1|h4|m15|m5|d1|'
                    r'sl\d+|ohlc|realticks|everytickreal|vol|the|set)')
_WORDS = ('strategy', 'daily', 'gold', 'aggr', 'nas', 'dtp', 'reaper', 'one')


def _tokens(s):
    """Identifier tokens of a comment or stem tail: numbers and single letters,
    incl. ones glued to known words — 'The Gold Reaper_XAUUSD_5' -> ['5'],
    'strategy7_H1' -> ['7'], 'dailyK' -> ['k'], 'goldtrade_D' -> ['d'],
    'US100_4' -> ['4'] (the 100 is family noise)."""
    s2 = _NOISE.sub(' ', str(s)).lower()
    out = []
    for run in re.findall(r'\d+|[a-z]+', s2):
        if run.isdigit():
            if len(run) <= 2:
                out.append(run.lstrip('0') or '0')
        elif len(run) == 1:
            out.append(run)
        else:
            for w in _WORDS:
                if run.startswith(w) and len(run) == len(w) + 1:
                    out.append(run[-1])
                    break
    return out


def suggest_stem(comment, stems):
    """(stem, confidence) — 'exact' | 'family+token' | 'family' | ''.
    Never returns a cross-family guess; blank beats wrong."""
    c = str(comment)
    n = ''.join(ch for ch in c.lower() if ch.isalnum())
    norm = {''.join(ch for ch in s.lower() if ch.isalnum()): s for s in stems}
    if n in norm:
        return norm[n], 'exact'
    low = c.lower()
    fam_stems = []
    for keys, prefixes in _FAMILY_HINTS:
        if any(k.replace(' ', '') in n for k in keys):
            fam_stems = [s for s in stems if s.startswith(prefixes)]
            if fam_stems:
                break
    if not fam_stems:
        return '', ''
    toks = _tokens(c)
    if toks:
        hits = []
        for s in fam_stems:
            tail = s.split('_', 1)[1] if '_' in s else s
            stoks = _tokens(tail)
            if any(t in stoks for t in toks):
                hits.append(s)
        if len(hits) == 1:
            return hits[0], 'family+token'
    if len(fam_stems) == 1:
        return fam_stems[0], 'family'
    return '', 'family?'


# ── Name mapping tab ──────────────────────────────────────────────────────────

def _norm(s):
    return ''.join(ch for ch in str(s).lower() if ch.isalnum())


def _skipped_comments():
    ov = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'ea_name_map_overrides.json')
    if os.path.isfile(ov):
        try:
            with open(ov, encoding='utf-8') as f:
                return set(json.load(f).get('_skipped', []))
        except Exception:
            pass
    return set()


def _render_mapping_tab(rules):
    """Search the cached accounts for EA comments that do not resolve to a
    pool strategy, suggest the closest match, and let the user map them —
    saved to ea_name_map_overrides.json (per install)."""
    st.caption('Every EA on your accounts is identified by its **EA_Comment**. '
               'The standard UBS set-file comments map to pool strategies '
               'automatically (shipped `ea_name_map.json`). Anything else — '
               'older comments from before the set files were standardised, '
               'renamed copies, other developers\' EAs — shows up here. Map it '
               'to the pool strategy it really is and the rules, bench '
               'matching, quality badges and candidates all line up. Mappings '
               'are saved locally (`ea_name_map_overrides.json`).')
    bdir = rules.get('baseline_timeline_dir') or ''
    meta_p = os.path.join(bdir, 'ea_meta.csv')
    stems = sorted(pd.read_csv(meta_p)['strategy'].unique()) if os.path.isfile(meta_p) else []
    name_map = load_name_map()
    refs = set(rules.get('reference_accounts') or [])
    cached, _ = _configured_cache()

    # Collect raw comments per account
    seen = {}
    for d in cached:
        df = d.get('df')
        if df is None or df.empty or 'strategy' not in df.columns:
            continue
        lbl = d.get('label', d.get('account_folder', ''))
        t = df.dropna(subset=['close_time'])
        for c, g in t.groupby('strategy'):
            e = seen.setdefault(c, {'accounts': set(), 'trades': 0, 'last': None,
                                    'on_bench': False})
            e['accounts'].add(lbl)
            e['trades'] += len(g)
            lt = pd.to_datetime(g['close_time']).max()
            e['last'] = lt if e['last'] is None or lt > e['last'] else e['last']
            e['on_bench'] |= lbl in refs
    stem_set = set(stems)
    unmapped = {c: e for c, e in seen.items()
                if c not in name_map and c not in stem_set
                and c not in _skipped_comments()}
    mapped_ov = {}
    ov_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'ea_name_map_overrides.json')
    if os.path.isfile(ov_path):
        try:
            with open(ov_path, encoding='utf-8') as f:
                mapped_ov = {k: v for k, v in json.load(f).items()
                             if not k.startswith('_')}
        except Exception:
            mapped_ov = {}

    c1, c2, c3 = st.columns(3)
    c1.metric('Comments seen on accounts', len(seen))
    c2.metric('Resolve automatically', len(seen) - len(unmapped))
    c3.metric('Unmapped', len(unmapped))

    if unmapped:
        st.subheader('Unmapped comments — pick the pool strategy each one is')
        rows = []
        for c, e in sorted(unmapped.items(), key=lambda kv: -kv[1]['trades']):
            sug, conf = suggest_stem(c, stems)
            rows.append({'Live comment': c,
                         'Accounts': ', '.join(sorted(e['accounts'])),
                         'Trades': e['trades'],
                         'Last trade': str(e['last'].date()) if e['last'] is not None else '',
                         'Map to strategy': mapped_ov.get(c, sug),
                         'Suggestion': {'exact': '✅ exact', 'family+token': '🟢 family + number/letter',
                                        'family': '🟡 only robot in family',
                                        'family?': '⚪ family found, ambiguous',
                                        '': ''}[conf],
                         'Skip': False})
        edf = pd.DataFrame(rows)
        edited = st.data_editor(
            edf, key='map_editor', use_container_width=True, hide_index=True,
            column_config={
                'Map to strategy': st.column_config.SelectboxColumn(
                    'Map to strategy', options=[''] + stems, required=False,
                    help='Pre-filled with the closest match — check it, '
                         'change it, or leave blank to leave unmapped.'),
                'Skip': st.column_config.CheckboxColumn(
                    'Not a pool robot', help='Tick for EAs that are not in the '
                    'pool at all (other developers, manual trades) — they stay '
                    'unmapped and this list stops nagging about them.'),
                'Live comment': st.column_config.TextColumn(disabled=True),
                'Accounts': st.column_config.TextColumn(disabled=True),
                'Trades': st.column_config.NumberColumn(disabled=True),
                'Last trade': st.column_config.TextColumn(disabled=True),
                'Suggestion': st.column_config.TextColumn(
                    disabled=True, help='How the pre-filled match was found. '
                         'Cross-family guesses are never made — blank means '
                         'you decide.')},
            disabled=['Live comment', 'Accounts', 'Trades', 'Last trade', 'Suggestion'])
        if st.button('💾 Save mappings', type='primary', key='map_save'):
            ov = {}
            if os.path.isfile(ov_path):
                try:
                    with open(ov_path, encoding='utf-8') as f:
                        ov = json.load(f)
                except Exception:
                    ov = {}
            n_map = n_skip = 0
            for _, r in edited.iterrows():
                c = r['Live comment']
                if r['Skip']:
                    ov[c] = ''          # recorded as deliberately unmapped
                    ov.setdefault('_skipped', [])
                    if c not in ov['_skipped']:
                        ov['_skipped'].append(c)
                    n_skip += 1
                elif r['Map to strategy']:
                    ov[c] = r['Map to strategy']
                    n_map += 1
            ov.setdefault('_note', 'Personal legacy-comment bridges: {live '
                          'EA_Comment: pool strategy stem}. Merged at load by '
                          'live_rules.load_name_map(). Edited from the Name '
                          'mapping tab.')
            with open(ov_path, 'w', encoding='utf-8') as f:
                json.dump(ov, f, indent=2)
            st.success(f'Saved {n_map} mapping(s), {n_skip} marked not-a-pool-robot.')
            st.rerun()
    else:
        st.success('Every EA comment on your accounts resolves to a pool '
                   'strategy — nothing to map.')

    # Existing overrides
    active = {k: v for k, v in mapped_ov.items() if v}
    if active:
        with st.expander(f'Existing manual mappings ({len(active)})'):
            tbl = pd.DataFrame([{'Live comment': k, 'Mapped to': v,
                                 'Seen on': ', '.join(sorted(seen.get(k, {}).get('accounts', [])))}
                                for k, v in sorted(active.items())])
            st.dataframe(tbl, use_container_width=True, hide_index=True)
            rem = st.multiselect('Remove mapping(s)', sorted(active), key='map_remove')
            if rem and st.button('Remove selected', key='map_remove_btn'):
                with open(ov_path, encoding='utf-8') as f:
                    ov = json.load(f)
                for k in rem:
                    ov.pop(k, None)
                with open(ov_path, 'w', encoding='utf-8') as f:
                    json.dump(ov, f, indent=2)
                st.rerun()


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
