"""
compile_timeline.py
===================
EA Portfolio Engine — step 1: compile MT5 backtest reports into a trade
timeline dataset that portfolio simulation runs are configured against.

Scans a folder (recursively) for Strategy Tester .htm reports, parses each
into trades + metadata, and writes one timeline dataset:

    timeline/<name>/trades.csv      all trades, tagged with ea_id
    timeline/<name>/ea_meta.csv     one row per EA: symbol, period, lot step,
                                    historical max DD, realized backtest DD,
                                    net profit, trade count, date range,
                                    validation flag vs the 5% DD target
    timeline/<name>/daily_pnl.csv   days x EAs matrix of net P&L (the core
                                    dataset every simulation run reads)

Usage:
    python compile_timeline.py                          # reads reports_in/
    python compile_timeline.py --reports <folder>       # scan another folder
    python compile_timeline.py --name gold_only         # separate timeline
"""

import os
import re
import sys
import glob
import json
import argparse
from datetime import datetime

import numpy as np
import pandas as pd

from parsers import parse_backtest_trades, parse_backtest_summary, parse_backtest_inputs

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_family_map():
    """Strategy-prefix -> product-family overrides (family_map.json).
    Keeps families = EA products even when reports sit in bucket folders."""
    p = os.path.join(ENGINE_DIR, 'family_map.json')
    if not os.path.isfile(p):
        return []
    with open(p, encoding='utf-8') as f:
        m = json.load(f).get('map', {})
    return sorted(m.items(), key=lambda kv: -len(kv[0]))  # longest prefix wins


def map_family(strategy, folder_family, fam_map):
    for prefix, fam in fam_map:
        if strategy.startswith(prefix):
            return fam
    return folder_family

# Backtests are normalised to this: fixed balance, lot step sized so the
# EA's historical max DD ~= DD_TARGET_PCT of the balance.
BALANCE       = 100_000
DD_TARGET_PCT = 5.0

INPUT_KEYS = ['ForceSymbol', 'ManualBalance', 'Risk',
              'LotPerBalance_step', 'HistoricalMaxDD', 'Run_Strategy']

# Report filenames are "<set stem>_<SYMBOL>.a_<period>_<model>"
_EA_ID_SUFFIX = re.compile(r'_(?P<symbol>[A-Za-z0-9.]+\.a)_(?P<period>[A-Za-z0-9]+)_(?P<model>[A-Z]+)$')


def split_ea_id(ea_id):
    """'eurusd_H1_SL17_EURUSD.a_H1_OHLC' -> ('eurusd_H1_SL17', 'EURUSD.a', 'H1')."""
    m = _EA_ID_SUFFIX.search(ea_id)
    if m:
        return ea_id[:m.start()], m.group('symbol'), m.group('period')
    return ea_id, '', ''


def streak_stats(trades):
    """
    Historical streak baselines from one EA's trades (sorted by close_time).
    Streak-cost caveat: a single news-gap / 1m-OHLC artifact loss can dominate
    the cost baseline — largest_single_loss is kept separately so consumers
    can detect that (cost baseline ~= one trade -> treat with suspicion).
    """
    pnl = trades.sort_values('close_time')['net_profit'].to_numpy()

    max_ln = max_wn = loss_n = win_n = 0
    max_lc = loss_c = 0.0
    loss_runs = []
    for v in pnl:
        if v < 0:
            loss_n += 1
            loss_c += float(v)
            if win_n:
                win_n = 0
            max_ln = max(max_ln, loss_n)
            max_lc = min(max_lc, loss_c)
        elif v > 0:
            if loss_n:
                loss_runs.append(loss_n)
            loss_n, loss_c = 0, 0.0
            win_n += 1
            max_wn = max(max_wn, win_n)
    if loss_n:
        loss_runs.append(loss_n)

    # Day-based losing streak (days with trades only)
    daily = trades.groupby(trades['close_time'].dt.date)['net_profit'].sum()
    day_n = max_day_n = 0
    for v in daily.to_numpy():
        if v < 0:
            day_n += 1
            max_day_n = max(max_day_n, day_n)
        elif v > 0:
            day_n = 0

    losses = pnl[pnl < 0]
    return {
        'win_rate_pct'          : round(float((pnl > 0).mean() * 100), 1) if len(pnl) else 0.0,
        'hist_max_loss_streak'  : int(max_ln),
        'hist_max_loss_streak_days': int(max_day_n),
        'hist_max_streak_cost'  : round(-max_lc, 2),
        'hist_avg_loss_streak'  : round(float(np.mean(loss_runs)), 2) if loss_runs else 0.0,
        'hist_max_win_streak'   : int(max_wn),
        'largest_single_loss'   : round(-float(losses.min()), 2) if len(losses) else 0.0,
    }


def compile_reports(reports_dir, out_dir):
    htm_files = sorted(
        glob.glob(os.path.join(reports_dir, '**', '*.htm'), recursive=True) +
        glob.glob(os.path.join(reports_dir, '**', '*.html'), recursive=True))
    if not htm_files:
        print(f"  ERROR: no .htm/.html reports found under {reports_dir}")
        sys.exit(1)

    print(f"  Found {len(htm_files)} report(s) under {reports_dir}")

    # Dedupe by filename — the same report can exist in more than one folder
    # (e.g. a 'live portfolio' collection duplicating strategy folders).
    # Prefer the copy in its strategy family folder.
    by_name = {}
    for path in htm_files:
        by_name.setdefault(os.path.basename(path), []).append(path)
    dropped = []
    deduped = []
    for fname, paths in sorted(by_name.items()):
        if len(paths) > 1:
            paths = sorted(paths, key=lambda p: ('live portfolio' in p.lower(), p))
            dropped.extend(paths[1:])
        deduped.append(paths[0])
    htm_files = sorted(deduped)
    if dropped:
        print(f"  Dropped {len(dropped)} duplicate report(s) (same filename in multiple folders):")
        for p in dropped:
            print(f"    - {os.path.relpath(p, reports_dir)}")

    all_trades = []
    meta_rows  = []
    skipped    = []
    fam_map    = load_family_map()

    for path in htm_files:
        fname = os.path.basename(path)
        ea_id = os.path.splitext(fname)[0]
        rel   = os.path.relpath(os.path.dirname(path), reports_dir)
        family = '' if rel == '.' else rel.replace(os.sep, '/').replace('/reports', '')

        with open(path, 'rb') as f:
            raw = f.read()

        summary = parse_backtest_summary(raw)
        if summary is None:
            skipped.append((fname, 'not a Strategy Tester report'))
            continue

        trades = parse_backtest_trades(raw)
        if trades is None or trades.empty:
            skipped.append((fname, 'no trades found'))
            continue

        inputs   = parse_backtest_inputs(raw, keys=INPUT_KEYS)
        lot_step = float(inputs.get('LotPerBalance_step', 0) or 0)
        hist_dd  = float(inputs.get('HistoricalMaxDD', 0) or 0)

        strategy, sym_from_id, tf_from_id = split_ea_id(ea_id)
        family = map_family(strategy, family, fam_map)

        trades = trades.copy()
        trades.insert(0, 'ea_id', ea_id)
        trades.insert(1, 'strategy', strategy)
        trades.insert(2, 'family', family)
        all_trades.append(trades)

        realized_dd_pct = summary.get('balance_dd_max_pct')
        meta_rows.append({
            'ea_id'            : ea_id,
            'strategy'         : strategy,
            'family'           : family,
            'symbol'           : summary.get('symbol', '') or sym_from_id,
            'timeframe'        : tf_from_id or str(summary.get('period', '')).split(' ')[0],
            'force_symbol'     : inputs.get('ForceSymbol', ''),
            'run_strategy'     : inputs.get('Run_Strategy', ''),
            # Base risk normalisation: fixed balance, lot step sized so the
            # set author's HistoricalMaxDD input ~= risk_pct_target of balance.
            'balance'          : BALANCE,
            'risk_pct_target'  : DD_TARGET_PCT,
            'hist_max_dd'      : hist_dd,
            'lot_step'         : lot_step,
            'net_profit'       : summary.get('total_net_profit', 0.0),
            'realized_dd'      : summary.get('balance_dd_max', 0.0),
            'realized_dd_pct'  : realized_dd_pct,
            'equity_dd_pct'    : summary.get('equity_dd_max_pct'),
            'trades'           : len(trades),
            'first_trade'      : trades['close_time'].min(),
            'last_trade'       : trades['close_time'].max(),
            **streak_stats(trades),
            # Validation: realized backtest DD vs the calibration target.
            # >1.0 = drew down more than target; far from 1.0 = the set's
            # HistoricalMaxDD input doesn't describe this window (stale
            # calibration or an untouched default from the set author).
            'dd_vs_target'     : round(realized_dd_pct / DD_TARGET_PCT, 2)
                                 if realized_dd_pct else None,
            'report_path'      : path,
        })

    if not all_trades:
        print("  ERROR: no parseable reports.")
        sys.exit(1)

    trades_df = pd.concat(all_trades, ignore_index=True)
    meta_df   = pd.DataFrame(meta_rows)

    # Daily P&L matrix — realized net profit summed on the close date.
    trades_df['close_date'] = trades_df['close_time'].dt.date
    daily = (trades_df
             .pivot_table(index='close_date', columns='ea_id',
                          values='net_profit', aggfunc='sum')
             .sort_index())
    # 0 = EA active but no trade closed that day. Days before an EA's first
    # trade / after its last stay 0 too — meta carries the active range.
    daily = daily.fillna(0.0).round(2)

    os.makedirs(out_dir, exist_ok=True)
    trades_df.drop(columns=['close_date']).to_csv(
        os.path.join(out_dir, 'trades.csv'), index=False)
    meta_df.to_csv(os.path.join(out_dir, 'ea_meta.csv'), index=False)
    daily.to_csv(os.path.join(out_dir, 'daily_pnl.csv'), index_label='date')

    # ── manifest.json — self-describing bundle, safe to hand to another AI ──
    manifest = {
        'dataset': {
            'name'         : os.path.basename(out_dir),
            'generated'    : datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'description'  : ('Backtest trade timeline for a pool of Ultimate Breakout '
                              'System (UBS) EA set files, compiled from MT5 Strategy '
                              'Tester reports for portfolio rotation / risk-scaling '
                              'simulation.'),
            'ea_count'     : len(meta_df),
            'trade_count'  : int(len(trades_df)),
            'first_trade'  : str(trades_df['close_time'].min().date()),
            'last_trade'   : str(trades_df['close_time'].max().date()),
            'trading_days' : int(len(daily)),
        },
        'risk_normalisation': {
            'balance'          : BALANCE,
            'risk_pct_target'  : DD_TARGET_PCT,
            'method'           : ('Every backtest ran on a fixed balance with fixed lots: '
                                  'LotPerBalance_step = HistoricalMaxDD / '
                                  f'{DD_TARGET_PCT / 100}, so each EA at weight 1.0 targets a '
                                  f'historical max drawdown of {DD_TARGET_PCT}% of balance.'),
            'linearity'        : ('Fixed balance + fixed lots means P&L is additive across '
                                  'EAs and linear in risk: running an EA at weight w equals '
                                  'w x its daily P&L. No compounding inside any backtest.'),
            'important_caveat' : ('HistoricalMaxDD is calibrated from history and is NOT a '
                                  'limit — future drawdowns can and do exceed it. '
                                  'dd_vs_target in ea_meta shows realized backtest DD vs '
                                  'target; values far from 1.0 mean the set author\'s '
                                  'HistoricalMaxDD input does not describe this test window '
                                  '(stale calibration or an untouched default).'),
        },
        'files': {
            'trades.csv'   : ('One row per closed trade. Columns: ea_id, strategy, family, '
                              'open_time, close_time, symbol, type, volume, open_price, '
                              'close_price, commission, swap, profit, comment, net_profit '
                              '(= profit + commission + swap, in account currency).'),
            'ea_meta.csv'  : ('One row per EA. ea_id (report stem), strategy (set file '
                              'stem), family (source folder), symbol, timeframe, balance, '
                              'risk_pct_target, hist_max_dd (set input), lot_step, '
                              'net_profit, realized_dd(_pct), equity_dd_pct, trades, '
                              'first/last_trade, dd_vs_target, report_path.'),
            'daily_pnl.csv': ('Matrix of net P&L: one row per calendar date, one column '
                              'per ea_id, value = sum of net_profit of trades closed that '
                              'day (0 = no trades closed). The core dataset for daily '
                              'portfolio simulation.'),
        },
        'eas': json.loads(meta_df.assign(
            first_trade=meta_df['first_trade'].astype(str),
            last_trade=meta_df['last_trade'].astype(str),
        ).to_json(orient='records')),
    }
    with open(os.path.join(out_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)

    # ── Report ────────────────────────────────────────────────────────────
    print()
    print(f"  EAs compiled : {len(meta_df)}")
    print(f"  Trades       : {len(trades_df)}")
    print(f"  Date range   : {trades_df['close_time'].min():%Y-%m-%d} -> "
          f"{trades_df['close_time'].max():%Y-%m-%d}")
    print(f"  Trading days : {len(daily)}")
    print()
    print(f"  Output       : {out_dir}")
    print(f"    trades.csv, ea_meta.csv, daily_pnl.csv, manifest.json")

    if skipped:
        print()
        print(f"  Skipped {len(skipped)} file(s):")
        for fname, why in skipped:
            print(f"    - {fname}: {why}")

    # DD calibration check — flag EAs whose backtest DD strayed from target
    flagged = meta_df[(meta_df['dd_vs_target'].notna()) &
                      ((meta_df['dd_vs_target'] > 1.5) | (meta_df['dd_vs_target'] < 0.5))]
    if not flagged.empty:
        print()
        print(f"  DD calibration check — {len(flagged)} EA(s) outside 0.5x-1.5x "
              f"of the {DD_TARGET_PCT}% target:")
        for _, r in flagged.sort_values('dd_vs_target', ascending=False).iterrows():
            print(f"    {r['ea_id']:60s} realized {r['realized_dd_pct']:.1f}% "
                  f"({r['dd_vs_target']:.1f}x target)")
    else:
        print()
        print(f"  DD calibration check: all EAs within 0.5x-1.5x of "
              f"the {DD_TARGET_PCT}% target.")


def main():
    ap = argparse.ArgumentParser(description='Compile MT5 backtest reports into a timeline dataset.')
    ap.add_argument('--reports', default=os.path.join(ENGINE_DIR, 'reports_in'),
                    help='Folder to scan (recursively) for .htm reports [reports_in]')
    ap.add_argument('--name', default='default',
                    help='Timeline name — output goes to timeline/<name>/ [default]')
    args = ap.parse_args()

    out_dir = os.path.join(ENGINE_DIR, 'timeline', args.name)
    print("=" * 70)
    print("  EA Portfolio Engine — Timeline Compiler")
    print("=" * 70)
    compile_reports(args.reports, out_dir)


if __name__ == '__main__':
    main()
