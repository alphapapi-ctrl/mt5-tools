"""
view_batch_backtest.py
======================
Batch Backtest runner page for MT5 Tools dashboard.
Reads config from mt5_batch_config.json.
Runs backtests in a background thread with live progress updates.
"""

import streamlit as st
import os
import sys
import glob
import json
import shutil
import subprocess
import time
import threading
import queue
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Constants ─────────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mt5_batch_config.json')

PERIOD_MAP = {
    'M1': 'M1', 'M5': 'M5', 'M15': 'M15', 'M30': 'M30',
    'H1': 'H1', 'H4': 'H4',
    'D': 'Daily', 'D1': 'Daily', 'DAILY': 'Daily',
    'W1': 'Weekly', 'MN': 'Monthly',
}

MODEL_LABELS = {
    '1': 'OHLC',
    '2': 'CTRLPTS',
    '4': 'EVERYTICK',
    '5': 'EVERYTICKREAL',
}

SUFFIX = '.a'


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config():
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return None


def read_utf16(path):
    with open(path, 'rb') as f:
        raw = f.read()
    if raw[:2] == b'\xff\xfe':
        return raw[2:].decode('utf-16-le').splitlines(), 'utf-16-le'
    elif raw[:2] == b'\xfe\xff':
        return raw[2:].decode('utf-16-be').splitlines(), 'utf-16-be'
    return raw.decode('utf-8', errors='replace').splitlines(), 'utf-8' 


def write_utf16(path, lines, encoding='utf-16-le'):
    text = '\r\n'.join(lines) + '\r\n'
    with open(path, 'wb') as f:
        if encoding == 'utf-16-le':
            f.write(b'\xff\xfe'); f.write(text.encode('utf-16-le'))
        elif encoding == 'utf-16-be':
            f.write(b'\xfe\xff'); f.write(text.encode('utf-16-be'))
        else:
            f.write(text.encode('utf-8'))


def detect_timeframe(filename):
    name = os.path.splitext(filename)[0].upper()
    for token in PERIOD_MAP:
        if name.endswith('_' + token) or name.endswith('-' + token) or \
           ('_' + token + '_') in name or ('-' + token + '-') in name:
            return PERIOD_MAP[token]
    return None


def detect_instrument(filename, n_chars):
    return os.path.splitext(filename)[0][:n_chars].upper()


def _clear_use_default_flags(lines):
    """Clear the UseDefault bit (bit 2) from parameter flags so MT5 uses the set file value."""
    result = []
    for line in lines:
        stripped = line.strip()
        if '=' in stripped and ',' not in stripped.split('=')[0] and '||' in stripped:
            parts = stripped.split('||')
            try:
                flag = int(float(parts[1]))
                if flag & 4:
                    parts[1] = str(flag & ~4)
                    line = '||'.join(parts)
            except (ValueError, TypeError, IndexError):
                pass
        result.append(line)
    return result


def update_set_file(set_path, ea_comment, lot_mode, lot_value):
    lines, encoding = read_utf16(set_path)
    lines = _clear_use_default_flags(lines)

    def update_param(lines, key, new_val):
        for i, line in enumerate(lines):
            if line.strip().startswith(key + '='):
                parts = line.strip().split('||')
                parts[0] = f'{key}={new_val}'
                lines[i] = '||'.join(parts)
                return True
        lines.append(f'{key}={new_val}')
        return False

    updated = False
    for i, line in enumerate(lines):
        if line.strip().startswith('EA_Comment='):
            lines[i] = f'EA_Comment={ea_comment}'
            updated = True
            break
    if not updated:
        lines.append(f'EA_Comment={ea_comment}')

    if lot_mode == 'manual':
        update_param(lines, 'Risk', '0')
        update_param(lines, 'StartLots', str(lot_value))
        # Zero out LotPerBalance_step so balance mode can't bleed through
        for i, line in enumerate(lines):
            if line.strip().startswith('LotPerBalance_step='):
                parts = line.strip().split('||')
                parts[0] = 'LotPerBalance_step=0'
                lines[i] = '||'.join(parts)
                break
    elif lot_mode == 'balance':
        update_param(lines, 'Risk', '9999')
        update_param(lines, 'LotPerBalance_step', str(lot_value))

    write_utf16(set_path, lines, encoding)


def read_set_lines(set_path):
    """Read a .set file and return lines as strings."""
    lines, _ = read_utf16(set_path)
    return lines


def build_ini(symbol, period, set_file_path, ini_out_path, report_name, cfg):
    # Build [Tester] section
    tester_section = (
        '[Tester]\r\n'
        f'Expert={cfg["ea_name"]}\r\n'
        f'Symbol={symbol}\r\n'
        f'Period={period}\r\n'
        f'Optimization={cfg.get("optimization", "0")}\r\n'
        f'Model={cfg["model"]}\r\n'
        f'FromDate={cfg["from_date"]}\r\n'
        f'ToDate={cfg["to_date"]}\r\n'
        'ForwardMode=0\r\n'
        f'Deposit={cfg["deposit"]}\r\n'
        f'Currency={cfg["currency"]}\r\n'
        'ProfitInPips=0\r\n'
        f'Leverage={cfg["leverage"]}\r\n'
        'ExecutionMode=0\r\n'
        'OptimizationCriterion=0\r\n'
        'Visual=0\r\n'
        f'Report={report_name}\r\n'
        'ReplaceReport=1\r\n'
        'ShutdownTerminal=1\r\n'
    )

    # Build [TesterInputs] section directly from set file
    # Embeds parameters in the ini — no separate set file needed
    tester_inputs = '[TesterInputs]\r\n'
    set_lines = read_set_lines(set_file_path)
    for line in set_lines:
        line = line.strip()
        if not line or line.startswith(';'):
            continue
        if '=' in line:
            tester_inputs += line + '\r\n'

    with open(ini_out_path, 'wb') as f:
        f.write((tester_section + tester_inputs).encode('utf-8'))


def get_lot_value_from_file(set_path):
    lines, _ = read_utf16(set_path)
    for line in lines:
        if line.strip().startswith('LotPerBalance_step='):
            parts = line.strip().split('||')
            return parts[0].replace('LotPerBalance_step=', '').strip()
    return None


# ── Background batch runner ───────────────────────────────────────────────────

def run_batch(set_files, set_folder, cfg, report_folder, lot_mode, lot_values,
              instruments, periods, strategy_name, progress_queue):
    """
    Runs in a background thread.
    Sends progress updates via progress_queue as dicts.
    """
    tester_folder  = cfg['tester_folder']
    terminal_path  = cfg['terminal_path']
    out_set_folder = os.path.join(set_folder, '_batch_modified')
    os.makedirs(out_set_folder, exist_ok=True)
    terminal_data = os.path.dirname(tester_folder)

    for idx, set_path in enumerate(set_files):
        filename    = os.path.basename(set_path)
        name_stem   = os.path.splitext(filename)[0]
        symbol      = instruments[idx]
        period      = periods[idx]
        lot_mode_f  = lot_mode
        lot_value   = lot_values[idx]
        model_label = MODEL_LABELS.get(cfg['model'], f"M{cfg['model']}")
        ea_comment  = f"{strategy_name + ' ' if strategy_name else ''}{name_stem} {symbol} {period} {model_label}"
        report_name = ea_comment.replace(' ', '_')

        progress_queue.put({
            'idx'     : idx,
            'total'   : len(set_files),
            'file'    : filename,
            'symbol'  : symbol,
            'period'  : period,
            'status'  : 'running',
            'message' : f"Running backtest...",
        })

        try:
            # Copy and modify set file
            rel_path   = os.path.relpath(set_path, set_folder)
            rel_subdir = os.path.dirname(rel_path)
            # _batch_modified mirrors subfolder structure
            out_dir    = os.path.join(out_set_folder, rel_subdir) if rel_subdir else out_set_folder
            # Reports go into a reports/ folder next to the set file
            set_file_dir = os.path.dirname(set_path)
            rep_dir      = os.path.join(set_file_dir, 'reports')
            os.makedirs(out_dir, exist_ok=True)
            os.makedirs(rep_dir, exist_ok=True)

            modified_set = os.path.join(out_dir, filename)
            shutil.copy2(set_path, modified_set)

            if lot_mode_f != 'asis':
                update_set_file(modified_set, ea_comment, lot_mode_f, lot_value)
            else:
                # Just update EA_Comment
                update_set_file(modified_set, ea_comment, None, None)

            ini_path   = os.path.join(tester_folder, f"{name_stem}.ini")
            tester_set = os.path.join(tester_folder, filename)
            shutil.copy2(modified_set, tester_set)
            build_ini(symbol, period, tester_set, ini_path, report_name, cfg)

            # Launch MT5 minimised
            si = subprocess.STARTUPINFO()
            si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 6  # SW_MINIMIZE
            proc = subprocess.Popen(
                [terminal_path, f'/config:{ini_path}'],
                startupinfo=si
            )

            # Wait for completion
            while proc.poll() is None:
                time.sleep(5)

            # Find and copy report
            search_dirs = [
                terminal_data,
                os.path.join(terminal_data, 'MQL5', 'Profiles', 'Tester'),
                tester_folder,
            ]

            success    = False
            report_out = None
            for search_dir in search_dirs:
                htm_src = os.path.join(search_dir, report_name + '.htm')
                if os.path.isfile(htm_src):
                    htm_dest = os.path.join(rep_dir, report_name + '.htm')
                    shutil.copy2(htm_src, htm_dest)
                    report_out = htm_dest
                    # Copy associated files
                    for fn in os.listdir(search_dir):
                        if fn.startswith(report_name) and not fn.endswith('.htm'):
                            shutil.copy2(
                                os.path.join(search_dir, fn),
                                os.path.join(rep_dir, fn)
                            )
                    # Clean up source
                    os.remove(htm_src)
                    for fn in os.listdir(search_dir):
                        if fn.startswith(report_name) and not fn.endswith('.htm'):
                            try:
                                os.remove(os.path.join(search_dir, fn))
                            except:
                                pass
                    success = True
                    break

            progress_queue.put({
                'idx'       : idx,
                'total'     : len(set_files),
                'file'      : filename,
                'symbol'    : symbol,
                'period'    : period,
                'status'    : 'done' if success else 'failed',
                'message'   : f"Report saved" if success else "No report found",
                'report'    : report_out,
            })

        except Exception as e:
            progress_queue.put({
                'idx'     : idx,
                'total'   : len(set_files),
                'file'    : filename,
                'symbol'  : symbol,
                'period'  : period,
                'status'  : 'error',
                'message' : str(e),
            })

    progress_queue.put({'status': 'complete'})


# ── Main render ───────────────────────────────────────────────────────────────

def render():
    st.title("📋 Batch Backtest")

    # ── Session state ─────────────────────────────────────────────────────────
    for k, v in {
        'bb_running'  : False,
        'bb_results'  : [],
        'bb_queue'    : None,
        'bb_thread'   : None,
        'bb_complete' : False,
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # ── Config check ──────────────────────────────────────────────────────────
    cfg = load_config()
    if cfg is None:
        st.error("No config found — run `mt5_batch_backtest.py` once from the terminal to set up config, then return here.")
        return

    with st.expander("📁 Active Config", expanded=False):
        c1, c2 = st.columns(2)
        c1.markdown(f"**Terminal:** {cfg.get('terminal_label', 'Unknown')}")
        c1.markdown(f"**EA:** `{cfg.get('ea_name', '')}`")
        c1.markdown(f"**Dates:** {cfg.get('from_date')} → {cfg.get('to_date')}")
        c2.markdown(f"**Model:** {cfg.get('model')} ({MODEL_LABELS.get(cfg.get('model','1'), '?')})")
        c2.markdown(f"**Deposit:** {cfg.get('deposit')} {cfg.get('currency')}")
        c2.markdown(f"**Leverage:** {cfg.get('leverage')}  **Suffix:** `{cfg.get('suffix', '.a')}`")
        st.caption(f"Config file: {CONFIG_FILE}")

    suffix = cfg.get('suffix', '.a')

    # ── Session overrides ─────────────────────────────────────────────────────
    # Scan available EAs from terminal MQL5/Experts/Market folder
    tester_folder = cfg.get('tester_folder', '')
    experts_dir   = os.path.join(os.path.dirname(tester_folder), 'MQL5', 'Experts', 'Market')
    ea_options    = []
    if os.path.isdir(experts_dir):
        for fn in sorted(os.listdir(experts_dir)):
            if fn.lower().endswith('.ex5'):
                ea_options.append(f"Market\\{fn}")

    if ea_options:
        current_ea  = cfg.get('ea_name', ea_options[0])
        default_idx = ea_options.index(current_ea) if current_ea in ea_options else 0
        selected_ea = st.selectbox(
            "Expert Advisor",
            ea_options,
            index=default_idx,
            key='bb_ea'
        )
    else:
        selected_ea = st.text_input(
            "Expert Advisor",
            value=cfg.get('ea_name', ''),
            key='bb_ea'
        )

    # Use selected EA for this session (don't save to config)
    cfg = dict(cfg)
    cfg['ea_name'] = selected_ea

    # ── Set file folder ───────────────────────────────────────────────────────
    st.divider()
    st.subheader("1 — Set Files")

    col_fi, col_rec = st.columns([4, 1])
    with col_fi:
        set_folder = st.text_input(
            "Folder containing .set files",
            placeholder=r"C:\Users\pc\Desktop\EA\MySetFiles",
            key='bb_folder'
        )
    with col_rec:
        st.markdown("<br>", unsafe_allow_html=True)
        recurse = st.toggle("Include subfolders", value=True, key='bb_recurse')

    set_files = []
    if set_folder and os.path.isdir(set_folder):
        if recurse:
            all_files = sorted(glob.glob(os.path.join(set_folder, '**', '*.set'), recursive=True))
        else:
            all_files = sorted(glob.glob(os.path.join(set_folder, '*.set')))
        set_files = [f for f in all_files
                     if not os.path.basename(f).lower().startswith('optimization')
                     and '_batch_modified' not in os.path.normpath(f).replace(os.sep, '/')]
        skipped = len(all_files) - len(set_files)
        if set_files:
            st.success(f"Found **{len(set_files)}** .set file(s)" +
                       (f" · {skipped} Optimization file(s) excluded" if skipped else ""))
        else:
            st.warning("No .set files found in that folder")
    elif set_folder:
        st.error("Folder not found")

    if not set_files:
        return

    # ── Report folder ─────────────────────────────────────────────────────────
    # Reports go into a reports/ subfolder next to each set file
    report_folder = None  # resolved per-file inside run_batch
    st.caption("📁 Reports will be saved to a `reports/` subfolder next to each set file.")

    # ── Lot size mode ─────────────────────────────────────────────────────────
    st.divider()
    st.subheader("2 — Lot Size")

    lot_mode_label = st.radio(
        "Lot size mode",
        ["Use set file as-is", "Manual lot size (all files)", "Lots per balance (from file)"],
        horizontal=True, key='bb_lot_mode'
    )
    lot_mode_map = {
        "Use set file as-is"           : 'asis',
        "Manual lot size (all files)"  : 'manual',
        "Lots per balance (from file)" : 'balance',
    }
    lot_mode = lot_mode_map[lot_mode_label]

    manual_lots = None
    if lot_mode == 'manual':
        manual_lots = st.text_input("StartLots for all files", value="0.01", key='bb_lots')

    # ── Strategy name ─────────────────────────────────────────────────────────
    st.divider()
    st.subheader("3 — Strategy Name (EA Comment)")
    st.caption("Prefixed to EA_Comment in each set file. Leave blank to use the set file name.")
    strategy_name = st.text_input(
        "Strategy name (one for all files)",
        placeholder="e.g. UBS_v2  —  leave blank to use filename",
        key='bb_strategy_name'
    ).strip()

    # ── Instrument & Timeframe defaults ──────────────────────────────────────
    st.divider()
    st.subheader("4 — Instrument & Timeframe")

    col1, col2, col3 = st.columns(3)
    with col1:
        instr_mode = st.radio(
            "Instrument default",
            ["One for all files", "Extract from filename"],
            key='bb_instr_mode'
        )
        instr_global  = None
        instr_n_chars = 6
        if instr_mode == "One for all files":
            instr_global = st.text_input("Instrument (without suffix)",
                           placeholder="GBPJPY", key='bb_instr').upper()
        else:
            instr_n_chars = st.number_input("Characters from filename start",
                           min_value=1, max_value=12, value=6, key='bb_nchars')

    with col2:
        tf_mode = st.radio(
            "Timeframe default",
            ["One for all files", "Detect from filename"],
            key='bb_tf_mode'
        )
        tf_global = None
        if tf_mode == "One for all files":
            tf_global = st.selectbox("Timeframe",
                        ['Daily','H4','H1','M30','M15','M5','M1','Weekly'], key='bb_tf')

    with col3:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.info("Review and edit all values in the table below before running.")

    # ── Build initial values ───────────────────────────────────────────────────
    import pandas as pd

    ml = MODEL_LABELS.get(cfg['model'], f"M{cfg['model']}")

    preview_rows = []
    for fp in set_files:
        fn   = os.path.basename(fp)
        stem = os.path.splitext(fn)[0]

        # Instrument default
        if instr_mode == "One for all files":
            inst = instr_global if instr_global else detect_instrument(fn, int(instr_n_chars))
        else:
            inst = detect_instrument(fn, int(instr_n_chars))

        # Timeframe default
        if tf_mode == "One for all files" and tf_global:
            period = tf_global
        else:
            period = detect_timeframe(fn) or 'Daily'

        # Lot value default
        if lot_mode == 'manual':
            lot_val = manual_lots or '0.01'
        elif lot_mode in ('balance', 'balance_ask'):
            lot_val = get_lot_value_from_file(fp) or '100'
        else:
            lot_val = 'as-is'

        ea_preview = f"{strategy_name + ' ' if strategy_name else ''}{stem} {inst + suffix} {period} {ml}"
        preview_rows.append({
            'File'       : fn,
            'Symbol'     : inst + suffix,
            'Period'     : period,
            'Lot Value'  : lot_val,
            'EA_Comment' : ea_preview,
            'Report Name': ea_preview.replace(' ', '_') + '.htm',
        })

    # ── Editable preview table ─────────────────────────────────────────────────
    st.divider()
    st.subheader("5 — Preview & Edit")
    st.caption("All values are editable — review before running. Symbol includes suffix.")

    lot_col_disabled = lot_mode in ('asis', 'manual')

    edited = st.data_editor(
        pd.DataFrame(preview_rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            'File'       : st.column_config.TextColumn('File', disabled=True),
            'Symbol'     : st.column_config.TextColumn('Symbol'),
            'Period'     : st.column_config.SelectboxColumn('Period',
                           options=['Daily','H4','H1','M30','M15','M5','M1','Weekly']),
            'Lot Value'  : st.column_config.TextColumn('Lot Value',
                           disabled=lot_col_disabled,
                           help='Disabled for as-is and manual modes'),
            'EA_Comment' : st.column_config.TextColumn('EA_Comment', disabled=True),
            'Report Name': st.column_config.TextColumn('Report Name', disabled=True),
        },
        key='bb_preview_table'
    )

    # Extract final values from edited table
    instruments = list(edited['Symbol'])
    periods     = list(edited['Period'])
    lot_values  = []
    for i, fp in enumerate(set_files):
        if lot_mode == 'manual':
            lot_values.append(manual_lots or '0.01')
        elif lot_mode in ('balance', 'balance_ask'):
            lot_values.append(str(edited.iloc[i]['Lot Value']))
        else:
            lot_values.append(None)

    # ── Run button ─────────────────────────────────────────────────────────────
    st.divider()

    # Poll for progress updates
    if st.session_state['bb_running'] and st.session_state['bb_queue']:
        q = st.session_state['bb_queue']
        while not q.empty():
            update = q.get_nowait()
            if update.get('status') == 'complete':
                st.session_state['bb_running']  = False
                st.session_state['bb_complete'] = True
            else:
                # Update or add to results
                results = st.session_state['bb_results']
                found   = False
                for r in results:
                    if r['file'] == update['file']:
                        r.update(update)
                        found = True
                        break
                if not found:
                    results.append(update)
                st.session_state['bb_results'] = results

    col_run, col_stop = st.columns([2, 1])

    with col_run:
        run_disabled = st.session_state['bb_running'] or not set_files
        if st.button("▶ Start Batch", type="primary",
                     disabled=run_disabled, use_container_width=True):
            # Validate
            if not os.path.isfile(cfg['terminal_path']):
                st.error(f"terminal64.exe not found at: {cfg['terminal_path']}")
            elif not os.path.isdir(cfg['tester_folder']):
                st.error(f"Tester folder not found: {cfg['tester_folder']}")
            else:
                st.session_state['bb_results']  = []
                st.session_state['bb_complete'] = False
                st.session_state['bb_running']  = True

                q = queue.Queue()
                st.session_state['bb_queue'] = q

                t = threading.Thread(
                    target = run_batch,
                    args   = (set_files, set_folder, cfg, report_folder, lot_mode,
                               lot_values, instruments, periods, strategy_name, q),
                    daemon = True
                )
                st.session_state['bb_thread'] = t
                t.start()
                st.rerun()

    with col_stop:
        if st.session_state['bb_running']:
            st.warning("⏳ Batch running...")

    # ── Progress display ───────────────────────────────────────────────────────
    if st.session_state['bb_results'] or st.session_state['bb_running']:
        st.divider()
        st.subheader("Progress")

        if st.session_state['bb_running']:
            done  = len([r for r in st.session_state['bb_results'] if r.get('status') in ('done', 'failed', 'error')])
            total = len(set_files)
            st.progress(done / total if total else 0, text=f"{done} / {total} complete")
            time.sleep(2)
            st.rerun()

        results = st.session_state['bb_results']
        if results:
            status_icon = {
                'running': '⏳',
                'done'   : '✅',
                'failed' : '❌',
                'error'  : '⚠️',
            }

            def colour_status(val):
                if val == '✅': return 'color: #2dc653'
                if val == '❌': return 'color: #e63946'
                if val == '⚠️': return 'color: #f77f00'
                return 'color: #aaa'

            rows = [{
                'Status' : status_icon.get(r.get('status', ''), ''),
                'File'   : r.get('file', ''),
                'Symbol' : r.get('symbol', ''),
                'Period' : r.get('period', ''),
                'Message': r.get('message', ''),
            } for r in results]

            df_prog = pd.DataFrame(rows)
            st.dataframe(
                df_prog.style.map(colour_status, subset=['Status']),
                use_container_width=True, hide_index=True
            )

        if st.session_state['bb_complete']:
            done   = len([r for r in results if r.get('status') == 'done'])
            failed = len([r for r in results if r.get('status') in ('failed', 'error')])
            st.success(f"Batch complete — {done} succeeded, {failed} failed")
            st.caption("Reports saved to reports/ subfolder next to each set file")

            if st.button("🔄 Clear & Run Another"):
                st.session_state['bb_results']  = []
                st.session_state['bb_complete'] = False
                st.session_state['bb_running']  = False
                st.rerun()