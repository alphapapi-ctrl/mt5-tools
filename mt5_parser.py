"""
mt5_parser.py
=============
Parsers for three MT5 trade report formats:
  1. MT5 Real Account HTM export
  2. MT5 Backtest HTM report
  3. Quant Analyzer CSV export

All normalise to a common DataFrame schema.
"""

import pandas as pd
import re


# ── Common schema ─────────────────────────────────────────────────────────────
# open_time, close_time, symbol, type, volume, open_price, close_price,
# sl, tp, commission, swap, profit, net_profit, comment, strategy,
# duration_min, win, day_of_week, hour, source

def _decode(file_bytes):
    for enc in ['utf-16', 'utf-8', 'latin-1', 'cp1252']:
        try:
            return file_bytes.decode(enc)
        except:
            continue
    return ''


def _strip(s):
    return re.sub(r'<[^>]+>', '', s).strip().replace('\xa0', '').replace('\u00a0', '')


def _to_float(s):
    try:
        return float(str(s).replace(' ', '').replace(',', ''))
    except:
        return None


def _to_dt(s, fmt='%Y.%m.%d %H:%M:%S'):
    return pd.to_datetime(s, format=fmt, errors='coerce')


def _enrich(df):
    """Add derived columns common to all formats."""
    df['open_time']    = pd.to_datetime(df['open_time'],  errors='coerce')
    df['close_time']   = pd.to_datetime(df['close_time'], errors='coerce')
    df['open_date']    = df['open_time'].dt.date
    df['close_date']   = df['close_time'].dt.date
    df['day_of_week']  = df['open_time'].dt.day_name()
    df['hour']         = df['open_time'].dt.hour
    df['duration_min'] = ((df['close_time'] - df['open_time'])
                          .dt.total_seconds() / 60).round(1)
    for col in ['volume', 'open_price', 'close_price', 'sl', 'tp',
                'commission', 'swap', 'profit']:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(' ', '').str.replace(',', ''),
                errors='coerce'
            )
    if 'net_profit' not in df.columns:
        df['net_profit'] = (
            df.get('profit', 0).fillna(0) +
            df.get('commission', 0).fillna(0) +
            df.get('swap', 0).fillna(0)
        )
    df['win']      = df['net_profit'] > 0
    df['type']     = df['type'].str.lower().str.strip()
    df['strategy'] = df['comment'].apply(extract_strategy)
    # Normalise symbol — strip .a suffix for display matching
    df['symbol_base'] = df['symbol'].str.replace(r'\.[a-z]+$', '', regex=True).str.upper()
    return df


# ── Format 1: Real Account HTM ────────────────────────────────────────────────

def parse_mt5_report(file_bytes):
    """Parse MT5 real account HTML trade history report."""
    text  = _decode(file_bytes)
    rows  = re.findall(r'<tr[^>]*>(.*?)</tr>', text, re.DOTALL)
    trades    = []
    in_trades = False

    COLS = ['open_time', 'position', 'symbol', 'type', 'comment', 'volume',
            'open_price', 'sl', 'tp', 'close_time', 'close_price',
            'commission', 'swap', 'profit']

    for row in rows:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
        cells = [re.sub(r'\s+', ' ', _strip(c)).strip() for c in cells]

        if cells and cells[0] == 'Time' and len(cells) >= 13:
            in_trades = True
            continue
        if not in_trades:
            continue
        if cells and any(kw in cells[0] for kw in
                         ['Total Net Profit', 'Results', 'Balance', 'Equity']):
            break

        if len(cells) >= 14 and re.match(r'\d{4}\.\d{2}\.\d{2}', cells[0]):
            last = [c.lower() for c in cells if c]
            if any(s in last for s in ['placed', 'cancelled', 'expired', 'partial']):
                continue
            if len(cells) < 10 or not re.match(r'\d{4}\.\d{2}\.\d{2}', cells[9]):
                continue
            if '/' in str(cells[5]):
                continue
            try:
                trade = dict(zip(COLS, cells[:14]))
                trades.append(trade)
            except:
                continue

    if not trades:
        return None

    df = pd.DataFrame(trades)
    df['source'] = 'real'
    return _enrich(df)


# ── Format 2: Backtest HTM ────────────────────────────────────────────────────

def parse_backtest_report(file_bytes):
    """
    Parse MT5 Strategy Tester HTML report.
    Pairs in/out deals into complete trades.
    """
    text   = _decode(file_bytes)
    tables = re.findall(r'<table[^>]*>(.*?)</table>', text, re.DOTALL)
    if len(tables) < 2:
        return None

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tables[1], re.DOTALL)

    # Find deals section
    in_deals = False
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

    # Columns: Time, Deal, Symbol, Type, Direction, Volume, Price, Order,
    #          Commission, Swap, Profit, Balance, Comment
    DEAL_COLS = ['time', 'deal', 'symbol', 'type', 'direction', 'volume',
                 'price', 'order', 'commission', 'swap', 'profit', 'balance', 'comment']

    deals = []
    for row in deal_rows:
        d = dict(zip(DEAL_COLS, row[:len(DEAL_COLS)]))
        deals.append(d)

    df_deals = pd.DataFrame(deals)
    df_deals  = df_deals[df_deals['direction'].isin(['in', 'out'])]

    # FIFO stack matching by symbol — handles concurrent positions on same symbol.
    # Skip daily commission/balance rows (no symbol).
    # Each 'in' is pushed to the stack; each 'out' pops the oldest open entry (FIFO).
    open_stack = {}  # symbol -> list of open 'in' deals (FIFO)
    trades     = []

    for _, deal in df_deals.iterrows():
        sym  = deal.get('symbol', '').strip()
        dirn = deal.get('direction', '').strip()

        # Skip commission/balance rows
        if not sym:
            continue

        if dirn == 'in':
            open_stack.setdefault(sym, []).append(deal)
        elif dirn == 'out':
            stack = open_stack.get(sym, [])
            if stack:
                entry = stack.pop(0)  # FIFO — oldest open first
                trades.append({
                    'open_time'  : entry['time'],
                    'close_time' : deal['time'],
                    'symbol'     : sym,
                    'type'       : entry.get('type', ''),
                    'volume'     : entry['volume'],
                    'open_price' : entry['price'],
                    'close_price': deal['price'],
                    'sl'         : None,
                    'tp'         : None,
                    'commission' : _to_float(entry.get('commission', 0)),
                    'swap'       : _to_float(deal.get('swap', 0)),
                    'profit'     : _to_float(deal.get('profit', 0)),
                    'comment'    : deal.get('comment', ''),
                    'position'   : entry.get('deal', ''),
                })

    if not trades:
        return None

    df = pd.DataFrame(trades)
    df['source'] = 'backtest'
    return _enrich(df)


# ── Format 3: Quant Analyzer CSV ─────────────────────────────────────────────

def parse_quant_csv(file_bytes):
    """Parse Quant Analyzer listOfTrades CSV export."""
    try:
        text = file_bytes.decode('utf-8-sig')
    except:
        text = file_bytes.decode('latin-1')

    from io import StringIO
    df_raw = pd.read_csv(StringIO(text))

    # Normalise column names
    df_raw.columns = [c.strip().lower().replace(' ', '_').replace('/', '_').replace('(', '').replace(')', '') for c in df_raw.columns]

    col_map = {
        'open_time'    : ['open_time_$', 'open_time_$_', 'open_time', 'opentime'],
        'close_time'   : ['close_time_$', 'close_time_$_', 'close_time', 'closetime'],
        'symbol'       : ['symbol_$', 'symbol_$_', 'symbol'],
        'type'         : ['type_$', 'type_$_', 'type', 'direction'],
        'volume'       : ['size_$', 'size_$_', 'size', 'volume', 'lots'],
        'open_price'   : ['open_price_$', 'open_price_$_', 'open_price', 'openprice'],
        'close_price'  : ['close_price_$', 'close_price_$_', 'close_price', 'closeprice'],
        'profit'       : ['profit_loss_$', 'profit_loss_$_', 'profit_loss', 'profit', 'net_profit'],
        'commission'   : ['comm_swap_$', 'comm_swap_$_', 'commission', 'comm'],
        'swap'         : ['swap_$', 'swap'],
        'sl'           : ['stop_loss_$', 'stop_loss_$_', 'stop_loss', 'sl'],
        'comment'      : ['comment_$', 'comment_$_', 'comment'],
        'strategy'     : ['strategy_name_$', 'strategy_name_$_', 'strategy_name', 'strategy'],
        'mae'          : ['mae_$', 'mae_$_', 'mae'],
        'mfe'          : ['mfe_$', 'mfe_$_', 'mfe'],
        'drawdown'     : ['drawdown_$', 'drawdown_$_', 'drawdown'],
    }

    result = {}
    for target, candidates in col_map.items():
        for cand in candidates:
            if cand in df_raw.columns:
                result[target] = df_raw[cand]
                break

    df = pd.DataFrame(result)

    # Parse datetimes — QA uses DD.MM.YYYY HH:MM:SS
    for col in ['open_time', 'close_time']:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], format='%d.%m.%Y %H:%M:%S', errors='coerce')
            if df[col].isna().all():
                df[col] = pd.to_datetime(df[col], infer_datetime_format=True, errors='coerce')

    # QA comm_swap is combined — split evenly as approximation if no separate swap
    if 'commission' in df.columns and 'swap' not in df.columns:
        df['swap'] = 0.0

    if 'sl' not in df.columns:
        df['sl'] = None
    if 'tp' not in df.columns:
        df['tp'] = None
    if 'position' not in df.columns:
        df['position'] = df.get('ticket', range(len(df)))

    df['source'] = 'quant_csv'

    # Add extra QA-specific columns if present
    for extra in ['mae', 'mfe', 'drawdown']:
        if extra in df_raw.columns:
            df[extra] = pd.to_numeric(df_raw[extra], errors='coerce')

    return _enrich(df)


# ── Auto-detect format ────────────────────────────────────────────────────────

def detect_and_parse(file_bytes, filename=''):
    """
    Auto-detect file format and parse.
    Returns (df, format_name) or (None, None).
    """
    fname = filename.lower()

    if fname.endswith('.csv'):
        df = parse_quant_csv(file_bytes)
        return df, 'Quant Analyzer CSV'

    # HTML/HTM — detect backtest vs real account
    try:
        text = _decode(file_bytes)
    except:
        return None, None

    if 'Strategy Tester Report' in text or 'strategy tester' in text.lower():
        df = parse_backtest_report(file_bytes)
        return df, 'MT5 Backtest Report'

    df = parse_mt5_report(file_bytes)
    return df, 'MT5 Account History'


# ── Stats ─────────────────────────────────────────────────────────────────────

def calc_stats(df):
    if df is None or len(df) == 0:
        return {}

    total        = len(df)
    wins         = df[df['win'] == True]
    losses       = df[df['win'] == False]
    win_rate     = round(len(wins) / total * 100, 1) if total > 0 else 0
    gross_profit = round(wins['net_profit'].sum(), 2)
    gross_loss   = round(losses['net_profit'].sum(), 2)
    net_profit   = round(df['net_profit'].sum(), 2)
    pf           = round(abs(gross_profit / gross_loss), 2) if gross_loss != 0 else float('inf')
    avg_win      = round(wins['net_profit'].mean(), 2)  if len(wins) > 0 else 0
    avg_loss     = round(losses['net_profit'].mean(), 2) if len(losses) > 0 else 0
    rr           = round(abs(avg_win / avg_loss), 2)    if avg_loss != 0 else float('inf')
    expectancy   = round((win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss), 2)

    results           = df.sort_values('close_time')['win'].tolist()
    max_cw            = _max_consec(results, True)
    max_cl            = _max_consec(results, False)

    cumulative  = df.sort_values('close_time')['net_profit'].cumsum()
    rolling_max = cumulative.cummax()
    max_dd      = round((cumulative - rolling_max).min(), 2)

    avg_dur     = round(df['duration_min'].mean(), 1)  if 'duration_min' in df.columns else 0
    avg_win_dur = round(wins['duration_min'].mean(), 1) if len(wins) > 0 else 0
    avg_los_dur = round(losses['duration_min'].mean(), 1) if len(losses) > 0 else 0

    longs  = df[df['type'] == 'buy']
    shorts = df[df['type'] == 'sell']

    return {
        'total_trades'      : total,
        'win_rate'          : win_rate,
        'net_profit'        : net_profit,
        'gross_profit'      : gross_profit,
        'gross_loss'        : gross_loss,
        'profit_factor'     : pf,
        'avg_win'           : avg_win,
        'avg_loss'          : avg_loss,
        'rr_ratio'          : rr,
        'expectancy'        : expectancy,
        'max_consec_wins'   : max_cw,
        'max_consec_losses' : max_cl,
        'max_drawdown'      : max_dd,
        'best_trade'        : round(df['net_profit'].max(), 2),
        'worst_trade'       : round(df['net_profit'].min(), 2),
        'avg_duration_min'  : avg_dur,
        'avg_win_duration'  : avg_win_dur,
        'avg_loss_duration' : avg_los_dur,
        'long_trades'       : len(longs),
        'short_trades'      : len(shorts),
        'long_win_rate'     : round(len(longs[longs['win']]) / len(longs) * 100, 1) if len(longs) > 0 else 0,
        'short_win_rate'    : round(len(shorts[shorts['win']]) / len(shorts) * 100, 1) if len(shorts) > 0 else 0,
    }


def extract_strategy(comment):
    if not comment or str(comment).strip() == '':
        return 'Manual'
    parts = str(comment).split('_')
    while parts and re.match(r'^\d+$', parts[-1]):
        parts.pop()
    if parts and re.match(r'^[A-Z]{3,8}(\.a)?$', parts[-1]):
        parts.pop()
    return '_'.join(parts) if parts else str(comment)


def _max_consec(results, target):
    max_c = cur_c = 0
    for r in results:
        if r == target:
            cur_c += 1
            max_c  = max(max_c, cur_c)
        else:
            cur_c  = 0
    return max_c