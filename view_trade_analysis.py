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



def _generate_html_report(df_plot, stats, fmt, view_sel,
                           stats_compare=None, df_compare=None,
                           group_summary=None, date_from=None, date_to=None,
                           deposit=10000.0):
    """Generate a self-contained HTML report of the trade analysis."""
    import pandas as pd
    import json
    from datetime import datetime

    now   = datetime.now().strftime('%Y-%m-%d %H:%M')
    title = f"Trade Analysis Report — {view_sel}"

    df_s = df_plot.copy()
    df_s['net_profit'] = pd.to_numeric(df_s['net_profit'], errors='coerce').fillna(0)
    df_s['close_time'] = pd.to_datetime(df_s['close_time'], errors='coerce')
    df_s = df_s.dropna(subset=['close_time']).sort_values('close_time').reset_index(drop=True)
    df_s['_cum']  = df_s['net_profit'].cumsum()
    df_s['_peak'] = df_s['_cum'].cummax()
    df_s['_dd']   = df_s['_cum'] - df_s['_peak']
    df_s['win']   = df_s['net_profit'] > 0
    if 'day_of_week' not in df_s.columns:
        df_s['day_of_week'] = df_s['close_time'].dt.day_name()
    if 'hour' not in df_s.columns:
        df_s['hour'] = df_s['close_time'].dt.hour

    LAYOUT_BASE = {
        'plot_bgcolor': 'rgba(20,20,30,1)',
        'paper_bgcolor': 'rgba(20,20,30,1)',
        'font': {'color': '#ccc', 'family': 'sans-serif'},
        'legend': {'bgcolor': 'rgba(0,0,0,0)', 'borderwidth': 0},
        'xaxis': {'gridcolor': 'rgba(128,128,128,0.15)'},
        'yaxis': {'gridcolor': 'rgba(128,128,128,0.15)', 'tickprefix': '$'},
    }

    def _chart(div_id, traces, layout_extra=None):
        layout = {**LAYOUT_BASE, **(layout_extra or {})}
        traces_json = json.dumps(traces)
        layout_json = json.dumps(layout)
        return (
            f'<div id="{div_id}" style="width:100%;height:{layout.get("height",300)}px"></div>\n'
            f'<script>Plotly.newPlot("{div_id}",{traces_json},{layout_json},'
            f'{{"responsive":true,"displayModeBar":false}});</script>'
        )

    # Equity
    eq_traces = [{
        'type': 'scatter', 'mode': 'lines', 'name': 'Equity',
        'x': df_s['close_time'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist(),
        'y': df_s['_cum'].round(2).tolist(),
        'line': {'color': '#7c6af7', 'width': 2},
        'fill': 'tozeroy', 'fillcolor': 'rgba(124,106,247,0.08)',
    }]
    if df_compare is not None:
        dc = df_compare.copy()
        dc['net_profit'] = pd.to_numeric(dc['net_profit'], errors='coerce').fillna(0)
        dc['close_time'] = pd.to_datetime(dc['close_time'], errors='coerce')
        dc = dc.dropna(subset=['close_time']).sort_values('close_time')
        dc['_cum'] = dc['net_profit'].cumsum()
        eq_traces.append({
            'type': 'scatter', 'mode': 'lines', 'name': 'Edited',
            'x': dc['close_time'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist(),
            'y': dc['_cum'].round(2).tolist(),
            'line': {'color': '#34C27A', 'width': 2, 'dash': 'dash'},
        })
    eq_html = _chart('eq_chart', eq_traces, {'height': 300, 'title': 'Equity Curve',
        'hovermode': 'x unified', 'margin': {'l':60,'r':20,'t':40,'b':40}})

    # Drawdown
    dd_html = _chart('dd_chart', [{
        'type': 'scatter', 'mode': 'lines', 'name': 'Drawdown',
        'x': df_s['close_time'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist(),
        'y': df_s['_dd'].round(2).tolist(),
        'line': {'color': '#dc5050', 'width': 1.5},
        'fill': 'tozeroy', 'fillcolor': 'rgba(220,80,80,0.18)',
    }], {'height': 150, 'title': 'Drawdown', 'showlegend': False,
         'margin': {'l':60,'r':20,'t':40,'b':20},
         'xaxis': {'gridcolor':'rgba(128,128,128,0.15)', 'showticklabels': False},
         'yaxis': {'gridcolor':'rgba(128,128,128,0.15)', 'tickprefix':'$'},
         'plot_bgcolor':'rgba(20,20,30,1)', 'paper_bgcolor':'rgba(20,20,30,1)',
         'font':{'color':'#ccc','family':'sans-serif'}})

    # Daily P&L
    daily = df_s.groupby(df_s['close_time'].dt.strftime('%Y-%m-%d'))['net_profit'].sum()
    daily_dates = daily.index.tolist()
    daily_vals  = daily.round(2).tolist()
    daily_colors = ['rgba(52,194,122,0.85)' if v >= 0 else 'rgba(220,80,80,0.85)' for v in daily_vals]
    daily_html = _chart('daily_chart', [{
        'type': 'bar', 'name': 'Daily P&L',
        'x': daily_dates, 'y': daily_vals,
        'marker': {'color': daily_colors},
    }], {'height': 160, 'title': 'Daily P&L', 'showlegend': False,
         'bargap': 0.2, 'margin': {'l':60,'r':20,'t':40,'b':40},
         'xaxis': {'type': 'category', 'gridcolor': 'rgba(128,128,128,0.15)', 'showticklabels': False},
         'yaxis': {'gridcolor': 'rgba(128,128,128,0.15)', 'tickprefix': '$',
                   'zeroline': True, 'zerolinecolor': 'rgba(128,128,128,0.4)'},
         'plot_bgcolor':'rgba(20,20,30,1)', 'paper_bgcolor':'rgba(20,20,30,1)',
         'font':{'color':'#ccc','family':'sans-serif'}})

    # DOW
    dow_order    = ['Monday','Tuesday','Wednesday','Thursday','Friday']
    present_days = [d for d in dow_order if d in df_s['day_of_week'].values]
    wins_dow   = df_s[df_s['win']].groupby('day_of_week')['net_profit'].sum().reindex(present_days, fill_value=0)
    losses_dow = df_s[~df_s['win']].groupby('day_of_week')['net_profit'].sum().reindex(present_days, fill_value=0)
    dow_html = _chart('dow_chart', [
        {'type':'bar','name':'Profit','x':present_days,'y':wins_dow.round(2).tolist(),
         'marker':{'color':'rgba(52,194,122,0.85)'}},
        {'type':'bar','name':'Loss',  'x':present_days,'y':losses_dow.round(2).tolist(),
         'marker':{'color':'rgba(220,80,80,0.85)'}},
    ], {'height':280,'title':'P&L by Day of Week','barmode':'relative','bargap':0.3,
        'margin':{'l':60,'r':20,'t':40,'b':40},
        'xaxis':{'type':'category','gridcolor':'rgba(128,128,128,0.15)'},
        'yaxis':{'gridcolor':'rgba(128,128,128,0.15)','tickprefix':'$'},
        'plot_bgcolor':'rgba(20,20,30,1)','paper_bgcolor':'rgba(20,20,30,1)',
        'font':{'color':'#ccc','family':'sans-serif'},
        'legend':{'bgcolor':'rgba(0,0,0,0)'}})

    # Hour
    all_hours  = sorted(df_s['hour'].unique())
    str_hours  = [str(h) for h in all_hours]
    wins_h   = df_s[df_s['win']].groupby('hour')['net_profit'].sum().reindex(all_hours, fill_value=0)
    losses_h = df_s[~df_s['win']].groupby('hour')['net_profit'].sum().reindex(all_hours, fill_value=0)
    hour_html = _chart('hour_chart', [
        {'type':'bar','name':'Profit','x':str_hours,'y':wins_h.round(2).tolist(),
         'marker':{'color':'rgba(52,194,122,0.85)'}},
        {'type':'bar','name':'Loss',  'x':str_hours,'y':losses_h.round(2).tolist(),
         'marker':{'color':'rgba(220,80,80,0.85)'}},
    ], {'height':280,'title':'P&L by Hour of Day','barmode':'relative','bargap':0.3,
        'margin':{'l':60,'r':20,'t':40,'b':40},
        'xaxis':{'type':'category','title':'Hour (UTC)','gridcolor':'rgba(128,128,128,0.15)'},
        'yaxis':{'gridcolor':'rgba(128,128,128,0.15)','tickprefix':'$'},
        'plot_bgcolor':'rgba(20,20,30,1)','paper_bgcolor':'rgba(20,20,30,1)',
        'font':{'color':'#ccc','family':'sans-serif'},
        'legend':{'bgcolor':'rgba(0,0,0,0)'}})

    # ── Stats table ───────────────────────────────────────────────────────────
    def _delta_html(key, fmt='$', inverse=False):
        if stats_compare is None or key not in stats_compare: return ''
        diff = stats_compare[key] - stats[key]
        if abs(diff) < 0.001: return ''
        better = diff > 0 if not inverse else diff < 0
        col   = '#34C27A' if better else '#E05555'
        arrow = '▲' if diff > 0 else '▼'
        val   = f"${abs(diff):.2f}" if fmt=='$' else f"{abs(diff):.2f}"
        return f'<span style="color:{col};font-size:11px;margin-left:6px">{arrow}{val}</span>'

    def _stat(label, val, delta=''):
        return f'<div class="sc"><div class="sl">{label}</div><div class="sv">{val}{delta}</div></div>'

    stats_html = f"""
    <div class="stats-grid">
      {_stat("Net Profit", f"${stats['net_profit']:,.2f}", _delta_html('net_profit','$'))}
      {_stat("Win Rate",   f"{stats['win_rate']}%",       _delta_html('win_rate','%'))}
      {_stat("Profit Factor", str(stats['profit_factor']), _delta_html('profit_factor','x'))}
      {_stat("R:R Ratio",  str(stats['rr_ratio']),        _delta_html('rr_ratio','x'))}
      {_stat("Expectancy", f"${stats['expectancy']:,.2f}", _delta_html('expectancy','$'))}
      {_stat("Total Trades", str(stats['total_trades']),  _delta_html('total_trades',''))}
      {_stat("Trading Days", str(stats.get('trading_days',0)), _delta_html('trading_days',''))}
      {_stat("Trades/Day",   str(stats.get('trades_per_day',0)), _delta_html('trades_per_day','x'))}
      {_stat("Avg Win",    f"${stats['avg_win']:,.2f}",   _delta_html('avg_win','$'))}
      {_stat("Avg Loss",   f"${stats['avg_loss']:,.2f}",  _delta_html('avg_loss','$', inverse=True))}
      {_stat("Max DD",     f"${stats['max_drawdown']:,.2f} ({abs(stats.get('max_drawdown_pct',0)):.2f}%)", _delta_html('max_drawdown','$', inverse=True))}
      {_stat("Best Trade", f"${stats['best_trade']:,.2f}", _delta_html('best_trade','$'))}
      {_stat("Worst Trade",f"${stats['worst_trade']:,.2f}", _delta_html('worst_trade','$', inverse=True))}
      {_stat("Max Consec Wins",   str(stats['max_consec_wins']),   _delta_html('max_consec_wins',''))}
      {_stat("Max Consec Losses", str(stats['max_consec_losses']), _delta_html('max_consec_losses','', inverse=True))}
      {_stat("Long Trades",  str(stats['long_trades']),       _delta_html('long_trades',''))}
      {_stat("Long Win Rate",f"{stats['long_win_rate']}%",    _delta_html('long_win_rate','%'))}
      {_stat("Short Trades", str(stats['short_trades']),      _delta_html('short_trades',''))}
      {_stat("Short Win Rate",f"{stats['short_win_rate']}%",  _delta_html('short_win_rate','%'))}
    </div>"""

    # ── Monthly table ─────────────────────────────────────────────────────────
    def _monthly_html(df_m, label, deposit=10000.0, table_id='mt1'):
        if df_m is None or df_m.empty: return ''
        tmp = df_m[['close_time','net_profit']].dropna().copy()
        tmp['year']  = pd.to_datetime(tmp['close_time']).dt.year
        tmp['month'] = pd.to_datetime(tmp['close_time']).dt.month
        monthly = tmp.groupby(['year','month'])['net_profit'].sum().reset_index()
        if monthly.empty: return ''
        pivot = monthly.pivot(index='year', columns='month', values='net_profit').fillna(0)
        pivot.columns = [pd.Timestamp(2000,int(m),1).strftime('%b') for m in pivot.columns]
        pivot['YTD'] = pivot.sum(axis=1)
        pivot = pivot.sort_index(ascending=False)
        month_order = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec','YTD']
        cols = [c for c in month_order if c in pivot.columns]
        hdr = '<tr><th>Year</th>' + ''.join(f'<th>{c}</th>' for c in cols) + '</tr>'

        def _rows(use_pct):
            out = ''
            for year, row in pivot[cols].iterrows():
                cells = f'<td>{year}</td>'
                for col in cols:
                    v = row.get(col, 0)
                    pv = round(v / deposit * 100, 2) if use_pct else v
                    bg = 'rgba(52,194,122,0.18)' if pv>0 else ('rgba(220,80,80,0.18)' if pv<0 else 'transparent')
                    fg = '#34C27A' if pv>0 else ('#E05555' if pv<0 else '#888')
                    txt = (f'{pv:+.2f}%' if pv!=0 else '—') if use_pct else (f'{pv:+.2f}' if pv!=0 else '—')
                    cells += f'<td style="background:{bg};color:{fg}">{txt}</td>'
                out += f'<tr>{cells}</tr>'
            return out

        rows_d = _rows(False)
        rows_p = _rows(True)

        return f'''
<div style="margin:20px 0">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
    <h3 style="margin:0">{label}</h3>
    <div style="display:flex;border:1px solid rgba(255,255,255,0.15);border-radius:4px;overflow:hidden;font-size:11px">
      <button onclick="mtToggle('{table_id}','$')" id="{table_id}_btn_d"
        style="padding:3px 10px;background:rgba(124,106,247,0.3);color:#e2e8f0;border:none;cursor:pointer">$</button>
      <button onclick="mtToggle('{table_id}','%')" id="{table_id}_btn_p"
        style="padding:3px 10px;background:transparent;color:#888;border:none;cursor:pointer">%</button>
    </div>
    <span style="font-size:11px;color:#666">Initial balance: ${deposit:,.0f}</span>
  </div>
  <div id="{table_id}_d"><table class="tbl"><thead>{hdr}</thead><tbody>{rows_d}</tbody></table></div>
  <div id="{table_id}_p" style="display:none"><table class="tbl"><thead>{hdr}</thead><tbody>{rows_p}</tbody></table></div>
</div>
<script>
function mtToggle(id, mode) {{
  document.getElementById(id+'_d').style.display = mode==='$' ? '' : 'none';
  document.getElementById(id+'_p').style.display = mode==='%' ? '' : 'none';
  document.getElementById(id+'_btn_d').style.background = mode==='$' ? 'rgba(124,106,247,0.3)' : 'transparent';
  document.getElementById(id+'_btn_d').style.color = mode==='$' ? '#e2e8f0' : '#888';
  document.getElementById(id+'_btn_p').style.background = mode==='%' ? 'rgba(124,106,247,0.3)' : 'transparent';
  document.getElementById(id+'_btn_p').style.color = mode==='%' ? '#e2e8f0' : '#888';
}}
</script>'''

    monthly_html = _monthly_html(df_s, "Monthly Performance", deposit=deposit, table_id='mt1')

    # ── Position summary ──────────────────────────────────────────────────────
    pos_html = ''
    if group_summary:
        gs_df = pd.DataFrame(group_summary)
        hdr = '<tr>' + ''.join(f'<th>{c}</th>' for c in gs_df.columns) + '</tr>'
        rows_html = ''
        for _, row in gs_df.iterrows():
            v = row.get('Net P&L ($)', 0)
            try: v = float(v)
            except: v = 0
            bg = 'rgba(52,194,122,0.12)' if v>0 else ('rgba(220,80,80,0.12)' if v<0 else '')
            cells = ''.join(f'<td style="background:{bg if col=='Net P&L ($)' else ''}">{row[col]}</td>'
                            for col in gs_df.columns)
            rows_html += f'<tr>{cells}</tr>'
        n_pos = len(gs_df)
        pos_html = (
            f'<details class="sd"><summary>Position Summary ({n_pos} positions)</summary>'
            f'<table class="tbl"><thead>{hdr}</thead><tbody>{rows_html}</tbody></table>'
            f'</details>'
        )

    # ── Trade log ─────────────────────────────────────────────────────────────
    log_cols = ['open_time','close_time','symbol','type','volume',
                'open_price','close_price','net_profit']
    log_cols = [c for c in log_cols if c in df_s.columns]
    log_hdr  = '<tr>' + ''.join(f'<th>{c}</th>' for c in log_cols) + '</tr>'
    log_rows = ''
    for i, (_, row) in enumerate(df_s[log_cols].iterrows()):
        bg = ''
        try:
            v = float(row['net_profit'])
            bg = 'rgba(52,194,122,0.08)' if v>0 else 'rgba(220,80,80,0.08)'
        except: pass
        cells = ''.join(f'<td style="background:{bg if col=='net_profit' else ''}">{row[col]}</td>'
                        for col in log_cols)
        log_rows += f'<tr>{cells}</tr>'
    n_log = len(df_s)
    log_html = (
        f'<details class="sd"><summary>Trade Log ({n_log} trades)</summary>'
        f'<table class="tbl tbl-sm"><thead>{log_hdr}</thead><tbody>{log_rows}</tbody></table>'
        f'</details>'
    )

    # ── Assemble ──────────────────────────────────────────────────────────────
    date_str = f"{date_from} — {date_to}" if date_from else ''
    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>{title}</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background:#0e1117; color:#e2e8f0; font-family:sans-serif; font-size:13px; padding:24px; }}
  h1 {{ font-size:22px; color:#7c6af7; margin-bottom:4px; }}
  h2 {{ font-size:16px; color:#a0aec0; margin:24px 0 12px; border-bottom:1px solid rgba(255,255,255,0.08); padding-bottom:6px; }}
  h3 {{ font-size:14px; color:#a0aec0; margin:20px 0 8px; }}
  .meta {{ color:#666; font-size:11px; margin-bottom:24px; }}
  .stats-grid {{ display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin-bottom:20px; }}
  .sc {{ background:rgba(255,255,255,0.04); border-radius:6px; padding:10px 12px; }}
  .sl {{ font-size:10px; color:#888; margin-bottom:4px; text-transform:uppercase; letter-spacing:.5px; }}
  .sv {{ font-size:16px; font-weight:600; color:#e2e8f0; }}
  .charts {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:20px; }}
  .chart-full {{ margin-bottom:8px; }}
  table.tbl {{ width:100%; border-collapse:collapse; font-size:12px; margin-bottom:20px; }}
  table.tbl th {{ background:rgba(255,255,255,0.06); padding:6px 10px; text-align:left; color:#888; font-weight:500; }}
  table.tbl td {{ padding:5px 10px; border-bottom:1px solid rgba(255,255,255,0.04); }}
  table.tbl-sm td, table.tbl-sm th {{ font-size:11px; padding:3px 8px; }}
  .tag {{ display:inline-block; background:rgba(124,106,247,0.2); color:#7c6af7;
          border-radius:4px; padding:2px 8px; font-size:11px; margin-bottom:16px; }}
  details.sd {{ margin:20px 0; border:1px solid rgba(255,255,255,0.08); border-radius:6px; overflow:hidden; }}
  details.sd summary {{ padding:10px 16px; cursor:pointer; font-size:14px; font-weight:600;
          color:#a0aec0; background:rgba(255,255,255,0.03); list-style:none;
          display:flex; align-items:center; gap:8px; user-select:none; }}
  details.sd summary::-webkit-details-marker {{ display:none; }}
  details.sd summary::before {{ content:'\25B6'; font-size:10px; transition:transform 0.2s; }}
  details[open].sd summary::before {{ transform:rotate(90deg); }}
  details.sd summary:hover {{ background:rgba(255,255,255,0.06); }}
</style>
</head><body>
<h1>{title}</h1>
<div class="meta">Generated {now} &nbsp;·&nbsp; {date_str} &nbsp;·&nbsp; Format: {fmt}</div>
<span class="tag">{view_sel}</span>

<h2>Statistics</h2>
{stats_html}

<h2>Charts</h2>
<div class="chart-full">{eq_html}</div>
<div class="chart-full">{dd_html}</div>
<div class="chart-full">{daily_html}</div>
<div class="charts">
  <div>{dow_html}</div>
  <div>{hour_html}</div>
</div>

{pos_html}

{monthly_html}

<h2>Trade Log</h2>
{log_html}

</body></html>"""
    return html


def render():
    st.title("📊 Trade Analysis")

    # ── Session state ─────────────────────────────────────────────────────────
    for _k, _v in {
        'ta_df':          None, 'ta_format': None,
        'ta_accounts':    [], 'ta_ic_bytes': None,
        'ta_df_original': None,
        'ta_df_edited':   None,
        'ta_group_summary': None,
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
                st.session_state['ta_df']          = df
                st.session_state['ta_df_original'] = df.copy()
                st.session_state['ta_format']      = fmt
                st.session_state['ta_accounts']    = []
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
                        st.session_state['ta_df']          = df_ic
                        st.session_state['ta_df_original'] = df_ic.copy()
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

    df_all   = st.session_state['ta_df']
    df_edited = st.session_state.get('ta_df_edited')
    fmt       = st.session_state['ta_format']

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

    # ── View selector ─────────────────────────────────────────────────────────
    has_edited = st.session_state.get('ta_df_edited') is not None
    if has_edited:
        view_opts = ["Original", "Edited", "Both"]
        view_sel  = st.radio("View", view_opts, horizontal=True, key='ta_view_sel')
    else:
        view_sel = "Original"
        st.session_state['ta_view_sel'] = "Original" 

    # ── Filters ───────────────────────────────────────────────────────────────
    st.divider()
    if 'ta_deposit' not in st.session_state:
        st.session_state['ta_deposit'] = 10000.0

    fc1, fc2, fc3, fc4, fc5 = st.columns(5)

    with fc1:
        valid_times = df_all['open_time'].dropna()
        date_min  = valid_times.min().date()
        date_max  = valid_times.max().date()
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
        trade_nums = [str(i) for i in range(1, len(df_all)+1)]
        sel_trades = st.multiselect("Trade #", trade_nums, key='ta_idx_sel',
                                    placeholder="All trades (filter by #)")

    with fc5:
        st.session_state['ta_deposit'] = st.number_input(
            "Initial Balance ($)", min_value=100.0, max_value=10_000_000.0,
            value=st.session_state.get('ta_deposit', 10000.0),
            step=1000.0, format="%.0f", key='ta_deposit_filter',
            help="Used for % calculations in monthly table and report")


    # Apply filters
    def _apply_filters(src_df):
        d = src_df.copy()
        d = d[(d['open_time'].dt.date >= date_from) &
              (d['open_time'].dt.date <= date_to)]
        if sel_symbol:   d = d[d['symbol'].isin(sel_symbol)]
        if sel_strategy: d = d[d['strategy'].isin(sel_strategy)]
        if sel_days:     d = d[d['day_of_week'].isin(sel_days)]
        if sel_type:     d = d[d['type'].isin(sel_type)]
        d = d.reset_index(drop=True)
        if sel_trades:
            sel_idx = [int(t)-1 for t in sel_trades if int(t)-1 < len(d)]
            d = d.iloc[sel_idx].reset_index(drop=True)
        return d

    df = _apply_filters(df_all)

    # Also prepare edited df if available
    df_e = _apply_filters(df_edited) if df_edited is not None else None

    st.caption(f"Showing **{len(df)}** trades after filters")

    # ── Analysis mode ─────────────────────────────────────────────────────────
    mode = st.radio(
        "Analysis mode",
        ["Overall", "By Strategy", "By Symbol", "By Day of Week"],
        horizontal=True, key='ta_mode'
    )
    st.divider()

    # ── Helpers ───────────────────────────────────────────────────────────────
    def render_stats(stats, label="", stats_compare=None):
        if label:
            st.markdown(f"**{label}**")

        def _delta(key, fmt='$', higher_is_better=True):
            """Return delta string for st.metric when compare stats available."""
            if stats_compare is None or key not in stats_compare:
                return None
            diff = stats_compare[key] - stats[key]
            if diff == 0:
                return None
            if fmt == '$':
                return f"${diff:+,.2f}"
            elif fmt == '%':
                return f"{diff:+.1f}%"
            elif fmt == 'x':
                return f"{diff:+.2f}"
            else:
                return f"{diff:+g}"

        def _inv_delta(key, fmt='$'):
            """Delta where lower is better (e.g. drawdown, losses)."""
            if stats_compare is None or key not in stats_compare:
                return None
            diff = stats_compare[key] - stats[key]
            if diff == 0:
                return None
            if fmt == '$':
                return f"${diff:+,.2f}"
            elif fmt == '%':
                return f"{diff:+.1f}%"
            else:
                return f"{diff:+g}"

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Net Profit",    f"${stats['net_profit']:,.2f}",
                  delta=_delta('net_profit','$'))
        c2.metric("Win Rate",      f"{stats['win_rate']}%",
                  delta=_delta('win_rate','%'))
        c3.metric("Profit Factor", f"{stats['profit_factor']}",
                  delta=_delta('profit_factor','x'))
        c4.metric("R:R Ratio",     f"{stats['rr_ratio']}",
                  delta=_delta('rr_ratio','x'))
        c5.metric("Expectancy",    f"${stats['expectancy']:,.2f}",
                  delta=_delta('expectancy','$'))

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Trades",  stats['total_trades'],
                  delta=_delta('total_trades',''))
        c2.metric("Avg Win",       f"${stats['avg_win']:,.2f}",
                  delta=_delta('avg_win','$'))
        c3.metric("Avg Loss",      f"${stats['avg_loss']:,.2f}",
                  delta=_inv_delta('avg_loss','$'), delta_color="inverse")
        _dd_pct = stats.get('max_drawdown_pct', 0)
        c4.metric("Max DD",        f"${stats['max_drawdown']:,.2f} ({abs(_dd_pct):.2f}%)",
                  delta=_inv_delta('max_drawdown','$'), delta_color="inverse")
        c5.metric("Best Trade",    f"${stats['best_trade']:,.2f}",
                  delta=_delta('best_trade','$'))

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Max Consec Wins",   stats['max_consec_wins'],
                  delta=_delta('max_consec_wins',''))
        c2.metric("Max Consec Losses", stats['max_consec_losses'],
                  delta=_inv_delta('max_consec_losses',''), delta_color="inverse")
        c3.metric("Avg Win Dur",       f"{stats['avg_win_duration']}m",
                  delta=_delta('avg_win_duration',''))
        c4.metric("Avg Loss Dur",      f"{stats['avg_loss_duration']}m",
                  delta=_inv_delta('avg_loss_duration',''), delta_color="inverse")
        c5.metric("Worst Trade",       f"${stats['worst_trade']:,.2f}",
                  delta=_inv_delta('worst_trade','$'), delta_color="inverse")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Long Trades",   stats['long_trades'],
                  delta=_delta('long_trades',''))
        c2.metric("Long Win Rate", f"{stats['long_win_rate']}%",
                  delta=_delta('long_win_rate','%'))
        c3.metric("Short Trades",  stats['short_trades'],
                  delta=_delta('short_trades',''))
        c4.metric("Short Win Rate",f"{stats['short_win_rate']}%",
                  delta=_delta('short_win_rate','%'))

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Trading Days",    stats.get('trading_days', 0),
                  delta=_delta('trading_days',''))
        c2.metric("Trades / Day",    f"{stats.get('trades_per_day', 0)}",
                  delta=_delta('trades_per_day','x'))

    def render_equity_curve(df_plot, label="Equity Curve", df_compare=None, compare_label="Edited"):
        import pandas as pd
        df_s = df_plot.sort_values('close_time').copy()

        COLORS = ['#7c6af7','#34C27A','#F5A623','#E05555','#4C8EF5',
                  '#A78BFA','#22D3EE','#FB923C','#F472B6','#86EFAC']

        safe_key = label.replace(" ","_").replace("/","_").replace("—","").strip("_")
        ov = st.columns(4)
        show_account  = ov[0].checkbox("Account total",  value=True,  key=f"eq_acc_{safe_key}")
        show_strategy = ov[1].checkbox("By Strategy",    value=False, key=f"eq_str_{safe_key}")
        show_symbol   = ov[2].checkbox("By Symbol",      value=False, key=f"eq_sym_{safe_key}")
        show_dow      = ov[3].checkbox("By Day of Week", value=False, key=f"eq_dow_{safe_key}")

        fig = go.Figure()

        if show_account:
            df_s['_cum'] = df_s['net_profit'].cumsum()
            fig.add_trace(go.Scatter(
                x=df_s['close_time'], y=df_s['_cum'], mode='lines', name='Original',
                line=dict(color='#7c6af7', width=2),
                fill='tozeroy', fillcolor='rgba(124,106,247,0.06)',
            ))
            # Overlay edited/compare line if provided
            if df_compare is not None:
                df_c = df_compare.sort_values('close_time').copy()
                df_c['_cum'] = df_c['net_profit'].cumsum()
                fig.add_trace(go.Scatter(
                    x=df_c['close_time'], y=df_c['_cum'], mode='lines',
                    name=compare_label,
                    line=dict(color='#34C27A', width=2, dash='dash'),
                    fill='tozeroy', fillcolor='rgba(52,194,122,0.04)',
                ))

        if show_strategy and 'strategy' in df_s.columns:
            for i, strat in enumerate(sorted(df_s['strategy'].dropna().unique())):
                sub = df_s[df_s['strategy']==strat].copy()
                sub['_cum'] = sub['net_profit'].cumsum()
                fig.add_trace(go.Scatter(
                    x=sub['close_time'], y=sub['_cum'], mode='lines', name=strat,
                    line=dict(color=COLORS[(i+1)%len(COLORS)], width=1.5, dash='dot'),
                ))

        if show_symbol and 'symbol' in df_s.columns:
            for i, sym in enumerate(sorted(df_s['symbol'].dropna().unique())):
                sub = df_s[df_s['symbol']==sym].copy()
                sub['_cum'] = sub['net_profit'].cumsum()
                fig.add_trace(go.Scatter(
                    x=sub['close_time'], y=sub['_cum'], mode='lines', name=sym,
                    line=dict(color=COLORS[(i+2)%len(COLORS)], width=1.5, dash='dash'),
                ))

        if show_dow and 'day_of_week' in df_s.columns:
            for i, dow in enumerate(['Monday','Tuesday','Wednesday','Thursday','Friday']):
                sub = df_s[df_s['day_of_week']==dow].copy()
                if sub.empty: continue
                sub['_cum'] = sub['net_profit'].cumsum()
                fig.add_trace(go.Scatter(
                    x=sub['close_time'], y=sub['_cum'], mode='lines', name=dow,
                    line=dict(color=COLORS[(i+3)%len(COLORS)], width=1.5),
                ))

        fig.update_layout(
            title=label, height=360,
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            font=dict(family='sans-serif'),
            xaxis=dict(gridcolor='rgba(128,128,128,0.15)', showgrid=True),
            yaxis=dict(gridcolor='rgba(128,128,128,0.15)', tickprefix='$', showgrid=True),
            margin=dict(l=60, r=20, t=40, b=40),
            legend=dict(bgcolor='rgba(0,0,0,0)', borderwidth=0),
            hovermode='x unified',
        )
        st.plotly_chart(fig, use_container_width=True, key=f"eq_fig_{safe_key}")

        # ── Drawdown panel ────────────────────────────────────────────────
        st.markdown("**Drawdown**")
        df_s['_cum2'] = df_s['net_profit'].cumsum()
        df_s['_peak'] = df_s['_cum2'].cummax()
        df_s['_dd']   = df_s['_cum2'] - df_s['_peak']
        fig_dd = go.Figure()
        fig_dd.add_trace(go.Scatter(
            x=df_s['close_time'], y=df_s['_dd'],
            mode='lines', fill='tozeroy',
            line=dict(color='rgba(124,106,247,0.8)', width=1.5),
            fillcolor='rgba(124,106,247,0.08)', name='DD Original',
        ))
        if df_compare is not None:
            df_c2 = df_compare.sort_values('close_time').copy()
            df_c2['_cum2'] = df_c2['net_profit'].cumsum()
            df_c2['_peak'] = df_c2['_cum2'].cummax()
            df_c2['_dd']   = df_c2['_cum2'] - df_c2['_peak']
            fig_dd.add_trace(go.Scatter(
                x=df_c2['close_time'], y=df_c2['_dd'],
                mode='lines', fill='tozeroy',
                line=dict(color='rgba(220,80,80,0.8)', width=1.5),
                fillcolor='rgba(220,80,80,0.08)', name=f'DD {compare_label}',
            ))
        fig_dd.update_layout(
            height=130,
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            font=dict(family='sans-serif'),
            xaxis=dict(gridcolor='rgba(128,128,128,0.15)', showgrid=True,
                       showticklabels=False),
            yaxis=dict(gridcolor='rgba(128,128,128,0.15)', tickprefix='$',
                       showgrid=True),
            margin=dict(l=60, r=20, t=8, b=4),
            showlegend=False,
        )
        st.plotly_chart(fig_dd, use_container_width=True, key=f"eq_dd_{safe_key}")

        # ── Daily P&L bars ────────────────────────────────────────────────
        st.markdown("**Daily P&L**")
        daily = (df_s.groupby(df_s['close_time'].dt.date)['net_profit']
                 .sum().reset_index())
        daily.columns = ['date','pnl']
        daily['color'] = daily['pnl'].apply(
            lambda v: 'rgba(52,194,122,0.85)' if v >= 0 else 'rgba(220,80,80,0.85)')

        fig_d = go.Figure()
        fig_d.add_trace(go.Bar(
            x=daily['date'], y=daily['pnl'],
            marker_color=daily['color'],
            name='Original',
            offsetgroup=0,
        ))

        if df_compare is not None:
            dc = df_compare.sort_values('close_time').copy()
            daily_c = (dc.groupby(dc['close_time'].dt.date)['net_profit']
                       .sum().reset_index())
            daily_c.columns = ['date','pnl']
            daily_c['color'] = daily_c['pnl'].apply(
                lambda v: 'rgba(124,106,247,0.45)' if v >= 0 else 'rgba(255,165,0,0.45)')
            fig_d.add_trace(go.Bar(
                x=daily_c['date'], y=daily_c['pnl'],
                marker_color=daily_c['color'],
                name=compare_label,
                offsetgroup=1,
            ))

        fig_d.update_layout(
            height=160, barmode='group',
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            font=dict(family='sans-serif'),
            xaxis=dict(gridcolor='rgba(128,128,128,0.15)', showgrid=True),
            yaxis=dict(gridcolor='rgba(128,128,128,0.15)', tickprefix='$',
                       showgrid=True, zeroline=True,
                       zerolinecolor='rgba(128,128,128,0.4)'),
            margin=dict(l=60, r=20, t=4, b=40),
            legend=dict(bgcolor='rgba(0,0,0,0)', orientation='h',
                        yanchor='bottom', y=1.02),
            showlegend=df_compare is not None,
        )
        st.plotly_chart(fig_d, use_container_width=True, key=f"eq_daily_{safe_key}")

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
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            font=dict(family='sans-serif'), margin=dict(l=60, r=20, t=40, b=40),
            xaxis=dict(gridcolor='rgba(128,128,128,0.15)'),
            yaxis=dict(gridcolor='rgba(128,128,128,0.15)', tickprefix='$'),
            legend=dict(bgcolor='rgba(0,0,0,0)')
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
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            font=dict(family='sans-serif'), margin=dict(l=60, r=20, t=40, b=40),
            xaxis=dict(gridcolor='rgba(128,128,128,0.15)', title='Hour (UTC)'),
            yaxis=dict(gridcolor='rgba(128,128,128,0.15)', tickprefix='$'),
            legend=dict(bgcolor='rgba(0,0,0,0)')
        )
        st.plotly_chart(fig, use_container_width=True)

    def render_monthly_table(df_plot, label="Monthly Performance", key_prefix="mt"):
        import pandas as pd
        if 'close_time' not in df_plot.columns or df_plot.empty:
            return
        tmp = df_plot[['close_time','net_profit']].dropna().copy()
        tmp['year']  = pd.to_datetime(tmp['close_time']).dt.year
        tmp['month'] = pd.to_datetime(tmp['close_time']).dt.month
        monthly = tmp.groupby(['year','month'])['net_profit'].sum().reset_index()
        if monthly.empty:
            return

        pivot = monthly.pivot(index='year', columns='month', values='net_profit').fillna(0)
        pivot.columns = [pd.Timestamp(2000, int(m), 1).strftime('%b') for m in pivot.columns]
        pivot['YTD']  = pivot.sum(axis=1)
        pivot = pivot.sort_index(ascending=False)

        deposit = st.session_state.get('ta_deposit', 10000.0)
        toggle  = st.radio("Unit", ["$", "%"], horizontal=True, key=f"{key_prefix}_toggle")

        month_order = ['Jan','Feb','Mar','Apr','May','Jun',
                       'Jul','Aug','Sep','Oct','Nov','Dec','YTD']
        cols_present = [c for c in month_order if c in pivot.columns]
        display = pivot[cols_present].copy()

        if toggle == "%":
            display = (display / deposit * 100).round(2)

        # Build HTML table with colour coding
        def _cell(val, fmt):
            if val > 0:  bg = "rgba(52,194,122,0.18)"; fg = "#34C27A"
            elif val < 0: bg = "rgba(220,80,80,0.18)";  fg = "#E05555"
            else:         bg = "transparent";             fg = "#888"
            txt = f"{val:+.2f}{'%' if fmt=='%' else ''}" if val != 0 else "—"
            return f'<td style="background:{bg};color:{fg};padding:5px 10px;text-align:right;font-size:12px;font-family:monospace;border-bottom:1px solid rgba(128,128,128,0.1)">{txt}</td>'

        rows = []
        for year, row in display.iterrows():
            cells = [f'<td style="padding:5px 10px;font-size:12px;font-weight:600;border-bottom:1px solid rgba(128,128,128,0.1)">{year}</td>']
            for col in cols_present:
                cells.append(_cell(row.get(col, 0), toggle))
            rows.append("<tr>" + "".join(cells) + "</tr>")

        hdr_cells = ["<th style='padding:5px 10px;font-size:11px;color:#888;text-align:right;border-bottom:1px solid rgba(128,128,128,0.2)'>Year</th>"]
        for col in cols_present:
            hdr_cells.append(f"<th style='padding:5px 10px;font-size:11px;color:#888;text-align:right;border-bottom:1px solid rgba(128,128,128,0.2)'>{col}</th>")

        html = (
            "<div style='overflow-x:auto'>"
            "<table style='width:100%;border-collapse:collapse'>"
            "<thead><tr>" + "".join(hdr_cells) + "</tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody>"
            "</table></div>"
        )
        if label:
            st.markdown(f"**{label}**")
        st.markdown(html, unsafe_allow_html=True)

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
        stats   = calc_stats(df, deposit=st.session_state.get("ta_deposit", 0.0))
        stats_e = calc_stats(df_e, deposit=st.session_state.get("ta_deposit", 0.0)) if df_e is not None else None

        # ── Report download ───────────────────────────────────────────────
        _rep_df    = df_e if (view_sel in ("Edited","Both") and df_e is not None) else df
        _rep_stats = stats_e if (view_sel == "Edited" and stats_e) else stats
        _rep_cmp_s = stats_e if (view_sel == "Both" and stats_e) else None
        _rep_cmp_d = df_e    if (view_sel == "Both" and df_e is not None) else None
        _rep_grp   = st.session_state.get('ta_group_summary') if view_sel in ("Edited","Both") else None
        try:
            from datetime import datetime as _dt
            _rep_html = _generate_html_report(
                _rep_df, _rep_stats, fmt or '', view_sel,
                stats_compare=_rep_cmp_s, df_compare=_rep_cmp_d,
                group_summary=_rep_grp,
                date_from=str(date_from), date_to=str(date_to),
                deposit=st.session_state.get('ta_deposit', 10000.0),
            )
            st.download_button(
                "📄 Download HTML Report",
                data      = _rep_html,
                file_name = f"trade_report_{view_sel.lower()}_{_dt.now().strftime('%Y%m%d_%H%M')}.html",
                mime      = 'text/html',
                key       = 'ta_report_dl',
            )
        except Exception as _e:
            import traceback
            st.error(f"Report generation error: {_e}")
            st.code(traceback.format_exc())

        if view_sel == "Edited" and stats_e:
            render_stats(stats_e, "Overall Statistics (Edited)")
        elif view_sel == "Both" and stats_e:
            render_stats(stats, "Overall Statistics", stats_compare=stats_e)
        else:
            render_stats(stats, "Overall Statistics")
        if view_sel == "Both" and df_e is not None:
            render_equity_curve(df, label="Equity Curve", df_compare=df_e,
                                compare_label="Edited")
        elif view_sel == "Edited" and df_e is not None:
            render_equity_curve(df_e, label="Equity Curve (Edited)")
        else:
            render_equity_curve(df)
        _df_charts = df_e if (view_sel in ("Edited","Both") and df_e is not None) else df
        col1, col2 = st.columns(2)
        with col1:
            render_dow_chart(_df_charts)
        with col2:
            render_hour_chart(_df_charts)
        st.divider()
        # ── Grouped trades summary (collapsible) ──────────────────────────
        group_summary = st.session_state.get('ta_group_summary')
        if group_summary and view_sel in ("Edited", "Both"):
            import pandas as _pd
            n_groups = len([r for r in group_summary if r['Group'] != '—'])
            n_single = len([r for r in group_summary if r['Group'] == '—'])
            with st.expander(
                f"Position Summary — {len(group_summary)} positions "
                f"({n_groups} grouped, {n_single} individual)", expanded=False):
                st.caption("Grouped positions show merged entries. Individual trades show as single rows. Sorted by open time.")
                gs_df = _pd.DataFrame(group_summary)
                def _colour_pnl(val):
                    try:
                        v = float(val)
                        if v > 0: return 'background-color: rgba(52,194,122,0.15)'
                        if v < 0: return 'background-color: rgba(220,80,80,0.15)'
                    except: pass
                    return ''
                st.dataframe(
                    gs_df.style.map(_colour_pnl, subset=['Net P&L ($)']),
                    use_container_width=True, hide_index=True
                )
        if view_sel == "Both" and df_e is not None:
            render_monthly_table(df,  "Monthly Performance (Original)", key_prefix="mt_overall_orig")
            render_monthly_table(df_e,"Monthly Performance (Edited)",   key_prefix="mt_overall_edit")
        else:
            render_monthly_table(_df_charts, "Monthly Performance", key_prefix="mt_overall")

    elif mode == "By Strategy":
        strats = sorted(df['strategy'].dropna().unique().tolist())
        if not strats:
            st.info("No strategies found")
        else:
            st.subheader("Strategy Comparison")
            _df_s = df_e if (view_sel in ("Edited","Both") and df_e is not None) else df
            rows = []
            for s in strats:
                sdf  = _df_s[_df_s['strategy'] == s] if s in _df_s['strategy'].values else df[df['strategy']==s]
                stat = calc_stats(sdf)
                row  = {
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
                }
                if view_sel == "Both" and df_e is not None:
                    sdf_e = df_e[df_e['strategy'] == s] if s in df_e['strategy'].values else None
                    if sdf_e is not None and len(sdf_e):
                        stat_e = calc_stats(sdf_e)
                        def _arr(k, higher=''):
                            diff = stat_e[k] - stat[k]
                            if abs(diff) < 0.001: return ''
                            arrow = '▲' if diff > 0 else '▼'
                            color = 'green' if (diff > 0) == (k not in ('max_drawdown','max_consec_losses')) else 'red'
                            return f" {arrow}{abs(diff):.2f}"
                        row['Net Profit']    = f"{stat['net_profit']:.2f}{_arr('net_profit')}"
                        row['Win Rate %']    = f"{stat['win_rate']}{_arr('win_rate')}"
                        row['Profit Factor'] = f"{stat['profit_factor']}{_arr('profit_factor')}"
                        row['Expectancy']    = f"{stat['expectancy']:.2f}{_arr('expectancy')}"
                        row['Max DD']        = f"{stat['max_drawdown']:.2f}{_arr('max_drawdown')}"
                rows.append(row)
            import pandas as pd
            sdf_sum = pd.DataFrame(rows).sort_values('Net Profit', ascending=False)
            st.dataframe(sdf_sum, use_container_width=True, hide_index=True)
            st.divider()
            sel = st.selectbox("Select strategy for detail", strats)
            if sel:
                sdf  = _df_s[_df_s['strategy'] == sel] if sel in _df_s['strategy'].values else df[df['strategy']==sel]
                stat = calc_stats(sdf)
                sdf_e_sel = df_e[df_e['strategy']==sel] if (df_e is not None and sel in df_e['strategy'].values) else None
                stats_e_sel = calc_stats(sdf_e_sel) if sdf_e_sel is not None and len(sdf_e_sel) else None
                if view_sel == "Both" and stats_e_sel:
                    render_stats(stat, sel, stats_compare=stats_e_sel)
                elif view_sel == "Edited" and stats_e_sel:
                    render_stats(stats_e_sel, f"{sel} (Edited)")
                else:
                    render_stats(stat, sel)
                render_equity_curve(sdf, f"{sel} — Equity Curve")
                col1, col2 = st.columns(2)
                with col1: render_dow_chart(sdf)
                with col2: render_hour_chart(sdf)
                st.divider()
                if view_sel == "Both" and sdf_e_sel is not None and len(sdf_e_sel):
                    render_monthly_table(sdf,     "Monthly Performance (Original)", key_prefix=f"mt_strat_orig_{sel}")
                    render_monthly_table(sdf_e_sel,"Monthly Performance (Edited)",  key_prefix=f"mt_strat_edit_{sel}")
                else:
                    render_monthly_table(sdf, "Monthly Performance", key_prefix=f"mt_strat_{sel}")

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
        import pandas as pd
        sdf_sum = pd.DataFrame(rows).sort_values('Net Profit', ascending=False)
        st.dataframe(
            sdf_sum.style.map(colour_profit, subset=['Net Profit', 'Expectancy', 'Max DD']),
            use_container_width=True, hide_index=True
        )
        _df_sym = df_e if (view_sel in ("Edited","Both") and df_e is not None) else df
        sel = st.selectbox("Select symbol for detail", syms)
        if sel:
            sdf  = _df_sym[_df_sym['symbol'] == sel] if sel in _df_sym['symbol'].values else df[df['symbol']==sel]
            stat = calc_stats(sdf)
            sdf_e_sel = df_e[df_e['symbol']==sel] if (df_e is not None and sel in df_e['symbol'].values) else None
            stats_e_sel = calc_stats(sdf_e_sel) if sdf_e_sel is not None and len(sdf_e_sel) else None
            if view_sel == "Both" and stats_e_sel:
                render_stats(stat, sel, stats_compare=stats_e_sel)
            elif view_sel == "Edited" and stats_e_sel:
                render_stats(stats_e_sel, f"{sel} (Edited)")
            else:
                render_stats(stat, sel)
            render_equity_curve(sdf, f"{sel} — Equity Curve")
            col1, col2 = st.columns(2)
            with col1: render_dow_chart(sdf)
            with col2: render_hour_chart(sdf)
            st.divider()
            if view_sel == "Both" and sdf_e_sel is not None and len(sdf_e_sel):
                render_monthly_table(sdf,      "Monthly Performance (Original)", key_prefix=f"mt_sym_orig_{sel}")
                render_monthly_table(sdf_e_sel,"Monthly Performance (Edited)",   key_prefix=f"mt_sym_edit_{sel}")
            else:
                render_monthly_table(sdf, "Monthly Performance", key_prefix=f"mt_sym_{sel}")

    elif mode == "By Day of Week":
        _df_dow = df_e if (view_sel in ("Edited","Both") and df_e is not None) else df
        render_dow_chart(_df_dow)
        render_hour_chart(_df_dow)

    # ── Raw trade log ─────────────────────────────────────────────────────────
    st.divider()
    with st.expander("Raw Trade Log"):
        edit_cols = ['open_time', 'close_time', 'symbol', 'type', 'strategy',
                     'volume', 'open_price', 'close_price', 'sl', 'tp',
                     'commission', 'swap', 'profit', 'net_profit', 'duration_min']
        edit_cols = [c for c in edit_cols if c in st.session_state['ta_df'].columns]

        # Show full dataset (not filtered) with trade index and Group column
        df_edit = st.session_state['ta_df'][edit_cols].copy()
        df_edit.insert(0, '#', range(1, len(df_edit) + 1))
        # Preserve existing Group column if already edited
        existing_edited = st.session_state.get('ta_df_edited')
        if existing_edited is not None and 'Group' in existing_edited.columns:
            df_edit.insert(1, 'Group', existing_edited['Group'].values[:len(df_edit)])
        else:
            df_edit.insert(1, 'Group', '')

        st.caption(
            "Edit any cell then click **Update**. "
            "Enter the same label in **Group** for trades to merge into one position. "
            "**Reset** restores the original upload."
        )
        bc1, bc2, bc3 = st.columns([1, 1, 6])
        do_update = bc1.button("✅ Update", type="primary", key="ta_log_update")
        do_reset  = bc2.button("↩️ Reset",  key="ta_log_reset")

        edited = st.data_editor(
            df_edit,
            use_container_width=True,
            hide_index=True,
            height=400,
            column_config={
                '#':           st.column_config.NumberColumn('#', disabled=True, width='small'),
                'Group':       st.column_config.TextColumn('Group', width='small',
                               help='Same label = merge into one trade on Update'),
                'open_time':   st.column_config.DatetimeColumn('open_time', format='YYYY-MM-DD HH:mm:ss'),
                'close_time':  st.column_config.DatetimeColumn('close_time', format='YYYY-MM-DD HH:mm:ss'),
                'symbol':      st.column_config.TextColumn('symbol'),
                'type':        st.column_config.SelectboxColumn('type', options=['buy','sell']),
                'strategy':    st.column_config.TextColumn('strategy'),
                'volume':      st.column_config.NumberColumn('volume', format='%.2f'),
                'open_price':  st.column_config.NumberColumn('open_price', format='%.5f'),
                'close_price': st.column_config.NumberColumn('close_price', format='%.5f'),
                'profit':      st.column_config.NumberColumn('profit', format='%.2f'),
                'net_profit':  st.column_config.NumberColumn('net_profit', format='%.2f'),
            },
            key='ta_log_editor'
        )

        if do_update:
            import pandas as pd
            upd = edited.drop(columns=['#'])
            for col in ['open_time','close_time']:
                if col in upd.columns:
                    upd[col] = pd.to_datetime(upd[col], errors='coerce')
            for col in ['profit','net_profit','volume','open_price','close_price',
                        'commission','swap','sl','tp','duration_min']:
                if col in upd.columns:
                    upd[col] = pd.to_numeric(upd[col], errors='coerce')

            # ── Merge grouped trades ──────────────────────────────────────
            upd_with_groups = upd.copy()  # preserve Group labels for summary
            groups = upd['Group'].fillna('').str.strip()
            ungrouped = upd[groups == ''].drop(columns=['Group'])
            grouped_rows = []
            for label, grp in upd[groups != ''].groupby(groups):
                merged = {
                    'open_time':   grp['open_time'].min(),
                    'close_time':  grp['close_time'].max(),
                    'symbol':      grp['symbol'].iloc[0],
                    'type':        grp['type'].iloc[0],
                    'strategy':    grp['strategy'].iloc[0],
                    'volume':      grp['volume'].sum(),
                    'open_price':  grp['open_price'].iloc[0],
                    'close_price': grp['close_price'].iloc[-1],
                    'profit':      grp['profit'].sum() if 'profit' in grp else 0,
                    'net_profit':  grp['net_profit'].sum(),
                    'commission':  grp['commission'].sum() if 'commission' in grp else 0,
                    'swap':        grp['swap'].sum() if 'swap' in grp else 0,
                }
                if 'sl' in grp: merged['sl'] = grp['sl'].iloc[0]
                if 'tp' in grp: merged['tp'] = grp['tp'].iloc[0]
                grouped_rows.append(merged)

            if grouped_rows:
                df_grouped = pd.DataFrame(grouped_rows)
                upd = pd.concat([ungrouped, df_grouped], ignore_index=True)
                upd = upd.sort_values('open_time').reset_index(drop=True)
            else:
                upd = ungrouped

            upd['duration_min'] = ((upd['close_time'] - upd['open_time'])
                                   .dt.total_seconds() / 60).round(1)
            upd['win']         = upd['net_profit'] > 0
            upd['day_of_week'] = upd['open_time'].dt.day_name()
            upd['hour']        = upd['open_time'].dt.hour
            # Preserve non-editable columns
            orig = st.session_state['ta_df']
            for col in orig.columns:
                if col not in upd.columns:
                    upd[col] = orig[col].values[:len(upd)]
            upd['comment']   = upd.get('comment', '')
            upd['source']    = upd.get('source', 'manual')
            st.session_state['ta_df_edited'] = upd

            # Build full position summary — grouped and ungrouped trades
            summary_rows = []
            grp_labels = upd_with_groups['Group'].fillna('').str.strip()

            # Add 1-based index to upd_with_groups for trade # reference
            upd_with_groups = upd_with_groups.reset_index(drop=True)
            upd_with_groups['_idx'] = range(1, len(upd_with_groups) + 1)

            # Grouped trades first
            for label, grp in upd_with_groups[grp_labels != ''].groupby(grp_labels[grp_labels != '']):
                net      = grp['net_profit'].sum()
                trade_nums = ', '.join(str(i) for i in sorted(grp['_idx'].tolist()))
                summary_rows.append({
                    'Trade #':      trade_nums,
                    'Group':        label,
                    'Entries':      len(grp),
                    'Symbol':       grp['symbol'].iloc[0],
                    'Type':         grp['type'].iloc[0],
                    'Open Time':    grp['open_time'].min(),
                    'Close Time':   grp['close_time'].max(),
                    'Total Volume': round(grp['volume'].sum(), 2),
                    'Net P&L ($)':  round(net, 2),
                    'Win':          '✅' if net > 0 else '❌',
                })

            # Individual (ungrouped) trades
            for _, row in upd_with_groups[grp_labels == ''].iterrows():
                net = row['net_profit']
                summary_rows.append({
                    'Trade #':      str(int(row['_idx'])),
                    'Group':        '—',
                    'Entries':      1,
                    'Symbol':       row['symbol'],
                    'Type':         row['type'],
                    'Open Time':    row['open_time'],
                    'Close Time':   row['close_time'],
                    'Total Volume': round(row['volume'], 2),
                    'Net P&L ($)':  round(net, 2),
                    'Win':          '✅' if net > 0 else '❌',
                })

            # Sort by open time
            summary_rows.sort(key=lambda r: r['Open Time'] if r['Open Time'] is not None else pd.Timestamp.min)
            st.session_state['ta_group_summary'] = summary_rows if summary_rows else None

            n_merged = len(groups[groups != ''].unique())
            st.success(
                f"Saved — {len(upd)} trades "
                f"({n_merged} group(s) merged). "
                "Select 'Edited' or 'Both' to compare."
            )
            st.rerun()

        if do_reset:
            st.session_state['ta_df_edited']    = None
            st.session_state['ta_group_summary'] = None
            st.success("Edited version cleared.")
            st.rerun()

        def colour_net(val):
            try:
                v = float(val)
                if v > 0: return 'background-color: rgba(0,180,0,0.12)'
                if v < 0: return 'background-color: rgba(180,0,0,0.12)'
            except:
                pass
            return ''

        st.divider()
        # Download uses edited version if available, otherwise original
        _dl_df = st.session_state['ta_df_edited'] if st.session_state.get('ta_df_edited') is not None else st.session_state['ta_df']
        _dl_cols = [c for c in edit_cols if c in _dl_df.columns]
        _dl_label = "Edited" if st.session_state.get('ta_df_edited') is not None else "Original"
        st.download_button(
            f"⬇ Download {_dl_label} trades CSV",
            data      = _dl_df[_dl_cols].to_csv(index=False),
            file_name = f"mt5_trades_{date_from}_{date_to}.csv",
            mime      = 'text/csv'
        )