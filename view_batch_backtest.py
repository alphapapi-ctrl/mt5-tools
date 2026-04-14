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
        return raw[2:].decode('utf-16-le').splitlines()
    elif raw[:2] == b'\xfe\xff':
        return raw[2:].decode('utf-16-be').splitlines()
    return raw.decode('utf-8', errors='replace').splitlines()


def write_utf16(path, lines):
    text = '\r\n'.join(lines) + '\r\n'
    with open(path, 'wb') as f:
        f.write(b'\xff\xfe')
        f.write(text.encode('utf-16-le'))


def detect_timeframe(filename):
    name = os.path.splitext(filename)[0].upper()
    for token in PERIOD_MAP:
        if name.endswith('_' + token) or name.endswith('-' + token) or \
           ('_' + token + '_') in name or ('-' + token + '-') in name:
            return PERIOD_MAP[token]
    return None


def detect_instrument(filename, n_chars):
    return os.path.splitext(filename)[0][:n_chars].upper()


def update_set_file(set_path, ea_comment, lot_mode, lot_value):
    lines = read_utf16(set_path)

    def update_param(lines, key, new_val):
        # Update existing key — if not found, append it so it is always written
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
        # Ensure Risk=0 and StartLots are written regardless of original file state
        update_param(lines, 'Risk', '0')
        update_param(lines, 'StartLots', str(lot_value))
        # Also clear LotPerBalance_step if present so balance mode can't leak through
        for i, line in enumerate(lines):
            if line.strip().startswith('LotPerBalance_step='):
                parts = line.strip().split('||')
                parts[0] = 'LotPerBalance_step=0'
                lines[i] = '||'.join(parts)
                break
    elif lot_mode == 'balance':
        update_param(lines, 'Risk', '9999')
        update_param(lines, 'LotPerBalance_step', str(lot_value))

    write_utf16(set_path, lines)


def build_ini(symbol, period, set_file_path, ini_out_path, report_name, cfg):
    content = (
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
        f'Inputs={set_file_path}\r\n'
        'ShutdownTerminal=1\r\n'
    )
    with open(ini_out_path, 'wb') as f:
        f.write(content.encode('utf-8'))


def get_lot_value_from_file(set_path):
    lines = read_utf16(set_path)
    for line in lines:
        if line.strip().startswith('LotPerBalance_step='):
            parts = line.strip().split('||')
            return parts[0].replace('LotPerBalance_step=', '').strip()
    return None


# ── Background batch runner ───────────────────────────────────────────────────

def run_batch(set_files, cfg, report_folder, lot_mode, lot_values,
              instruments, periods, progress_queue, report_names=None):
    """
    Runs in a background thread.
    Sends progress updates via progress_queue as dicts.
    """
    tester_folder  = cfg['tester_folder']
    terminal_path  = cfg['terminal_path']
    out_set_folder = os.path.join(os.path.dirname(set_files[0]), '_batch_modified')
    os.makedirs(out_set_folder, exist_ok=True)
    os.makedirs(report_folder, exist_ok=True)

    terminal_data = os.path.dirname(tester_folder)

    for idx, set_path in enumerate(set_files):
        filename    = os.path.basename(set_path)
        name_stem   = os.path.splitext(filename)[0]
        symbol      = instruments[idx]
        period      = periods[idx]
        lot_mode_f  = lot_mode
        lot_value   = lot_values[idx]
        model_label = MODEL_LABELS.get(cfg['model'], f"M{cfg['model']}")
        # Use report name from preview table if provided, else fall back to default
        if report_names and idx < len(report_names) and report_names[idx]:
            report_name = report_names[idx]
        else:
            report_name = f"{name_stem}_{model_label}"
        ea_comment  = f"{report_name}"

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
            rel_path   = os.path.relpath(set_path, os.path.dirname(set_files[0]))
            rel_subdir = os.path.dirname(rel_path)
            out_dir    = os.path.join(out_set_folder, rel_subdir) if rel_subdir else out_set_folder
            rep_dir    = os.path.join(report_folder, rel_subdir)  if rel_subdir else report_folder
            os.makedirs(out_dir, exist_ok=True)
            os.makedirs(rep_dir, exist_ok=True)

            modified_set = os.path.join(out_dir, filename)
            shutil.copy2(set_path, modified_set)

            if lot_mode_f != 'asis':
                update_set_file(modified_set, ea_comment, lot_mode_f, lot_value)
            else:
                # Just update EA_Comment
                update_set_file(modified_set, ea_comment, None, None)

            ini_path = os.path.join(tester_folder, f"{name_stem}.ini")
            build_ini(symbol, period, modified_set, ini_path, report_name, cfg)

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
        'bb_edit_cfg' : False,
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # ── Config check ──────────────────────────────────────────────────────────
    cfg = load_config()
    if cfg is None:
        st.error("No config found — run `mt5_batch_backtest.py` once from the terminal to set up config, then return here.")
        return

    with st.expander("📁 Active Config", expanded=False):
        # ── View mode ─────────────────────────────────────────────────────
        if not st.session_state.get('bb_edit_cfg', False):
            c1, c2 = st.columns(2)
            c1.markdown(f"**Terminal:** {cfg.get('terminal_label', 'Unknown')}")
            c1.markdown(f"**EA:** `{cfg.get('ea_name', '')}`")
            c1.markdown(f"**Dates:** {cfg.get('from_date')} → {cfg.get('to_date')}")
            c2.markdown(f"**Model:** {cfg.get('model')} ({MODEL_LABELS.get(cfg.get('model','1'), '?')})")
            c2.markdown(f"**Deposit:** {cfg.get('deposit')} {cfg.get('currency')}")
            c2.markdown(f"**Leverage:** {cfg.get('leverage')}  **Suffix:** `{cfg.get('suffix', '.a')}`")
            st.caption(f"Config file: {CONFIG_FILE}")
            if st.button("✏️ Edit Config", key='bb_edit_cfg_btn'):
                st.session_state['bb_edit_cfg'] = True
                st.rerun()
        else:
            # ── Edit mode ─────────────────────────────────────────────────
            st.caption("Edit and save to update mt5_batch_config.json")
            e1, e2 = st.columns(2)
            new_terminal = e1.text_input("terminal64.exe path",
                value=cfg.get('terminal_path',''), key='bb_cfg_terminal')
            new_tester   = e1.text_input("Tester folder",
                value=cfg.get('tester_folder',''), key='bb_cfg_tester')
            new_from     = e2.text_input("From date (YYYY.MM.DD)",
                value=cfg.get('from_date',''), key='bb_cfg_from')
            new_to       = e2.text_input("To date (YYYY.MM.DD)",
                value=cfg.get('to_date',''), key='bb_cfg_to')
            e3, e4 = st.columns(2)
            new_deposit  = e3.text_input("Deposit",
                value=cfg.get('deposit','10000'), key='bb_cfg_deposit')
            new_currency = e3.text_input("Currency",
                value=cfg.get('currency','USD'), key='bb_cfg_currency')
            new_leverage = e4.text_input("Leverage",
                value=cfg.get('leverage','100'), key='bb_cfg_leverage')
            new_suffix   = e4.text_input("Instrument suffix (e.g. .a)",
                value=cfg.get('suffix','.a'), key='bb_cfg_suffix')
            new_model    = e3.selectbox("Model",
                options=['1','2','4','5'],
                format_func=lambda x: f"{x} — {MODEL_LABELS.get(x,'?')}",
                index=['1','2','4','5'].index(cfg.get('model','1')),
                key='bb_cfg_model')

            sv1, sv2 = st.columns(2)
            if sv1.button("💾 Save Config", type="primary", key='bb_save_cfg'):
                updated = dict(cfg)
                updated.update({
                    'terminal_path': new_terminal,
                    'tester_folder': new_tester,
                    'from_date'    : new_from,
                    'to_date'      : new_to,
                    'deposit'      : new_deposit,
                    'currency'     : new_currency,
                    'leverage'     : new_leverage,
                    'suffix'       : new_suffix,
                    'model'        : new_model,
                })
                try:
                    with open(CONFIG_FILE, 'w') as f:
                        json.dump(updated, f, indent=2)
                    cfg = updated
                    st.session_state['bb_edit_cfg'] = False
                    st.success("Config saved.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not save config: {e}")
            if sv2.button("Cancel", key='bb_cancel_cfg'):
                st.session_state['bb_edit_cfg'] = False
                st.rerun()

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
    default_reports = os.path.join(set_folder, 'reports')
    report_folder   = st.text_input("Report output folder", value=default_reports, key='bb_reports')

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

    # ── Strategy name & instance ─────────────────────────────────────────────
    st.divider()
    st.subheader("3 — Strategy & Report Naming")
    nc1, nc2 = st.columns([3, 3])
    strategy_name = nc1.text_input(
        "Strategy name (used in report filename)",
        placeholder="e.g. GoldPhantom",
        key='bb_strategy_name',
        help="Applied to all files. Leave blank to use the .set filename stem."
    )
    nc2.markdown("<br>", unsafe_allow_html=True)
    nc2.caption("When a strategy name is set, report names become: "
                "`{strategy}_{symbol}_{period}_{model}_{stem}` — "
                "the .set filename stem becomes the instance (e.g. A, B).")

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
        if instr_mode == "One for all files" and instr_global:
            inst = instr_global
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

        # Build report name
        # If strategy name given: {strategy}_{symbol}_{period}_{model}_{stem}
        # Stem is the instance letter when set files are named A.set, B.set etc.
        # If no strategy name: fall back to original {stem}_{model}
        sym_part = (inst + suffix).replace('.','')
        if strategy_name.strip():
            rpt_name = f"{strategy_name.strip()}_{sym_part}_{period}_{ml}_{stem}"
        else:
            rpt_name = f"{stem}_{ml}"

        preview_rows.append({
            'File'       : fn,
            'Symbol'     : inst + suffix,
            'Period'     : period,
            'Lot Value'  : lot_val,
            'Report Name': f"{rpt_name}.htm",
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
            'Report Name': st.column_config.TextColumn('Report Name', help='Editable — final filename used for .htm report'),
        },
        key='bb_preview_table'
    )

    # Extract final values from edited table
    instruments  = list(edited['Symbol'])
    periods      = list(edited['Period'])
    report_names = [str(r).replace('.htm','') for r in edited['Report Name']]
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
                    args   = (set_files, cfg, report_folder, lot_mode,
                               lot_values, instruments, periods, q,
                               report_names),
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
            st.caption(f"Reports saved to: {report_folder}")

            if st.button("🔄 Clear & Run Another"):
                st.session_state['bb_results']  = []
                st.session_state['bb_complete'] = False
                st.session_state['bb_running']  = False
                st.rerun()