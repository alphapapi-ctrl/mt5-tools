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
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mt5_parser import detect_and_parse, calc_stats


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

    # ── File upload ───────────────────────────────────────────────────────────
    st.subheader("Load Files")
    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("**File A** — Reference (e.g. Backtest)")
        up_a = st.file_uploader("Upload File A", type=['html','htm','csv'], key='tc_up_a')
        if up_a:
            df_a, fmt_a = detect_and_parse(up_a.read(), up_a.name)
            if df_a is not None:
                st.session_state['tc_df_a']  = df_a
                st.session_state['tc_fmt_a'] = fmt_a
                st.success(f"✓ {len(df_a)} trades — {fmt_a}")
            else:
                st.error("Could not parse File A")
        if st.session_state['tc_df_a'] is not None:
            st.caption(f"Loaded: **{st.session_state['tc_fmt_a']}** · {len(st.session_state['tc_df_a'])} trades")

    with col_b:
        st.markdown("**File B** — Comparison (e.g. Real Account)")
        up_b = st.file_uploader("Upload File B", type=['html','htm','csv'], key='tc_up_b')
        if up_b:
            df_b, fmt_b = detect_and_parse(up_b.read(), up_b.name)
            if df_b is not None:
                st.session_state['tc_df_b']  = df_b
                st.session_state['tc_fmt_b'] = fmt_b
                st.success(f"✓ {len(df_b)} trades — {fmt_b}")
            else:
                st.error("Could not parse File B")
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
        x=list(range(len(matched))),
        y=matched['profit_var'],
        marker_color=colours,
        name='Profit Variance (B - A)'
    ))
    fig2.update_layout(
        height=250,
        plot_bgcolor='rgba(10,10,15,1)',
        paper_bgcolor='rgba(10,10,15,1)',
        font=dict(color='#aaa'),
        xaxis=dict(gridcolor='rgba(255,255,255,0.04)', title='Trade #'),
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

    display = matched[[
        'A_open_time', 'A_symbol', 'A_type',
        'A_open_price', 'A_close_price', 'A_profit', 'A_duration',
        'B_open_time',
        'B_open_price', 'B_close_price', 'B_profit', 'B_duration',
        'open_slippage', 'close_slippage', 'profit_var', 'time_diff_min'
    ]].copy()

    display.columns = [
        'A Open Time', 'Symbol', 'Type',
        'A Entry', 'A Exit', 'A Profit', 'A Dur(m)',
        'B Open Time',
        'B Entry', 'B Exit', 'B Profit', 'B Dur(m)',
        'Entry Slip', 'Exit Slip', 'Profit Var', 'Time Diff(m)'
    ]

    # Format numeric columns
    for col in ['A Entry', 'A Exit', 'B Entry', 'B Exit']:
        if col in display.columns:
            display[col] = display[col].apply(
                lambda x: f"{x:.5f}" if pd.notna(x) else '')

    for col in ['A Profit', 'B Profit', 'Profit Var']:
        display[col] = display[col].apply(
            lambda x: f"{x:+.2f}" if pd.notna(x) else '')

    for col in ['Entry Slip', 'Exit Slip']:
        display[col] = display[col].apply(
            lambda x: f"{x:+.5f}" if pd.notna(x) else '')

    st.dataframe(
        display.style
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
