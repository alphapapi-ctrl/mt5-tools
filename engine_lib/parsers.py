"""
parsers.py
==========
MT5 Strategy Tester HTML report parsing for the EA Portfolio Engine.
Adapted from MT5Tools/mt5_parser.py (same deal-pairing logic) with an
added Inputs-section extractor for LotPerBalance_step / HistoricalMaxDD.
"""

import re
import pandas as pd


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _decode(file_bytes):
    """Decode report bytes — MT5 tester reports are UTF-16 LE with BOM."""
    if file_bytes[:2] == b'\xff\xfe':
        return file_bytes.decode('utf-16-le', errors='replace')
    if file_bytes[:2] == b'\xfe\xff':
        return file_bytes.decode('utf-16-be', errors='replace')
    return file_bytes.decode('utf-8', errors='replace')


def _strip(s):
    return re.sub(r'<[^>]+>', '', s)


def _to_float(s):
    if s is None:
        return 0.0
    try:
        return float(str(s).replace(' ', '').replace(',', ''))
    except (ValueError, TypeError):
        return 0.0


# ── Trades (Deals section, FIFO-paired) ───────────────────────────────────────

def parse_backtest_trades(file_bytes):
    """
    Parse the Deals section of an MT5 Strategy Tester report and pair
    in/out deals into complete trades (FIFO per symbol).
    Returns a DataFrame or None.
    """
    text   = _decode(file_bytes)
    tables = re.findall(r'<table[^>]*>(.*?)</table>', text, re.DOTALL)
    if len(tables) < 2:
        return None

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tables[1], re.DOTALL)

    in_deals  = False
    deal_rows = []
    for row in rows:
        cells = [re.sub(r'\s+', ' ', _strip(c)).strip()
                 for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)]
        cells = [c for c in cells if c]
        if not cells:
            continue
        if 'Deals' in cells:
            in_deals = True
            continue
        if in_deals and cells[0] == 'Time' and 'Deal' in cells:
            continue  # header row
        if in_deals and len(cells) >= 7 and re.match(r'\d{4}\.\d{2}\.\d{2}', cells[0]):
            deal_rows.append(cells)

    if not deal_rows:
        return None

    DEAL_COLS = ['time', 'deal', 'symbol', 'type', 'direction', 'volume',
                 'price', 'order', 'commission', 'swap', 'profit', 'balance', 'comment']
    df_deals = pd.DataFrame([dict(zip(DEAL_COLS, r[:len(DEAL_COLS)])) for r in deal_rows])
    df_deals = df_deals[df_deals['direction'].isin(['in', 'out'])]

    open_stack = {}  # symbol -> list of open 'in' deals (FIFO)
    trades     = []
    for _, deal in df_deals.iterrows():
        sym  = deal.get('symbol', '').strip()
        dirn = deal.get('direction', '').strip()
        if not sym:
            continue  # commission/balance rows
        if dirn == 'in':
            open_stack.setdefault(sym, []).append(deal)
        elif dirn == 'out':
            stack = open_stack.get(sym, [])
            if stack:
                entry = stack.pop(0)
                trades.append({
                    'open_time'  : entry['time'],
                    'close_time' : deal['time'],
                    'symbol'     : sym,
                    'type'       : entry.get('type', ''),
                    'volume'     : _to_float(entry['volume']),
                    'open_price' : _to_float(entry['price']),
                    'close_price': _to_float(deal['price']),
                    'commission' : _to_float(entry.get('commission', 0)),
                    'swap'       : _to_float(deal.get('swap', 0)),
                    'profit'     : _to_float(deal.get('profit', 0)),
                    'comment'    : entry.get('comment', '') or deal.get('comment', ''),
                })

    if not trades:
        return None

    df = pd.DataFrame(trades)
    df['open_time']  = pd.to_datetime(df['open_time'],  format='%Y.%m.%d %H:%M:%S', errors='coerce')
    df['close_time'] = pd.to_datetime(df['close_time'], format='%Y.%m.%d %H:%M:%S', errors='coerce')
    df['net_profit'] = df['profit'] + df['commission'] + df['swap']
    return df


# ── Summary (Settings/Results header) ─────────────────────────────────────────

def parse_backtest_summary(file_bytes):
    """
    Parse the Settings/Results summary. Returns a dict with expert, symbol,
    period, initial_deposit, total_net_profit, balance/equity max DD ($ and %),
    or None if this isn't a Strategy Tester report.
    """
    text = _decode(file_bytes)
    if 'Strategy Tester Report' not in text and 'strategy tester' not in text.lower():
        return None

    rows    = re.findall(r'<tr[^>]*>(.*?)</tr>', text, re.DOTALL)
    summary = {}

    def _dd_pair(value):
        # "118.84 (0.12%)" -> (118.84, 0.12)
        m = re.match(r'([\d\s,\.]+?)\s*\(([\d\.]+)%\)', value)
        if m:
            return _to_float(m.group(1)), _to_float(m.group(2))
        return _to_float(value), None

    LABELS = {
        'Expert:'                   : ('expert',           str),
        'Symbol:'                   : ('symbol',           str),
        'Period:'                   : ('period',           str),
        'Currency:'                 : ('currency',         str),
        'Initial Deposit:'          : ('initial_deposit',  _to_float),
        'Total Net Profit:'         : ('total_net_profit', _to_float),
        'Balance Drawdown Maximal:' : ('balance_dd_max',   _dd_pair),
        'Equity Drawdown Maximal:'  : ('equity_dd_max',    _dd_pair),
    }

    for row in rows:
        cells = [re.sub(r'\s+', ' ', _strip(c)).strip()
                 for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)]
        cells = [c for c in cells if c]
        for j in range(len(cells) - 1):
            key = LABELS.get(cells[j])
            if key is None:
                continue
            name, conv = key
            if name in summary:
                continue
            val = conv(cells[j + 1])
            if name in ('balance_dd_max', 'equity_dd_max'):
                summary[name], summary[name + '_pct'] = val
            else:
                summary[name] = val
        if 'equity_dd_max' in summary and 'balance_dd_max' in summary:
            break

    return summary or None


# ── Inputs section ────────────────────────────────────────────────────────────

def parse_backtest_inputs(file_bytes, keys=None):
    """
    Extract input parameters from the report's Inputs section.
    Returns {name: value-string}. If keys is given, only those inputs.
    """
    text   = _decode(file_bytes)
    plain  = re.sub(r'<[^>]+>', ' ', text)
    result = {}
    wanted = set(keys) if keys else None
    for m in re.finditer(r'([A-Za-z0-9_]+)=([^\s<]+)', plain):
        name, val = m.group(1), m.group(2)
        if wanted is not None and name not in wanted:
            continue
        if name not in result:  # first occurrence wins
            result[name] = val
    return result
