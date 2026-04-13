"""
MT5 Batch Backtest Runner
=========================
Generates a MT5 tester .ini file for each .set file in a folder,
updates EA_Comment in each .set file, then launches MT5 terminal
for each backtest sequentially.

On first run: detects MT5 installations and saves config.
Subsequent runs: loads saved config, prompts to use same or modify.

Usage: python mt5_batch_backtest.py
"""

import os
import sys
import glob
import subprocess
import time
import shutil
import json

# ── MT5 Period constants ───────────────────────────────────────────────────────
PERIOD_MAP = {
    'M1'   : 'M1',
    'M5'   : 'M5',
    'M15'  : 'M15',
    'M30'  : 'M30',
    'H1'   : 'H1',
    'H4'   : 'H4',
    'D'    : 'Daily',
    'D1'   : 'Daily',
    'DAILY': 'Daily',
    'W1'   : 'Weekly',
    'MN'   : 'Monthly',
}

# ── Model labels ─────────────────────────────────────────────────────────────
MODEL_LABELS = {
    '1' : 'OHLC',
    '2' : 'CTRLPTS',
    '4' : 'EVERYTICK',
    '5' : 'EVERYTICKREAL',
}

# ── Defaults (used if no config found) ────────────────────────────────────────
DEFAULTS = {
    'terminal_path' : r"C:\Program Files\MetaTrader 5\terminal64.exe",
    'tester_folder' : '',
    'ea_name'       : r"Market\Ultimate Breakout System.ex5",
    'from_date'     : '2018.01.01',
    'to_date'       : '2026.04.01',
    'model'         : '1',
    'deposit'       : '10000',
    'currency'      : 'USD',
    'leverage'      : '100',
    'optimization'  : '0',
    'suffix'        : '.a',
}

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mt5_batch_config.json')


# ── Helpers ────────────────────────────────────────────────────────────────────

def prompt(text, default=None):
    if default is not None and default != '':
        val = input(f"  {text} [{default}]: ").strip()
        return val if val else default
    else:
        while True:
            val = input(f"  {text}: ").strip()
            if val:
                return val
            print("    (required)")


def load_config():
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return None


def save_config(cfg):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"  WARNING: Could not save config: {e}")


def find_mt5_terminals():
    """Scan MetaQuotes Terminal folder for all MT5 installations."""
    appdata = os.environ.get('APPDATA', '')
    base    = os.path.join(appdata, 'MetaQuotes', 'Terminal')
    results = []
    if not os.path.isdir(base):
        return results
    for entry in os.listdir(base):
        entry_path = os.path.join(base, entry)
        if not os.path.isdir(entry_path):
            continue
        # Check for origin.txt which contains the terminal exe path
        origin = os.path.join(entry_path, 'origin.txt')
        tester = os.path.join(entry_path, 'Tester')
        label  = entry
        if os.path.isfile(origin):
            try:
                with open(origin, 'r', encoding='utf-8', errors='replace') as f:
                    label = f.read().strip() or entry
            except:
                pass
        # Skip folders that don't look like real MT5 terminals
        if not os.path.isdir(os.path.join(entry_path, 'MQL5')):
            continue
        results.append({
            'id'            : entry,
            'label'         : label,
            'tester_folder' : tester,
            'data_folder'   : entry_path,
        })
    return results


def pick_terminal():
    """Let user pick from detected MT5 terminals."""
    terminals = find_mt5_terminals()
    if not terminals:
        print("  No MT5 terminals found in AppData\\MetaQuotes\\Terminal\\")
        print("  You will need to enter the tester folder path manually.")
        return None, None

    print()
    print("  Detected MT5 terminal(s):")
    for i, t in enumerate(terminals, 1):
        print(f"    {i}) {t['label']}")
        print(f"       Tester: {t['tester_folder']}")

    if len(terminals) == 1:
        choice = input(f"\n  Select terminal [1]: ").strip()
        idx = 0
    else:
        while True:
            choice = input(f"\n  Select terminal [1-{len(terminals)}]: ").strip()
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(terminals):
                    break
            except:
                pass
            print("  Invalid selection.")

    selected = terminals[idx]
    return selected['tester_folder'], selected['label']


def find_ea_files(tester_folder):
    """
    Scan MQL5/Experts folder for .ex5 files.
    Returns list of dicts with label (display) and value (ini path).
    """
    experts_dir = os.path.join(os.path.dirname(tester_folder), 'MQL5', 'Experts')
    results = []
    if not os.path.isdir(experts_dir):
        return results
    for root, dirs, files in os.walk(experts_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fn in sorted(files):
            if fn.lower().endswith('.ex5'):
                full_path = os.path.join(root, fn)
                rel = os.path.relpath(full_path, experts_dir)
                # Only include EAs in the Market subfolder
                if rel.startswith('Market' + os.sep) or rel.startswith('Market/'):
                    results.append({'label': rel, 'value': rel})
    return results


def pick_ea_name(tester_folder, current=None):
    """List available EAs and let user pick, or enter manually."""
    eas = find_ea_files(tester_folder)

    if not eas:
        print("  No .ex5 files found in MQL5/Experts -- enter EA name manually.")
        return prompt("EA Name", current or DEFAULTS['ea_name'])

    print()
    print("  Available EAs:")
    for i, ea in enumerate(eas, 1):
        marker = ' <' if current and ea['value'] == current else ''
        print(f"    {i:>3}) {ea['label']}{marker}")
    print(f"    {len(eas)+1:>3}) Enter manually")

    while True:
        default_idx = None
        if current:
            for i, ea in enumerate(eas, 1):
                if ea['value'] == current:
                    default_idx = i
                    break
        hint = f"1-{len(eas)+1}" + (f", Enter={default_idx}" if default_idx else "")
        choice = input(f"  Select EA [{hint}]: ").strip()

        if choice == '' and default_idx:
            return eas[default_idx - 1]['value']
        try:
            idx = int(choice) - 1
            if idx == len(eas):
                return prompt("EA Name", current or DEFAULTS['ea_name'])
            if 0 <= idx < len(eas):
                return eas[idx]['value']
        except:
            pass
        print("  Invalid selection.")


def setup_config():
    """First-run setup — detect terminals and build config."""
    print()
    print("  ── First Run Setup ──────────────────────────────────────")

    tester_folder, terminal_label = pick_terminal()

    if not tester_folder:
        tester_folder = prompt("Tester folder path")

    terminal_path = prompt("Path to terminal64.exe", DEFAULTS['terminal_path'])

    print()
    print("  ── Backtest Defaults ────────────────────────────────────")
    cfg = {
        'terminal_path' : terminal_path,
        'tester_folder' : tester_folder,
        'terminal_label': terminal_label or '',
        'ea_name'       : pick_ea_name(tester_folder, DEFAULTS['ea_name']),
        'from_date'     : prompt("From Date (YYYY.MM.DD)", DEFAULTS['from_date']),
        'to_date'       : prompt("To Date   (YYYY.MM.DD)", DEFAULTS['to_date']),
        'model'         : prompt("Model (1=OHLC M1, 2=Control points, 4=Every tick)", DEFAULTS['model']),
        'deposit'       : prompt("Deposit", DEFAULTS['deposit']),
        'currency'      : prompt("Currency", DEFAULTS['currency']),
        'leverage'      : prompt("Leverage", DEFAULTS['leverage']),
        'suffix'        : prompt("Instrument suffix (e.g. .a)", DEFAULTS['suffix']),
    }
    save_config(cfg)
    print()
    print(f"  Config saved to: {CONFIG_FILE}")
    return cfg


def review_config(cfg):
    """Show saved config and ask to use same or modify."""
    print()
    print("  ── Saved Settings ───────────────────────────────────────")
    print(f"  Terminal : {cfg.get('terminal_label', cfg['tester_folder'])}")
    print(f"  Tester   : {cfg['tester_folder']}")
    print(f"  EA       : {cfg['ea_name']}")
    print(f"  Dates    : {cfg['from_date']} → {cfg['to_date']}")
    print(f"  Model    : {cfg['model']}  Deposit: {cfg['deposit']} {cfg['currency']}  Leverage: {cfg['leverage']}")
    print(f"  Suffix   : {cfg['suffix']}")
    print()
    choice = input("  Use these settings? [Y/n/reset]: ").strip().lower()

    if choice == 'reset':
        os.remove(CONFIG_FILE)
        print("  Config reset — re-running setup.")
        return setup_config()

    if choice == 'n':
        print()
        print("  ── Modify Settings ──────────────────────────────────────")
        redetect = input("  Re-detect MT5 terminals? [y/N]: ").strip().lower()
        if redetect == 'y':
            tester_folder, terminal_label = pick_terminal()
            if tester_folder:
                cfg['tester_folder']  = tester_folder
                cfg['terminal_label'] = terminal_label or ''

        cfg['terminal_path'] = prompt("terminal64.exe path", cfg['terminal_path'])
        cfg['ea_name']       = pick_ea_name(cfg['tester_folder'], cfg['ea_name'])
        cfg['from_date']     = prompt("From Date",           cfg['from_date'])
        cfg['to_date']       = prompt("To Date",             cfg['to_date'])
        cfg['model']         = prompt("Model (1=OHLC M1, 2=Control points, 4=Every tick)", cfg['model'])
        cfg['deposit']       = prompt("Deposit",             cfg['deposit'])
        cfg['currency']      = prompt("Currency",            cfg['currency'])
        cfg['leverage']      = prompt("Leverage",            cfg['leverage'])
        cfg['suffix']        = prompt("Suffix",              cfg['suffix'])
        save_config(cfg)
        print("  Config updated.")

    return cfg


def detect_timeframe(filename):
    name = os.path.splitext(filename)[0].upper()
    for token in PERIOD_MAP:
        if name.endswith('_' + token) or name.endswith('-' + token) or \
           ('_' + token + '_') in name or ('-' + token + '-') in name:
            return PERIOD_MAP[token]
    return None


def detect_instrument(filename, n_chars):
    return os.path.splitext(filename)[0][:n_chars].upper()


def read_utf16(path):
    with open(path, 'rb') as f:
        raw = f.read()
    if raw[:2] == b'\xff\xfe':
        text = raw[2:].decode('utf-16-le')
    elif raw[:2] == b'\xfe\xff':
        text = raw[2:].decode('utf-16-be')
    else:
        text = raw.decode('utf-8', errors='replace')
    return text.splitlines()


def write_utf16(path, lines):
    text = '\r\n'.join(lines) + '\r\n'
    with open(path, 'wb') as f:
        f.write(b'\xff\xfe')
        f.write(text.encode('utf-16-le'))


def update_set_file(set_path, ea_comment, lot_mode, lot_value):
    lines = read_utf16(set_path)

    def update_param(lines, key, new_val):
        # Update existing key — also clears the "use default" flag (bit 2) and
        # updates the default value field so MT5 does not override with the old default.
        # MT5 .set format: param=value||flags||default||min||max||step||digits
        for i, line in enumerate(lines):
            if line.strip().startswith(key + '='):
                parts = line.strip().split('||')
                parts[0] = f'{key}={new_val}'
                if len(parts) > 1:
                    try:
                        flags = int(parts[1])
                        flags = flags & ~4   # clear bit 2 ("use default")
                        parts[1] = str(flags)
                    except ValueError:
                        pass
                if len(parts) > 2:
                    parts[2] = str(new_val)  # update default field too
                lines[i] = '||'.join(parts)
                return True
        lines.append(f'{key}={new_val}')
        return False

    # Always update EA_Comment
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
        # Clear LotPerBalance_step so balance mode can't leak through
        for i, line in enumerate(lines):
            if line.strip().startswith('LotPerBalance_step='):
                parts = line.strip().split('||')
                parts[0] = 'LotPerBalance_step=0'
                lines[i] = '||'.join(parts)
                break
    elif lot_mode == 'balance':
        update_param(lines, 'Risk', '9999')
        update_param(lines, 'LotPerBalance_step', str(lot_value))
    # lot_mode None/'asis': only EA_Comment updated

    write_utf16(set_path, lines)


def build_ini(symbol, period, set_file_path, ini_out_path, report_folder, cfg):
    name_stem   = os.path.splitext(os.path.basename(set_file_path))[0]
    model_label = MODEL_LABELS.get(cfg['model'], f"M{cfg['model']}")
    report_name = f"{name_stem}_{model_label}"
    content = (
        '[Tester]\r\n'
        f'Expert={cfg["ea_name"]}\r\n'
        f'Symbol={symbol}\r\n'
        f'Period={period}\r\n'
        f'Optimization={cfg.get("optimization","0")}\r\n'
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


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  MT5 Batch Backtest Runner")
    print("=" * 60)

    # ── Load or create config ──────────────────────────────────────
    cfg = load_config()
    if cfg is None:
        cfg = setup_config()
    else:
        cfg = review_config(cfg)

    terminal_path = cfg['terminal_path']
    tester_folder = cfg['tester_folder']
    os.makedirs(tester_folder, exist_ok=True)

    if not os.path.isfile(terminal_path):
        print(f"\n  WARNING: terminal64.exe not found at: {terminal_path}")

    # ── Folder of set files ────────────────────────────────────────
    print()
    set_folder = prompt("Path to folder containing .set files")
    set_folder = os.path.expandvars(set_folder.strip('"').strip("'"))
    if not os.path.isdir(set_folder):
        print(f"  ERROR: Folder not found: {set_folder}")
        sys.exit(1)

    all_set_files = sorted(glob.glob(os.path.join(set_folder, '**', '*.set'), recursive=True))
    set_files = [f for f in all_set_files
                 if not os.path.basename(f).lower().startswith('optimization')
                 and '_batch_modified' not in f.replace('\\', '/')]
    skipped = len(all_set_files) - len(set_files)
    if not set_files:
        print("  ERROR: No .set files found (after exclusions).")
        sys.exit(1)
    print(f"  Found {len(set_files)} .set file(s).", end='')
    if skipped:
        print(f" ({skipped} Optimization file(s) excluded)", end='')
    print()

    # ── Report output folder ───────────────────────────────────────
    default_reports = os.path.join(set_folder, 'reports')
    report_folder = prompt("Path to save reports", default_reports)
    report_folder = report_folder.strip('"').strip("'")
    os.makedirs(report_folder, exist_ok=True)

    # ── Lot size mode ──────────────────────────────────────────────
    print()
    print("  Lot size mode:")
    print("    0 = Use set file as-is (no changes to Risk/Lots)")
    print("    1 = Manual lot size (same for all) — sets Risk=0, StartLots=X")
    print("    2 = Lots per balance (from each .set file) — sets Risk=9999")
    print("    3 = Lots per balance (enter per file) — sets Risk=9999")
    lot_mode_choice = prompt("Choose [0/1/2/3]", "2")

    lot_mode    = {'0': 'asis', '1': 'manual'}.get(lot_mode_choice, 'balance')
    balance_ask = lot_mode_choice == '3'
    manual_lots = None

    if lot_mode == 'asis':
        print("  Set files used as-is — no Risk/Lots changes.")
    elif lot_mode == 'manual':
        manual_lots = prompt("StartLots for all files", "0.01")
    elif balance_ask:
        print("  Will prompt LotPerBalance_step per file. Risk=9999.")
    else:
        print("  Using LotPerBalance_step from each file. Risk=9999.")

    # ── Instrument mode ────────────────────────────────────────────
    print()
    print("  Instrument detection:")
    print("    1 = Enter one instrument for all set files")
    print("    2 = Extract from filename (specify number of characters)")
    print("    3 = Ask per file")
    instr_mode    = prompt("Choose [1/2/3]", "1")
    instr_global  = None
    instr_n_chars = None

    if instr_mode == '1':
        instr_global = prompt("Instrument (without suffix, e.g. GBPJPY)").upper()
    elif instr_mode == '2':
        instr_n_chars = int(prompt("Characters from start of filename", "6"))

    # ── Timeframe mode ─────────────────────────────────────────────
    print()
    print("  Timeframe:")
    print("    1 = One timeframe for all set files")
    print("    2 = Detect from filename (D/Daily/H1/H4 etc.)")
    print("    3 = Ask per file")
    tf_mode   = prompt("Choose [1/2/3]", "2")
    tf_global = None

    if tf_mode == '1':
        tf_raw    = prompt("Timeframe (e.g. Daily, H1, H4, M15)").upper()
        tf_global = PERIOD_MAP.get(tf_raw, tf_raw)

    # ── Output folder for modified .set copies ─────────────────────
    # (subfolders mirrored inside _batch_modified and reports)
    out_set_base = os.path.join(set_folder, '_batch_modified')
    os.makedirs(out_set_base, exist_ok=True)

    # ── Process each set file ──────────────────────────────────────
    print()
    print("=" * 60)
    print(f"  Processing {len(set_files)} file(s)...")
    print("=" * 60)

    results = []

    for set_path in set_files:
        filename  = os.path.basename(set_path)
        name_stem = os.path.splitext(filename)[0]

        # Mirror subfolder structure from set_folder root
        rel_path        = os.path.relpath(set_path, set_folder)
        rel_subdir      = os.path.dirname(rel_path)
        out_set_folder  = os.path.join(out_set_base, rel_subdir) if rel_subdir else out_set_base
        file_report_dir = os.path.join(report_folder, rel_subdir) if rel_subdir else report_folder
        os.makedirs(out_set_folder, exist_ok=True)
        os.makedirs(file_report_dir, exist_ok=True)

        subfolder_label = f" ({rel_subdir})" if rel_subdir else ""
        print(f"\n  [{filename}]{subfolder_label}")

        # Instrument
        if instr_mode == '1':
            instrument = instr_global
        elif instr_mode == '2':
            instrument = detect_instrument(filename, instr_n_chars)
            print(f"    Instrument: {instrument}")
        else:
            instrument = prompt(f"Instrument for {filename} (without suffix)").upper()

        symbol = instrument + cfg['suffix']

        # Timeframe
        if tf_mode == '1':
            period = tf_global
        elif tf_mode == '2':
            period = detect_timeframe(filename)
            if period:
                print(f"    Timeframe : {period}")
            else:
                tf_raw = prompt(f"Timeframe for {filename} (e.g. Daily, H1, H4)").upper()
                period = PERIOD_MAP.get(tf_raw, tf_raw)
        else:
            tf_raw = prompt(f"Timeframe for {filename}").upper()
            period = PERIOD_MAP.get(tf_raw, tf_raw)

        # Lot value
        if lot_mode == 'asis':
            lot_value = None
            print(f"    Lots      : as-is")
        elif lot_mode == 'manual':
            lot_value = manual_lots
            print(f"    Lots      : Manual StartLots={lot_value}")
        else:
            if balance_ask:
                file_lines = read_utf16(set_path)
                file_lot   = None
                for line in file_lines:
                    if line.strip().startswith('LotPerBalance_step='):
                        parts    = line.strip().split('||')
                        file_lot = parts[0].replace('LotPerBalance_step=', '').strip()
                        break
                lot_value = prompt(f"LotPerBalance_step for {filename}", file_lot or "100")
            else:
                file_lines = read_utf16(set_path)
                lot_value  = None
                for line in file_lines:
                    if line.strip().startswith('LotPerBalance_step='):
                        parts     = line.strip().split('||')
                        lot_value = parts[0].replace('LotPerBalance_step=', '').strip()
                        break
                if lot_value is None:
                    lot_value = prompt(f"LotPerBalance_step not found, enter value", "100")
            print(f"    Lots      : Balance LotPerBalance_step={lot_value} Risk=9999")

        # EA_Comment
        model_label = MODEL_LABELS.get(cfg['model'], f"M{cfg['model']}")
        ea_comment  = f"{name_stem} {symbol} {period} {model_label}"
        print(f"    EA_Comment: {ea_comment}")

        # Copy and modify set file
        modified_set = os.path.join(out_set_folder, filename)
        shutil.copy2(set_path, modified_set)
        if lot_mode == 'asis':
            update_set_file(modified_set, ea_comment, None, None)
        else:
            update_set_file(modified_set, ea_comment, lot_mode, lot_value)

        # Write ini
        model_label = MODEL_LABELS.get(cfg['model'], f"M{cfg['model']}")
        report_name = f"{name_stem}_{model_label}"
        ini_path = os.path.join(tester_folder, f"{name_stem}.ini")
        build_ini(symbol, period, modified_set, ini_path, report_folder, cfg)
        print(f"    INI       : {ini_path}")
        print(f"    Report as : {report_name}.htm")

        # Launch MT5 minimised
        cmd = [terminal_path, f'/config:{ini_path}']
        print(f"    Launching MT5", end='', flush=True)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 6  # SW_MINIMIZE
        proc = subprocess.Popen(cmd, startupinfo=si)
        while proc.poll() is None:
            time.sleep(10)
            print('.', end='', flush=True)
        print(f" done (exit {proc.returncode})")

        # Copy report files from MT5 terminal folder
        search_dirs = [
            os.path.dirname(tester_folder),
            os.path.join(os.path.dirname(tester_folder), 'MQL5', 'Profiles', 'Tester'),
            tester_folder,
        ]

        success = False
        for search_dir in search_dirs:
            htm_src = os.path.join(search_dir, report_name + '.htm')
            if os.path.isfile(htm_src):
                htm_dest = os.path.join(file_report_dir, report_name + '.htm')
                shutil.copy2(htm_src, htm_dest)
                copied = [report_name + '.htm']
                for fn in os.listdir(search_dir):
                    if fn.startswith(report_name) and not fn.endswith('.htm'):
                        shutil.copy2(os.path.join(search_dir, fn),
                                     os.path.join(file_report_dir, fn))
                        copied.append(fn)
                # Remove from MT5 folder
                os.remove(htm_src)
                for fn in os.listdir(search_dir):
                    if fn.startswith(report_name) and not fn.endswith('.htm'):
                        try:
                            os.remove(os.path.join(search_dir, fn))
                        except:
                            pass
                print(f"    Report    : {len(copied)} file(s) → {file_report_dir}")
                success = True
                break

        if not success:
            print(f"    FAIL      : Report not found. Checked:")
            for d in search_dirs:
                print(f"      {d}")

        results.append({
            'file'    : filename,
            'subfolder': rel_subdir,
            'symbol'  : symbol,
            'period'  : period,
            'success' : success,
        })

    # ── Summary ────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    passed = [r for r in results if r['success']]
    failed = [r for r in results if not r['success']]
    print(f"  Completed: {len(passed)}/{len(results)}")
    if failed:
        print(f"\n  Failed (no report generated):")
        for r in failed:
            subfolder = f" [{r['subfolder']}]" if r.get('subfolder') else ""
            print(f"    - {r['file']}{subfolder} ({r['symbol']} {r['period']})")
        failed_log = os.path.join(report_folder, 'failed_backtests.txt')
        with open(failed_log, 'w') as f:
            for r in failed:
                f.write(f"{r['file']}\t{r['symbol']}\t{r['period']}\n")
        print(f"\n  Failed list saved to: {failed_log}")
    print()


if __name__ == '__main__':
    main()