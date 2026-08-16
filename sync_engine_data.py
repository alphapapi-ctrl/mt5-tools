"""
sync_engine_data.py
===================
Refresh the base data and helper code that MT5Tools bundles from the UBS
Portfolio Manager (EA_Portfolio_engine) so the Live UBS EA Management
page works STANDALONE — a clone of mt5-tools alone has everything it needs.

Bundled (committed to this repo):
  engine_data/timeline/main_pool_2018/     ea_meta.csv, daily_pnl.csv,
                                           manifest.json, description.txt
  engine_data/timeline/proxy_3m_realticks/ same four files
  engine_lib/                              parsers.py, compile_timeline.py,
                                           fill_trust.py, family_map.json
                                           (verbatim copies from the engine)

Only the files the live layer reads are bundled (no trades.csv / regime
matrices — those stay in the engine repo for the simulator).

Usage:  python sync_engine_data.py [--engine <path to EA_Portfolio_engine>]
Run it whenever the engine's datasets change, then commit.
"""
import os
import argparse
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
TIMELINES = ['main_pool_2018', 'proxy_3m_realticks']
DATA_FILES = ['ea_meta.csv', 'daily_pnl.csv', 'manifest.json', 'description.txt']
LIB_FILES = ['parsers.py', 'compile_timeline.py', 'fill_trust.py', 'family_map.json']


def find_engine():
    for c in (r'C:\BulkBackTest\EA_Portfolio_engine',
              os.path.join(os.path.dirname(HERE), 'EA_Portfolio_engine')):
        if os.path.isfile(os.path.join(c, 'compile_timeline.py')):
            return c
    return ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--engine', default=find_engine())
    a = ap.parse_args()
    if not a.engine:
        raise SystemExit('engine repo not found — pass --engine <path>')
    for tl in TIMELINES:
        src = os.path.join(a.engine, 'timeline', tl)
        dst = os.path.join(HERE, 'engine_data', 'timeline', tl)
        os.makedirs(dst, exist_ok=True)
        for f in DATA_FILES:
            if os.path.isfile(os.path.join(src, f)):
                shutil.copy(os.path.join(src, f), os.path.join(dst, f))
        print(f'synced timeline {tl}')
    lib = os.path.join(HERE, 'engine_lib')
    os.makedirs(lib, exist_ok=True)
    for f in LIB_FILES:
        shutil.copy(os.path.join(a.engine, f), os.path.join(lib, f))
    open(os.path.join(lib, '__init__.py'), 'a').close()
    print(f'synced engine_lib ({len(LIB_FILES)} files)')


if __name__ == '__main__':
    main()
