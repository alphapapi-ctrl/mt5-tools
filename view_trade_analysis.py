"""
pages/trade_analysis.py
=======================
MT5 Trade Analysis page — migrated from main dashboard.
"""

import streamlit as st
import plotly.graph_objects as go
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mt5_parser import detect_and_parse, calc_stats



def _normalise_ic(df):
    """Map IC Markets DataFrame columns to the schema expected by calc_stats."""
    import pandas as pd
    out = df.copy()
    # calc_stats / render helpers need: open_time, close_time, symbol, type,
    # strategy, net_profit, win, volume, open_price, close_price,
    # commission, swap, profit, duration_min, day_of_week, hour
    if "symbol_base" in out.columns and "strategy" not in out.columns:
        out["strategy"] = out["symbol_base"]
    if "net_profit" in out.columns and "profit" not in out.columns:
        out["profit"] = out["net_profit"]
    if "commission" not in out.columns:
        out["commission"] = 0.0
    if "swap" not in out.columns:
        out["swap"] = 0.0
    if "sl" not in out.columns:
        out["sl"] = None
    if "tp" not in out.columns:
        out["tp"] = None
    # Ensure win column
    if "win" not in out.columns and "net_profit" in out.columns:
        out["win"] = out["net_profit"] > 0
    # Ensure day_of_week and hour
    if "open_time" in out.columns:
        out["open_time"] = pd.to_datetime(out["open_time"], errors="coerce")
        if "day_of_week" not in out.columns:
            out["day_of_week"] = out["open_time"].dt.day_name()
        if "hour" not in out.columns:
            out["hour"] = out["open_time"].dt.hour
    if "close_time" in out.columns:
        out["close_time"] = pd.to_datetime(out["close_time"], errors="coerce")
    if "duration_min" not in out.columns and "open_time" in out.columns and "close_time" in out.columns:
        out["duration_min"] = ((out["close_time"] - out["open_time"])
                               .dt.total_seconds() / 60).round(1)
    return out


def render():
    st.title("📊 Trade Analysis")

    # ── Session state ─────────────────────────────────────────────────────────
    for _k, _v in {
        'ta_df': None, 'ta_format': None,
        'ta_accounts': [], 'ta_ic_bytes': None,
    }.items():
        if _k not in st.session_state:
            st.session_state[_k] = _v

    # ── Source selector ──────────────────────────────────────────────────────
    src_col1, src_col2 = st.columns([4, 1])
    with src_col1:
        source = st.radio(
            "File source",
            ["MT5 / Quant Analyzer", "IC Markets XLSX"],
            horizontal=True, key='ta_source',
        )
    with src_col2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🗑 Clear", key='ta_clear'):
            st.session_state['ta_df']      = None
            st.session_state['ta_format']  = None
            st.session_state['ta_accounts'] = []
            st.rerun()

    # ── File upload ───────────────────────────────────────────────────────────
    if source == "MT5 / Quant Analyzer":
        uploaded = st.file_uploader(
            "Upload MT5 Report (HTM/HTML) or Quant Analyzer CSV",
            type=None, key='ta_upload',
        )
        if uploaded and uploaded.name.lower().endswith(('.htm','.html','.csv')):
            df, fmt = detect_and_parse(uploaded.read(), uploaded.name)
            if df is not None:
                st.session_state['ta_df']       = df
                st.session_state['ta_format']   = fmt
                st.session_state['ta_accounts'] = []
                st.success(f"✓ Loaded {len(df)} trades — {fmt}")
            else:
                st.error("Could not parse report — check file format")
        elif uploaded:
            st.warning("Please upload a .htm, .html, or .csv file.")

    else:  # IC Markets XLSX
        uploaded = st.file_uploader(
            "Upload IC Markets Position History (.xlsx)",
            type=None, key='ta_upload',
        )
        if uploaded and uploaded.name.lower().endswith(('.xlsx','.xls')):
            try:
                from icmarkets_parser import get_icmarkets_accounts, parse_icmarkets_xlsx
            except ImportError as e:
                st.error(f"icmarkets_parser.py not found — ensure it is in the MT5Tools folder. ({e})")
                uploaded = None
            if uploaded:
                try:
                    file_bytes = uploaded.read()
                    accounts   = get_icmarkets_accounts(file_bytes)
                    if not accounts:
                        st.error("No accounts found — check this is an IC Markets Position History export.")
                    else:
                        st.session_state['ta_ic_bytes']  = file_bytes
                        st.session_state['ta_accounts']  = accounts
                        st.session_state['ta_format']    = "IC Markets XLSX"
                        df_ic = parse_icmarkets_xlsx(file_bytes, account=accounts[0])
                        df_ic = _normalise_ic(df_ic)
                        st.session_state['ta_df'] = df_ic
                        st.success(f"✓ Loaded {len(df_ic)} trades — {len(accounts)} account(s) found")
                except Exception as e:
                    st.error(f"Error parsing file: {e}")
                    import traceback; st.code(traceback.format_exc())
        elif uploaded:
            st.warning("Please upload an .xlsx file.")

    # IC Markets account selector (shown after upload)
    if (st.session_state.get('ta_accounts') and
            st.session_state.get('ta_source', source) == "IC Markets XLSX"):
        accounts = st.session_state['ta_accounts']
        ac_opts  = ["All accounts"] + accounts
        sel_ac   = st.selectbox("Account", ac_opts, key='ta_ic_account')
        acct     = None if sel_ac == "All accounts" else sel_ac
        if st.session_state.get('ta_ic_bytes'):
            from icmarkets_parser import parse_icmarkets_xlsx
            df_ic = parse_icmarkets_xlsx(st.session_state['ta_ic_bytes'], account=acct)
            df_ic = _normalise_ic(df_ic)
            st.session_state['ta_df'] = df_ic

    df_all = st.session_state['ta_df']
    fmt    = st.session_state['ta_format']

    if df_all is None or len(df_all) == 0:
        st.markdown("""
        <div class="info-card">
            Upload an MT5 account history report (.htm/.html), MT5 backtest report,
            or a Quant Analyzer CSV export to begin analysis.
        </div>
        """, unsafe_allow_html=True)
        return

    if fmt:
        st.caption(f"Format detected: **{fmt}** · {len(df_all)} total trades")

    # ── Filters ───────────────────────────────────────────────────────────────
    st.divider()
    fc1, fc2, fc3, fc4 = st.columns(4)

    with fc1:
        date_min  = df_all['open_time'].min().date()
        date_max  = df_all['open_time'].max().date()
        date_from = st.date_input("From", value=date_min, min_value=date_min,
                                  max_value=date_max, key='ta_from')
        date_to   = st.date_input("To",   value=date_max, min_value=date_min,
                                  max_value=date_max, key='ta_to')

    with fc2:
        symbols    = sorted(df_all['symbol'].dropna().unique().tolist())
        sel_symbol = st.multiselect("Symbol", symbols, key='ta_sym')

    with fc3:
        strategies    = sorted(df_all['strategy'].dropna().unique().tolist())
        sel_strategy  = st.multiselect("Strategy / EA", strategies, key='ta_strat')

    with fc4:
        days     = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
        sel_days = st.multiselect("Day of week", days, key='ta_days')
        sel_type = st.multiselect("Type", ['buy', 'sell'], key='ta_type')

    # Apply filters
    df = df_all.copy()
    df = df[(df['open_time'].dt.date >= date_from) &
            (df['open_time'].dt.date <= date_to)]
    if sel_symbol:
        df = df[df['symbol'].isin(sel_symbol)]
    if sel_strategy:
        df = df[df['strategy'].isin(sel_strategy)]
    if sel_days:
        df = df[df['day_of_week'].isin(sel_days)]
    if sel_type:
        df = df[df['type'].isin(sel_type)]

    st.caption(f"Showing **{len(df)}** trades after filters")

    # ── Analysis mode ─────────────────────────────────────────────────────────
    mode = st.radio(
        "Analysis mode",
        ["Overall", "By Strategy", "By Symbol", "By Day of Week"],
        horizontal=True, key='ta_mode'
    )
    st.divider()

    # ── Helpers ───────────────────────────────────────────────────────────────
    def render_stats(stats, label=""):
        if label:
            st.markdown(f"**{label}**")

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Net Profit",    f"${stats['net_profit']:,.2f}")
        c2.metric("Win Rate",      f"{stats['win_rate']}%")
        c3.metric("Profit Factor", f"{stats['profit_factor']}")
        c4.metric("R:R Ratio",     f"{stats['rr_ratio']}")
        c5.metric("Expectancy",    f"${stats['expectancy']:,.2f}")

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Trades",  stats['total_trades'])
        c2.metric("Avg Win",       f"${stats['avg_win']:,.2f}")
        c3.metric("Avg Loss",      f"${stats['avg_loss']:,.2f}")
        c4.metric("Max DD",        f"${stats['max_drawdown']:,.2f}")
        c5.metric("Best Trade",    f"${stats['best_trade']:,.2f}")

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Max Consec Wins",   stats['max_consec_wins'])
        c2.metric("Max Consec Losses", stats['max_consec_losses'])
        c3.metric("Avg Win Dur",       f"{stats['avg_win_duration']}m")
        c4.metric("Avg Loss Dur",      f"{stats['avg_loss_duration']}m")
        c5.metric("Worst Trade",       f"${stats['worst_trade']:,.2f}")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Long Trades",   stats['long_trades'])
        c2.metric("Long Win Rate", f"{stats['long_win_rate']}%")
        c3.metric("Short Trades",  stats['short_trades'])
        c4.metric("Short Win Rate",f"{stats['short_win_rate']}%")

    def render_equity_curve(df_plot, label="Equity Curve"):
        df_s = df_plot.sort_values('close_time').copy()
        df_s['cumulative'] = df_s['net_profit'].cumsum()
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df_s['close_time'], y=df_s['cumulative'],
            mode='lines',
            line=dict(color='#7c6af7', width=2),
            fill='tozeroy',
            fillcolor='rgba(124,106,247,0.08)',
            name='Equity'
        ))
        fig.update_layout(
            title=label, height=300,
            plot_bgcolor='rgba(10,10,15,1)',
            paper_bgcolor='rgba(10,10,15,1)',
            font=dict(color='#aaa', family='JetBrains Mono'),
            xaxis=dict(gridcolor='rgba(255,255,255,0.04)'),
            yaxis=dict(gridcolor='rgba(255,255,255,0.04)', tickprefix='$'),
            margin=dict(l=60, r=20, t=40, b=40)
        )
        st.plotly_chart(fig, use_container_width=True)

    def render_dow_chart(df_plot):
        dow_order = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
        dow = df_plot.groupby('day_of_week').agg(
            trades     = ('net_profit', 'count'),
            net_profit = ('net_profit', 'sum'),
            win_rate   = ('win', lambda x: round(x.mean()*100, 1))
        ).reindex([d for d in dow_order if d in df_plot['day_of_week'].unique()])

        wins_dow   = df_plot[df_plot['win']].groupby('day_of_week')['net_profit'].sum().reindex(dow.index, fill_value=0)
        losses_dow = df_plot[~df_plot['win']].groupby('day_of_week')['net_profit'].sum().reindex(dow.index, fill_value=0)

        fig = go.Figure()
        fig.add_trace(go.Bar(x=dow.index, y=wins_dow,   name='Profit', marker_color='rgba(45,198,83,0.8)'))
        fig.add_trace(go.Bar(x=dow.index, y=losses_dow, name='Loss',   marker_color='rgba(230,57,70,0.8)'))
        fig.update_layout(
            title='P&L by Day of Week', height=280, barmode='relative',
            plot_bgcolor='rgba(10,10,15,1)', paper_bgcolor='rgba(10,10,15,1)',
            font=dict(color='#aaa'), margin=dict(l=60, r=20, t=40, b=40),
            xaxis=dict(gridcolor='rgba(255,255,255,0.04)'),
            yaxis=dict(gridcolor='rgba(255,255,255,0.04)', tickprefix='$'),
            legend=dict(bgcolor='rgba(0,0,0,0.3)')
        )
        st.plotly_chart(fig, use_container_width=True)

        dt = dow.reset_index()
        dt.columns = ['Day', 'Trades', 'Net Profit', 'Win Rate %']
        dt['Net Profit'] = dt['Net Profit'].round(2)
        st.dataframe(dt, use_container_width=True, hide_index=True)

    def render_hour_chart(df_plot):
        hourly = df_plot.groupby('hour').agg(
            trades     = ('net_profit', 'count'),
            net_profit = ('net_profit', 'sum'),
        )
        wins_h   = df_plot[df_plot['win']].groupby('hour')['net_profit'].sum().reindex(hourly.index, fill_value=0)
        losses_h = df_plot[~df_plot['win']].groupby('hour')['net_profit'].sum().reindex(hourly.index, fill_value=0)

        fig = go.Figure()
        fig.add_trace(go.Bar(x=wins_h.index,   y=wins_h,   name='Profit', marker_color='rgba(45,198,83,0.8)'))
        fig.add_trace(go.Bar(x=losses_h.index, y=losses_h, name='Loss',   marker_color='rgba(230,57,70,0.8)'))
        fig.update_layout(
            title='P&L by Hour of Day', height=280, barmode='relative',
            plot_bgcolor='rgba(10,10,15,1)', paper_bgcolor='rgba(10,10,15,1)',
            font=dict(color='#aaa'), margin=dict(l=60, r=20, t=40, b=40),
            xaxis=dict(gridcolor='rgba(255,255,255,0.04)', title='Hour (UTC)'),
            yaxis=dict(gridcolor='rgba(255,255,255,0.04)', tickprefix='$'),
            legend=dict(bgcolor='rgba(0,0,0,0.3)')
        )
        st.plotly_chart(fig, use_container_width=True)

    def colour_profit(val):
        try:
            v = float(str(val).replace(',', ''))
            if v > 0: return 'background-color: rgba(0,180,0,0.12)'
            if v < 0: return 'background-color: rgba(180,0,0,0.12)'
        except:
            pass
        return ''

    # ── Render mode ───────────────────────────────────────────────────────────
    if mode == "Overall":
        stats = calc_stats(df)
        render_stats(stats, "Overall Statistics")
        render_equity_curve(df)
        col1, col2 = st.columns(2)
        with col1:
            render_dow_chart(df)
        with col2:
            render_hour_chart(df)

    elif mode == "By Strategy":
        strats = sorted(df['strategy'].dropna().unique().tolist())
        if not strats:
            st.info("No strategies found")
        else:
            st.subheader("Strategy Comparison")
            rows = []
            for s in strats:
                sdf  = df[df['strategy'] == s]
                stat = calc_stats(sdf)
                rows.append({
                    'Strategy'      : s,
                    'Trades'        : stat['total_trades'],
                    'Net Profit'    : stat['net_profit'],
                    'Win Rate %'    : stat['win_rate'],
                    'Profit Factor' : stat['profit_factor'],
                    'R:R'           : stat['rr_ratio'],
                    'Expectancy'    : stat['expectancy'],
                    'Max DD'        : stat['max_drawdown'],
                    'Max Consec W'  : stat['max_consec_wins'],
                    'Max Consec L'  : stat['max_consec_losses'],
                })
            sdf_sum = __import__('pandas').DataFrame(rows).sort_values('Net Profit', ascending=False)
            st.dataframe(
                sdf_sum.style.map(colour_profit, subset=['Net Profit', 'Expectancy', 'Max DD']),
                use_container_width=True, hide_index=True
            )
            st.divider()
            sel = st.selectbox("Select strategy for detail", strats)
            if sel:
                sdf  = df[df['strategy'] == sel]
                stat = calc_stats(sdf)
                render_stats(stat, sel)
                render_equity_curve(sdf, f"{sel} — Equity Curve")
                col1, col2 = st.columns(2)
                with col1: render_dow_chart(sdf)
                with col2: render_hour_chart(sdf)

    elif mode == "By Symbol":
        syms = sorted(df['symbol'].dropna().unique().tolist())
        rows = []
        for s in syms:
            sdf  = df[df['symbol'] == s]
            stat = calc_stats(sdf)
            rows.append({
                'Symbol'        : s,
                'Trades'        : stat['total_trades'],
                'Net Profit'    : stat['net_profit'],
                'Win Rate %'    : stat['win_rate'],
                'Profit Factor' : stat['profit_factor'],
                'R:R'           : stat['rr_ratio'],
                'Expectancy'    : stat['expectancy'],
                'Max DD'        : stat['max_drawdown'],
            })
        sdf_sum = __import__('pandas').DataFrame(rows).sort_values('Net Profit', ascending=False)
        st.dataframe(
            sdf_sum.style.map(colour_profit, subset=['Net Profit', 'Expectancy', 'Max DD']),
            use_container_width=True, hide_index=True
        )
        sel = st.selectbox("Select symbol for detail", syms)
        if sel:
            sdf  = df[df['symbol'] == sel]
            stat = calc_stats(sdf)
            render_stats(stat, sel)
            render_equity_curve(sdf, f"{sel} — Equity Curve")
            col1, col2 = st.columns(2)
            with col1: render_dow_chart(sdf)
            with col2: render_hour_chart(sdf)

    elif mode == "By Day of Week":
        render_dow_chart(df)
        render_hour_chart(df)

    # ── Raw trade log ─────────────────────────────────────────────────────────
    st.divider()
    with st.expander("Raw Trade Log"):
        show_cols = ['open_time', 'close_time', 'symbol', 'type', 'strategy',
                     'volume', 'open_price', 'close_price', 'sl', 'tp',
                     'commission', 'swap', 'profit', 'net_profit', 'duration_min']
        show_cols = [c for c in show_cols if c in df.columns]

        def colour_net(val):
            try:
                v = float(val)
                if v > 0: return 'background-color: rgba(0,180,0,0.12)'
                if v < 0: return 'background-color: rgba(180,0,0,0.12)'
            except:
                pass
            return ''

        st.dataframe(
            df[show_cols].style.map(colour_net, subset=['net_profit', 'profit']),
            use_container_width=True, hide_index=True, height=400
        )
        st.download_button(
            "⬇ Download filtered trades CSV",
            data      = df[show_cols].to_csv(index=False),
            file_name = f"mt5_trades_{date_from}_{date_to}.csv",
            mime      = 'text/csv'
        )